"""HTTP servers on 127.0.0.1, and the reference API's JSON shapes."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

# A placeholder for the server's origin in a served page.
ORIGIN = "__ORIGIN__"

# A request's path and query.
Asked = tuple[str, dict[str, list[str]]]


@contextmanager
def serving(*payloads: dict) -> Iterator[tuple[str, list[Asked]]]:
    """Serve one JSON payload per request, repeating the last, and record each request."""
    asked: list[Asked] = []
    queue = list(payloads)

    class Handler(_Handler):
        def do_GET(self):
            parts = urlsplit(self.path)
            asked.append((parts.path, parse_qs(parts.query)))
            self.reply(json.dumps(queue.pop(0) if len(queue) > 1 else queue[0]))

    with _serve(Handler) as origin:
        yield origin, asked


@contextmanager
def serving_pages(
    pages: dict[str, str], failing: dict[str, int] | None = None
) -> Iterator[tuple[str, list[str]]]:
    """Serve pages by path, and record each path asked for."""
    asked: list[str] = []
    broken = failing or {}
    served: dict[str, str] = {}

    class Handler(_Handler):
        def do_GET(self):
            path = urlsplit(self.path).path
            asked.append(path)
            if path in broken:
                self.send_error(broken[path])
            elif path not in served:
                self.send_error(404)
            else:
                self.reply(served[path])

    with _serve(Handler) as origin:
        served.update({path: body.replace(ORIGIN, origin) for path, body in pages.items()})
        yield origin, asked


def node(node_type: str, site_id: str, availability: str) -> dict:
    return {
        "node_type": node_type,
        "site_id": site_id,
        "availability": availability,
        "architecture": {"platform_type": "x86_64", "smt_size": 8},
        "main_memory": {"ram_size": 137438953472},
        "storage_devices": [{"size": 240000000000}],
        "gpu": {"gpu": False},
    }


def page(*nodes: dict) -> dict:
    return {"total": len(nodes), "offset": 0, "items": list(nodes)}


class _Handler(BaseHTTPRequestHandler):
    def reply(self, body: str) -> None:
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
