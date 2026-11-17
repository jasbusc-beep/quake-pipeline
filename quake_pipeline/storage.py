"""Google Cloud Storage helpers.

Uses Application Default Credentials (your gcloud login). There are no key
files and no credentials in code. Objects are only ever created and read:
never overwritten, deleted or made public.

Every object name is built here from a fixed prefix and a run id that has
been checked against a strict pattern. Callers cannot pass a free-form path.
"""

import os
import re

import google.auth
from google.api_core.exceptions import NotFound
from google.cloud import storage as gcs

from . import config
from .schemas import ProcessedResult

_RAW_NAME_RE = re.compile(rf"^raw/{config.RUN_ID_PATTERN}\.geojson$")
_PROCESSED_NAME_RE = re.compile(rf"^processed/({config.RUN_ID_PATTERN})\.json$")

_client = None


def _get_client():
    """Build the storage client once, from Application Default Credentials.

    The project is taken from the login itself. Letting the library guess
    would make it shell out to the gcloud program, which may not be on the
    path. Reading and writing objects only needs the bucket name anyway.
    """
    global _client
    if _client is None:
        credentials, project = google.auth.default()
        project = (
            project
            or getattr(credentials, "quota_project_id", None)
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
        )
        if not project:
            raise RuntimeError(
                "Could not tell which Google Cloud project to use. Add "
                "GOOGLE_CLOUD_PROJECT=your-project-id to quake_pipeline/.env."
            )
        _client = gcs.Client(project=project, credentials=credentials)
    return _client


def _bucket():
    return _get_client().bucket(config.get_bucket_name())


def _check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not config.RUN_ID_RE.fullmatch(run_id):
        raise ValueError("Invalid run id")
    return run_id


def raw_object_name(run_id: str) -> str:
    return f"{config.RAW_PREFIX}{_check_run_id(run_id)}.geojson"


def processed_object_name(run_id: str) -> str:
    return f"{config.PROCESSED_PREFIX}{_check_run_id(run_id)}.json"


def upload(name: str, data: bytes | str, content_type: str) -> None:
    """Create a new object. Fails if an object with that name exists."""
    if not (_RAW_NAME_RE.fullmatch(name) or _PROCESSED_NAME_RE.fullmatch(name)):
        raise ValueError("Object name is not one this pipeline may write")
    blob = _bucket().blob(name)
    # if_generation_match=0 means "only if the object does not exist yet".
    blob.upload_from_string(data, content_type=content_type, if_generation_match=0)


def _download(name: str) -> bytes | None:
    try:
        return _bucket().blob(name).download_as_bytes()
    except NotFound:
        return None


def list_run_ids(limit: int = config.MAX_RUNS_LISTED) -> list[str]:
    """Run ids that have a processed result, newest first."""
    blobs = _get_client().list_blobs(
        config.get_bucket_name(), prefix=config.PROCESSED_PREFIX
    )
    matches = (_PROCESSED_NAME_RE.fullmatch(blob.name) for blob in blobs)
    # Run ids start with a UTC timestamp, so sorting by name sorts by time.
    run_ids = sorted((m.group(1) for m in matches if m), reverse=True)
    return run_ids[:limit]


def read_processed(run_id: str) -> ProcessedResult | None:
    """Load and validate one processed result, or None if it does not exist."""
    data = _download(processed_object_name(run_id))
    return None if data is None else ProcessedResult.model_validate_json(data)


def read_raw(run_id: str) -> bytes | None:
    """Load the untouched API response for a run, or None if it is missing."""
    return _download(raw_object_name(run_id))


def latest_processed() -> ProcessedResult | None:
    """Return the newest processed result, or None if there is none yet."""
    run_ids = list_run_ids(limit=1)
    return read_processed(run_ids[0]) if run_ids else None
