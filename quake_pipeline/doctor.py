"""Check that this machine is ready to run the pipeline.

    python -m quake_pipeline.doctor

Each line says PASS or FAIL and, on a failure, what to do about it. It reads
settings and makes two read-only requests (list the bucket, fetch a tiny USGS
feed). It never writes anything and never prints a secret.
"""

import os
import sys

import google.auth
import httpx
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

from . import config, nodes, storage


def check_python() -> tuple[bool, str]:
    version = sys.version_info
    found = f"Python {version.major}.{version.minor}"
    if version >= (3, 11):
        return True, found
    return False, f"{found} is too old. ADK 2.x needs Python 3.11 or newer."


def check_bucket_setting() -> tuple[bool, str]:
    try:
        return True, f"GCS_BUCKET is set to {config.get_bucket_name()}"
    except RuntimeError as err:
        return False, str(err)


def check_model_access() -> tuple[bool, str]:
    if os.environ.get("GOOGLE_API_KEY"):
        return True, "GOOGLE_API_KEY is set (Gemini API key path)"
    cloud = ("GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")
    if all(os.environ.get(name) for name in cloud):
        return True, "Google Cloud model settings are set (no API key path)"
    return False, (
        "No model access configured. Put GOOGLE_API_KEY in quake_pipeline/.env, "
        "or set all three of: " + ", ".join(cloud) + "."
    )


def check_google_login() -> tuple[bool, str]:
    try:
        google.auth.default()
    except GoogleAuthError:
        return False, (
            "No Google login found. Run: gcloud auth application-default login"
        )
    return True, "Google login found (Application Default Credentials)"


def check_bucket_access() -> tuple[bool, str]:
    try:
        runs = storage.list_run_ids(limit=1)
    except (GoogleAPIError, GoogleAuthError, RuntimeError, OSError) as err:
        return False, (
            f"Could not read the bucket ({type(err).__name__}). Check the bucket "
            "exists and your account has the two roles from the README."
        )
    newest = f"newest run {runs[0]}" if runs else "no runs stored yet"
    return True, f"Bucket is readable ({newest})"


def check_usgs() -> tuple[bool, str]:
    url = nodes.build_feed_url("4.5", "hour")
    try:
        features, _ = nodes.parse_feed(nodes.fetch_feed(url))
    except (httpx.HTTPError, nodes.TransientUpstreamError, ValueError) as err:
        return False, f"Could not reach the USGS feed ({type(err).__name__})."
    return True, f"USGS feed is reachable ({len(features)} events in the past hour)"


CHECKS = [
    ("Python version", check_python),
    ("Bucket setting", check_bucket_setting),
    ("AI model access", check_model_access),
    ("Google login", check_google_login),
    ("Bucket access", check_bucket_access),
    ("USGS data source", check_usgs),
]


def main() -> int:
    failures = 0
    for name, check in CHECKS:
        ok, detail = check()
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print("Ready to run." if not failures else f"{failures} thing(s) to fix first.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
