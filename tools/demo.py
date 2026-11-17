"""Demo mode: see the report with no cloud account and no AI model.

    python tools/demo.py        then open http://127.0.0.1:8080

It runs the real ingest, transform, publish and verify code against live
USGS data, but keeps the files in memory instead of a bucket, and skips the
two AI steps (the request is fixed and the summary is the built-in fallback
sentence). Nothing is saved anywhere and everything is gone when you stop it.

This is for a quick look. The real pipeline is: adk run quake_pipeline "..."
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "viewer"))

# Demo mode needs no real bucket. This name is never contacted.
os.environ.setdefault("GCS_BUCKET", "demo-mode-no-bucket")

from google.api_core.exceptions import NotFound, PreconditionFailed  # noqa: E402

import serve  # noqa: E402
from quake_pipeline import nodes, storage  # noqa: E402
from quake_pipeline.schemas import PipelineRequest  # noqa: E402

DEMO_REQUESTS = [
    PipelineRequest(timeframe="month", min_magnitude=4.5, group_by="region"),
    PipelineRequest(timeframe="week", min_magnitude=2.5, group_by="magnitude_band"),
    PipelineRequest(timeframe="week", min_magnitude=4.5, group_by="day"),
]


class MemoryBlob:
    def __init__(self, objects: dict, name: str):
        self.objects, self.name = objects, name

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        if if_generation_match == 0 and self.name in self.objects:
            raise PreconditionFailed(f"{self.name} already exists")
        self.objects[self.name] = data.encode("utf-8") if isinstance(data, str) else data

    def download_as_bytes(self) -> bytes:
        if self.name not in self.objects:
            raise NotFound(self.name)
        return self.objects[self.name]


class MemoryClient:
    """Just enough of the storage client for demo mode: create, list, read."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def bucket(self, name):
        return self

    def blob(self, name):
        return MemoryBlob(self.objects, name)

    def list_blobs(self, bucket_name, prefix=""):
        return [MemoryBlob(self.objects, n) for n in sorted(self.objects) if n.startswith(prefix)]


def build_demo_store(requests=DEMO_REQUESTS) -> MemoryClient:
    client = MemoryClient()
    storage._client = client
    for request in requests:
        ingested = nodes.run_ingest(request)
        _, processed = nodes.run_transform(ingested)
        nodes.run_publish("", processed)  # no model in demo mode: fallback summary
    return client


def main():
    print("Demo mode: fetching live USGS data (no cloud, no AI model)...")
    client = build_demo_store()
    print(f"Built {len(DEMO_REQUESTS)} demo runs in memory ({len(client.objects)} files).")
    server = serve.make_server()
    print(f"Viewer running at http://{serve.HOST}:{serve.PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
