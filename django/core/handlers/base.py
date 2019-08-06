import asyncio
import logging
import types

from asgiref.sync import async_to_sync, sync_to_async

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed
from django.core.signals import request_finished
from django.db import connections, transaction
from django.urls import get_resolver, set_urlconf
from django.utils.log import log_response
from django.utils.module_loading import import_string

from .exception import convert_exception_to_response

logger = logging.getLogger('django.request')


class BaseHandler:
    _view_middleware = None
    _template_response_middleware = None
    _exception_middleware = None
    _middleware_chain = None

    def load_middleware(self):
        """
        Populate middleware lists from settings.MIDDLEWARE.

        Must be called after the environment is fixed (see __call__ in subclasses).
        """
        self._view_middleware = []
        self._template_response_middleware = []
        self._exception_middleware = []

        handler = convert_exception_to_response(self._get_response)
        for middleware_path in reversed(settings.MIDDLEWARE):
            middleware = import_string(middleware_path)
            try:
                if getattr(middleware, "_is_async", False):
                    mw_instance = middleware(handler)
                else:
                    if settings.DEBUG:
                        logger.debug('Synchronous middleware adapted: %r', middleware_path)
                    mw_instance = middleware(async_to_sync(handler))
            except MiddlewareNotUsed as exc:
                if settings.DEBUG:
                    if str(exc):
                        logger.debug('MiddlewareNotUsed(%r): %s', middleware_path, exc)
                    else:
                        logger.debug('MiddlewareNotUsed: %r', middleware_path)
                continue

            if mw_instance is None:
                raise ImproperlyConfigured(
                    'Middleware factory %s returned None.' % middleware_path
                )

            if hasattr(mw_instance, 'process_view'):
                if asyncio.iscoroutinefunction(mw_instance.process_view):
                    self._view_middleware.insert(0, mw_instance.process_view)
                else:
                    self._view_middleware.insert(0, sync_to_async(mw_instance.process_view, thread_sensitive=True))
            if hasattr(mw_instance, 'process_template_response'):
                if asyncio.iscoroutinefunction(mw_instance.process_template_response):
                    self._template_response_middleware.append(mw_instance.process_template_response)
                else:
                    self._template_response_middleware.append(
                        sync_to_async(mw_instance.process_template_response, thread_sensitive=True)
                    )
            if hasattr(mw_instance, 'process_exception'):
                if asyncio.iscoroutinefunction(mw_instance.process_exception):
                    self._exception_middleware.append(mw_instance.process_exception)
                else:
                    self._exception_middleware.append(
                        sync_to_async(mw_instance.process_exception, thread_sensitive=True)
                    )

            if asyncio.iscoroutinefunction(mw_instance):
                handler = convert_exception_to_response(mw_instance)
            else:
                handler = convert_exception_to_response(sync_to_async(mw_instance, thread_sensitive=True))

        # We only assign to this when initialization is complete as it is used
        # as a flag for initialization being complete.
        self._middleware_chain = handler

    def make_view_atomic(self, view):
        non_atomic_requests = getattr(view, '_non_atomic_requests', set())
        for db in connections.all():
            if db.settings_dict['ATOMIC_REQUESTS'] and db.alias not in non_atomic_requests:
                if asyncio.iscoroutinefunction(view):
                    raise RuntimeError("You cannot use ATOMIC_REQUESTS with async views.")
                view = transaction.atomic(using=db.alias)(view)
        return view

    async def get_response(self, request):
        """Return an HttpResponse object for the given HttpRequest."""
        # Setup default url resolver for this thread
        set_urlconf(settings.ROOT_URLCONF)
        response = await self._middleware_chain(request)
        response._closable_objects.append(request)
        if response.status_code >= 400:
            await sync_to_async(log_response)(
                '%s: %s', response.reason_phrase, request.path,
                response=response,
                request=request,
            )
        return response

    async def _get_response(self, request):
        """
        Resolve and call the view, then apply view, exception, and
        template_response middleware. This method is everything that happens
        inside the request/response middleware.
        """
        response = None

        if hasattr(request, 'urlconf'):
            urlconf = request.urlconf
            set_urlconf(urlconf)
            resolver = get_resolver(urlconf)
        else:
            resolver = get_resolver()

        resolver_match = resolver.resolve(request.path_info)
        callback, callback_args, callback_kwargs = resolver_match
        request.resolver_match = resolver_match

        # Apply view middleware
        for middleware_method in self._view_middleware:
            response = await middleware_method(request, callback, callback_args, callback_kwargs)
            if response:
                break

        if response is None:
            wrapped_callback = self.make_view_atomic(callback)
            # If it is a synchronous view, run it in a subthread
            if not asyncio.iscoroutinefunction(wrapped_callback):
                wrapped_callback = sync_to_async(wrapped_callback, thread_sensitive=True)
            try:
                response = await wrapped_callback(request, *callback_args, **callback_kwargs)
            except Exception as e:
                response = await self.process_exception_by_middleware(e, request)

        # Complain if the view returned None (a common error).
        if response is None:
            if isinstance(callback, types.FunctionType):    # FBV
                view_name = callback.__name__
            else:                                           # CBV
                view_name = callback.__class__.__name__ + '.__call__'

            raise ValueError(
                "The view %s.%s didn't return an HttpResponse object. It "
                "returned None instead." % (callback.__module__, view_name)
            )

        # If the response supports deferred rendering, apply template
        # response middleware and then render the response
        elif hasattr(response, 'render') and callable(response.render):
            for middleware_method in self._template_response_middleware:
                response = await middleware_method(request, response)
                # Complain if the template response middleware returned None (a common error).
                if response is None:
                    raise ValueError(
                        "%s.process_template_response didn't return an "
                        "HttpResponse object. It returned None instead."
                        % (middleware_method.__self__.__class__.__name__)
                    )

            try:
                if asyncio.iscoroutinefunction(response.render):
                    response = await response.render()
                else:
                    response = await sync_to_async(response.render, thread_sensitive=True)()
            except Exception as e:
                response = await self.process_exception_by_middleware(e, request)

        # Make sure the response is not a coroutine
        if asyncio.iscoroutine(response):
            raise RuntimeError("Response is still a coroutine")

        return response

    async def process_exception_by_middleware(self, exception, request):
        """
        Pass the exception to the exception middleware. If no middleware
        return a response for this exception, raise it.
        """
        for middleware_method in self._exception_middleware:
            response = await middleware_method(request, exception)
            if response:
                return response
        raise


def reset_urlconf(sender, **kwargs):
    """Reset the URLconf after each request is finished."""
    set_urlconf(None)


request_finished.connect(reset_urlconf)
