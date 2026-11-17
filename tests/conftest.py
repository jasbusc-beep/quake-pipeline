"""Shared test setup. No network, no real GCS, no real model."""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from google.api_core.exceptions import NotFound

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "viewer"))

# Must be set before quake_pipeline is imported (it validates the bucket).
os.environ["GCS_BUCKET"] = "test-bucket"

from quake_pipeline import nodes, storage  # noqa: E402
from quake_pipeline.schemas import PipelineRequest  # noqa: E402

# --- fake Google Cloud Storage ---------------------------------------------


class FakeBlob:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self.client.upload_calls.append(
            {
                "name": self.name,
                "content_type": content_type,
                "if_generation_match": if_generation_match,
            }
        )
        if if_generation_match == 0 and self.name in self.client.objects:
            raise RuntimeError("precondition failed: object exists")
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.client.objects[self.name] = data

    def download_as_bytes(self):
        if self.name not in self.client.objects:
            raise NotFound(f"No such object: {self.name}")
        return self.client.objects[self.name]


class FakeBucket:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def blob(self, name):
        return FakeBlob(self.client, name)


class FakeClient:
    def __init__(self):
        self.objects = {}
        self.upload_calls = []
        self.bucket_names = []

    def bucket(self, name):
        self.bucket_names.append(name)
        return FakeBucket(self, name)

    def list_blobs(self, bucket_name, prefix=None):
        self.bucket_names.append(bucket_name)
        return [
            FakeBlob(self, name)
            for name in sorted(self.objects)
            if name.startswith(prefix or "")
        ]


FROZEN_NOW = datetime(2026, 11, 17, 5, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin the pipeline's clock so freshness checks do not depend on today."""
    monkeypatch.setattr(nodes, "_utc_now", lambda: FROZEN_NOW)


@pytest.fixture
def fake_gcs(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(storage, "_get_client", lambda: client)
    return client


@pytest.fixture
def set_transport(monkeypatch):
    """Route the pipeline's HTTP client through a mock transport."""

    def _set(transport):
        monkeypatch.setattr(nodes, "HTTP_TRANSPORT", transport)

    return _set


def json_response(body: bytes, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body, headers={"content-type": "application/json"}
    )


@pytest.fixture
def ingest_feed(fake_gcs, set_transport):
    """Run the real ingest step on a given feed (default: the fixture feed)."""

    def _run(collection=None, **request):
        body = json.dumps(collection or feature_collection()).encode("utf-8")
        set_transport(httpx.MockTransport(lambda r: json_response(body)))
        return nodes.run_ingest(PipelineRequest(**request))

    return _run


def collection_of(features: list) -> dict:
    return {
        "type": "FeatureCollection",
        "metadata": {"generated": ms(17, 5), "count": len(features)},
        "features": features,
    }


# --- fixture earthquake feed -----------------------------------------------


def ms(day: int, hour: int = 12) -> int:
    return int(datetime(2026, 11, day, hour, tzinfo=UTC).timestamp() * 1000)


def feature(fid, mag, place, time_ms, depth, event_type="earthquake"):
    return {
        "type": "Feature",
        "id": fid,
        "properties": {"mag": mag, "place": place, "time": time_ms, "type": event_type},
        "geometry": {"type": "Point", "coordinates": [-150.0, 61.0, depth]},
    }


def feature_collection() -> dict:
    features = [
        feature("a", 5.0, "10 km S of Town, Alaska", ms(10), 10),
        feature("b", 4.6, "Somewhere, Alaska", ms(10, 18), 20),
        feature("c", 6.1, "5 km N of X, Japan", ms(11), 30.555),
        feature("d", 3.0, "CA", ms(11), 5),
        feature("e", None, "Nowhere, Chile", ms(11), 5),
        feature("f", 5.5, "Mine, Utah", ms(11), 0, event_type="quarry blast"),
        feature("g", 2.0, "Y, Nevada", ms(12), None),
        {"type": "Feature", "id": "broken", "properties": {"mag": 4.0}},
    ]
    return collection_of(features)


@pytest.fixture
def feed_bytes() -> bytes:
    return json.dumps(feature_collection()).encode("utf-8")


@pytest.fixture
def publish_run(ingest_feed):
    """Run ingest, transform and publish for real. Returns the stored result."""

    def _run(summary="Three earthquakes matched.", collection=None, **request):
        request = request or {"min_magnitude": 4.5}
        ingested = ingest_feed(collection, **request)
        _, processed = nodes.run_transform(ingested)
        result, _ = nodes.run_publish(summary, processed)
        return result

    return _run


def store_copy(fake_gcs, result, run_id: str) -> None:
    """Store a copy of a result (and its raw file) under another run id."""
    raw = fake_gcs.objects[result.lineage.raw_object]
    lineage = result.lineage.model_copy(update={"raw_object": f"raw/{run_id}.geojson"})
    copy = result.model_copy(update={"run_id": run_id, "lineage": lineage})
    fake_gcs.objects[f"raw/{run_id}.geojson"] = raw
    fake_gcs.objects[f"processed/{run_id}.json"] = copy.model_dump_json().encode()
