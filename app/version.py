"""One release identifier for the bot, worker, health endpoints and audit data."""
from pathlib import Path
import os

VERSION = "4.4.0"
_build_file = Path(__file__).with_name("build_commit.txt")
COMMIT = (_build_file.read_text().strip() if _build_file.exists() else os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"))
PREDICTION_VERSION = "4.4-observation-confirmations"
