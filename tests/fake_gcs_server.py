"""A tiny in-process stand-in for the Cloud Storage JSON API.

It speaks just enough HTTP for the REAL google-cloud-storage client to
upload, list and download. That lets the tests prove what our code actually
puts on the wire (for example ifGenerationMatch=0), instead of trusting a
hand-written fake of the client.
"""

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import google_crc32c


def _hashes(data: bytes) -> tuple[str, str]:
    crc = base64.b64encode(google_crc32c.Checksum(data).digest()).decode()
    md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
    return crc, md5


class FakeGcsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._json(status, {"error": {"code": status, "message": message}})

    def _resource(self, bucket: str, name: str) -> dict:
        obj = self.server.objects[name]
        crc, md5 = _hashes(obj["data"])
        return {
            "kind": "storage#object",
            "bucket": bucket,
            "name": name,
            "generation": "1",
            "metageneration": "1",
            "size": str(len(obj["data"])),
            "contentType": obj["content_type"],
            "crc32c": crc,
            "md5Hash": md5,
        }

    def do_POST(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        self.server.requests.append(("POST", url.path, query))
        bucket = url.path.split("/b/")[1].split("/")[0]
        body = self.rfile.read(int(self.headers["Content-Length"]))

        boundary = self.headers["Content-Type"].split("boundary=")[1].strip('"')
        parts = body.split(b"--" + boundary.encode())[1:-1]
        meta_part, data_part = (part.split(b"\r\n\r\n", 1) for part in parts)
        metadata = json.loads(meta_part[1])
        data = data_part[1][:-2]  # drop the trailing CRLF before the boundary
        content_type = data_part[0].split(b":", 1)[1].strip().decode()

        name = metadata["name"]
        if query.get("ifGenerationMatch") == ["0"] and name in self.server.objects:
            self._error(412, "Precondition Failed: object already exists")
            return
        self.server.objects[name] = {"data": data, "content_type": content_type}
        self._json(200, self._resource(bucket, name))

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        self.server.requests.append(("GET", url.path, query))
        bucket = url.path.split("/b/")[1].split("/")[0]

        if url.path.endswith("/o"):  # list objects
            prefix = query.get("prefix", [""])[0]
            items = [
                self._resource(bucket, name)
                for name in sorted(self.server.objects)
                if name.startswith(prefix)
            ]
            self._json(200, {"kind": "storage#objects", "items": items})
            return

        if "/o/" not in url.path:
            # Bucket settings. The pipeline's least-privilege roles cannot
            # read these, so answer the way real GCS would: 403.
            self._error(403, "storage.buckets.get denied")
            return

        name = unquote(url.path.split("/o/", 1)[1])
        if name not in self.server.objects:
            self._error(404, f"No such object: {bucket}/{name}")
            return
        if query.get("alt") == ["media"]:  # download
            data = self.server.objects[name]["data"]
            crc, md5 = _hashes(data)
            self.send_response(200)
            self.send_header("Content-Type", self.server.objects[name]["content_type"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Goog-Hash", f"crc32c={crc},md5={md5}")
            self.send_header("X-Goog-Generation", "1")
            self.send_header("X-Goog-Stored-Content-Encoding", "identity")
            self.end_headers()
            self.wfile.write(data)
        else:  # metadata
            self._json(200, self._resource(bucket, name))


class FakeGcsServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeGcsHandler)
        self.httpd.objects = {}
        self.httpd.requests = []
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )

    @property
    def objects(self) -> dict:
        return self.httpd.objects

    @property
    def requests(self) -> list:
        return self.httpd.requests

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
