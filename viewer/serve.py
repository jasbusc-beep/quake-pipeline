"""Local viewer. Serves one page on 127.0.0.1 and reads results from the
private bucket. Nothing here is reachable from another machine.

    python viewer/serve.py      then open http://127.0.0.1:8080
"""

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from quake_pipeline import config, storage, verify  # noqa: E402

logger = logging.getLogger("viewer")

HOST = "127.0.0.1"
PORT = 8080
WEB_DIR = Path(__file__).resolve().parent / "web"

# The only files this server will ever send. Requests are looked up in this
# dict, so no part of a request path is ever joined onto a file path.
STATIC_ROUTES = {
    "/": (WEB_DIR / "index.html", "text/html; charset=utf-8"),
    "/app.js": (WEB_DIR / "app.js", "text/javascript; charset=utf-8"),
    "/style.css": (WEB_DIR / "style.css", "text/css; charset=utf-8"),
    "/vendor/chart.umd.js": (
        WEB_DIR / "vendor" / "chart.umd.js",
        "text/javascript; charset=utf-8",
    ),
}
JSON_TYPE = "application/json; charset=utf-8"

# API routes that take a run id. The id must match the strict run id
# pattern before it is used for anything.
RUN_PREFIX = "/api/runs/"
VERIFY_PREFIX = "/api/verify/"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _dump(model) -> dict:
    return model.model_dump(mode="json")


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "QuakeViewer"
    sys_version = ""

    # --- responses ---------------------------------------------------------

    def _send(self, status: int, content_type: str, body: bytes, extra=None):
        self.send_response(status)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict, extra=None):
        self._send(status, JSON_TYPE, json.dumps(payload).encode("utf-8"), extra)

    def _not_found(self):
        self._send_json(404, {"error": "not_found"})

    # --- request checks ----------------------------------------------------

    def _host_allowed(self) -> bool:
        """Only answer requests addressed to this machine by name.

        A hostile website can point its own domain name at 127.0.0.1 to get
        a browser to talk to local servers (DNS rebinding). Those requests
        arrive with that site's name in the Host header, so they stop here.
        """
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return self.headers.get("Host", "") in allowed

    # --- routing -----------------------------------------------------------

    def do_GET(self):
        if not self._host_allowed():
            self._send_json(403, {"error": "forbidden_host"})
            return
        path = self.path.split("?", 1)[0]
        if path in STATIC_ROUTES:
            file_path, content_type = STATIC_ROUTES[path]
            self._send(200, content_type, file_path.read_bytes())
        elif path == "/api/latest":
            self._api(lambda: _dump_or_none(storage.latest_processed()), "no_result")
        elif path == "/api/runs":
            self._api(lambda: {"runs": storage.list_run_ids()}, "no_result")
        elif path.startswith(RUN_PREFIX):
            self._api_for_run(path[len(RUN_PREFIX):], storage.read_processed)
        elif path.startswith(VERIFY_PREFIX):
            self._api_for_run(path[len(VERIFY_PREFIX):], verify.verify_run)
        else:
            self._not_found()

    def _api_for_run(self, run_id: str, load):
        if not config.RUN_ID_RE.fullmatch(run_id):
            self._not_found()
            return
        self._api(lambda: _dump_or_none(load(run_id)), "no_such_run")

    def _api(self, produce, missing_error: str):
        """Run produce() and send its result as JSON.

        None becomes a 404. Any failure becomes a generic 500: details go
        to the server log only, never to the browser.
        """
        try:
            payload = produce()
        except Exception:
            logger.exception("API request failed")
            self._send_json(500, {"error": "internal_error"})
            return
        if payload is None:
            self._send_json(404, {"error": missing_error})
        else:
            self._send_json(200, payload)

    def _method_not_allowed(self):
        self._send_json(405, {"error": "method_not_allowed"}, {"Allow": "GET"})

    do_HEAD = do_POST = do_PUT = do_PATCH = _method_not_allowed
    do_DELETE = do_OPTIONS = _method_not_allowed

    def send_error(self, code, message=None, explain=None):
        """Any other error (odd methods, bad requests) gets the same headers."""
        if code == 501:  # a method this server does not implement
            self._method_not_allowed()
        else:
            self._send_json(code, {"error": "bad_request"})


def _dump_or_none(model):
    return None if model is None else _dump(model)


def make_server(host: str = HOST, port: int = PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), ViewerHandler)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config.get_bucket_name()  # fail fast with a clear message
    except RuntimeError as err:
        sys.exit(f"{err}\nTo look without a cloud account: python tools/demo.py")
    server = make_server()
    print(f"Viewer running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
