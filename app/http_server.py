"""HTTP server primitives for the local TeleRoute WSGI runtime."""
from __future__ import annotations

from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Serve independent WSGI requests in separate threads."""

    daemon_threads = True
    multithread = True


def make_threading_server(host: str, port: int, application):
    """Build the threaded server used by both local application entry points."""
    return make_server(
        host,
        port,
        application,
        server_class=ThreadingWSGIServer,
        handler_class=WSGIRequestHandler,
    )
