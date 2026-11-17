"""Settings, allow-lists and limits.

Everything security-relevant here is a constant. Neither the user nor the
model can change the host, the URL shape, the size cap or the object names.
"""

import os
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ENV_FILE = PACKAGE_DIR / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load KEY=VALUE lines from the local .env file into the environment.

    `adk run` already does this. The viewer does not, so we do it here too.
    Real environment variables always win over the file.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_env_file()

# --- Settings read from the environment -----------------------------------

GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"

BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


def get_bucket_name() -> str:
    """Return the validated bucket name, or stop with a clear error."""
    name = os.environ.get("GCS_BUCKET", "")
    if not name:
        raise RuntimeError(
            "GCS_BUCKET is not set. Copy .env.example to quake_pipeline/.env "
            "and fill in your bucket name."
        )
    if not BUCKET_NAME_RE.fullmatch(name):
        raise RuntimeError(
            "GCS_BUCKET is not a valid bucket name (lowercase letters, "
            "digits, dots, dashes and underscores, 3 to 63 characters)."
        )
    return name


# --- Constants (not configurable by the user or the model) ----------------

USGS_HOST = "earthquake.usgs.gov"
USGS_URL_TEMPLATE = (
    "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
    "{level}_{timeframe}.geojson"
)
ALLOWED_TIMEFRAMES = {"hour", "day", "week", "month"}
ALLOWED_LEVELS = {"all", "1.0", "2.5", "4.5"}

HTTP_TIMEOUT_SECONDS = 10  # per network operation
MAX_DOWNLOAD_SECONDS = 30  # whole download, so a slow drip cannot hang a run
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
USER_AGENT = "quake-pipeline/1.0 (class project; ADK data pipeline)"

MAX_SUMMARY_CHARS = 600
MAX_LABEL_CHARS = 80
TOP_GROUPS = 15

# Example run id: 20261117T051500Z-3fa9c2d1
RUN_ID_PATTERN = r"\d{8}T\d{6}Z-[0-9a-f]{8}"
RUN_ID_RE = re.compile(rf"^{RUN_ID_PATTERN}$")

RAW_PREFIX = "raw/"
PROCESSED_PREFIX = "processed/"

SOURCE_NAME = "USGS Earthquake Hazards Program"
PIPELINE_VERSION = "1.0.0"

# Plausible ranges for one record. Anything outside is treated as bad data.
MAG_RANGE = (-5.0, 12.0)
DEPTH_KM_RANGE = (-100.0, 1000.0)
TIME_MS_MAX = 4_102_444_800_000  # 2100-01-01 UTC
MAX_ID_CHARS = 64
MAX_PLACE_CHARS = 300
MAX_EVENT_TYPE_CHARS = 40

# Data quality gates
INVALID_RATIO_WARN = 0.01  # more than 1% unreadable records: warn
INVALID_RATIO_FAIL = 0.20  # more than 20%: the feed format changed, stop
FEED_STALE_SECONDS = 3600  # USGS rebuilds feeds every few minutes

MAX_RUNS_LISTED = 20  # run history shown in the viewer
