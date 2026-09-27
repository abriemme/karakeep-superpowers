"""Configuration shared between the worker, the workflows and the tests.

The ``build_id`` is derived from the git SHA of the current commit. In CI, the
``GIT_SHA`` environment variable is injected by GitHub Actions (``github.sha``).
Locally, we fall back to ``git rev-parse HEAD``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

# Load a local ``.env`` (if present) before reading any variable below, so
# `uv run python -m app.worker` / `app.starter` pick it up without an explicit
# `export`. Existing environment variables win (``override=False``), so the
# shell and CI (``GIT_SHA``, etc.) stay authoritative; in Docker/prod, pass the
# variables through the environment (e.g. ``--env-file``) instead of shipping a
# ``.env`` in the image.
load_dotenv()

# Logical name of the worker deployment. It must stay stable over time: it is
# the identity under which all versions (build_ids) succeed one another.
DEPLOYMENT_NAME = "ig-to-karakeep"

# Task queue used by the worker and the clients.
TASK_QUEUE = "ig-sync-task-queue"

# Temporal server address (overridden in prod via the environment).
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")

# --- Sync (Instagram -> Karakeep) configuration ------------------------------

KARAKEEP_URL = os.environ.get("KARAKEEP_URL", "").rstrip("/")
KARAKEEP_TOKEN = os.environ.get("KARAKEEP_TOKEN", "")
KARAKEEP_LIST_ID = os.environ.get("KARAKEEP_LIST_ID", "").strip()

# How many of the most recent saved posts to examine per run; dedup does the
# rest. Set backfill=True on the workflow input to fetch the whole history.
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "40"))

# Guard-rail on a backfill: at most this many pages are walked in one run.
# The cursor lives in the (durable) workflow state, so a run that hits the cap
# resumes from where it stopped on the next execution.
MAX_PAGES = int(os.environ.get("MAX_PAGES", "25"))

# instagrapi session and dedup state. On the NUC (Docker) this is /data.
DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
SESSION_FILE = DATA_DIR / "session.json"
STATE_FILE = DATA_DIR / "seen.json"
COLLECTION_CACHE_FILE = DATA_DIR / "collections.json"

# --- Assets (Instagram CDN -> Karakeep) --------------------------------------
#
# Which assets to upload alongside the bookmark. Instagram CDN URLs are signed
# and expire, so they are only recoverable at import time.
#   banner     post thumbnail as bannerImage
#   screenshot post thumbnail as screenshot (replaces Karakeep's login-wall grab)
#   carousel   every slide of a multi-image post, as userUploaded
#   video      the Reel's video file
#   avatar     the author's profile picture
ASSET_TYPES = [
    t.strip().lower()
    for t in os.environ.get("ASSET_TYPES", "banner,screenshot,carousel").split(",")
    if t.strip()
]

# Max slides pulled from a carousel; a 20-image post is 20 downloads + uploads.
MAX_CAROUSEL = int(os.environ.get("MAX_CAROUSEL", "10"))

# Per-file ceiling (MB): above this the asset is skipped rather than saved.
MAX_ASSET_MB = float(os.environ.get("MAX_ASSET_MB", "50"))

# Overwrite the banner produced by Karakeep's crawler (a login wall on an
# Instagram URL) with the post thumbnail. Set to False to keep the existing one.
REPLACE_BANNER = os.environ.get("REPLACE_BANNER", "1") == "1"

# --- Deterministic tags ------------------------------------------------------
#
# Tags derived from metadata without an LLM. Comma-separated among:
# hashtags, author, type, music, location. Empty to disable.
AUTO_TAGS = [
    t.strip()
    for t in os.environ.get("AUTO_TAGS", "hashtags,author,type,music,location").split(",")
    if t.strip()
]

# Cap on tags per bookmark (deterministic + LLM tags merged). Instagram posts
# routinely carry 20-30 hashtags: without a cap the Karakeep tag cloud rots.
MAX_TAGS = int(os.environ.get("MAX_TAGS", "12"))

# Above this hashtag count the caption is SEO noise, not a description: the
# hashtags are dropped rather than imported as tags.
HASHTAG_SPAM_THRESHOLD = int(os.environ.get("HASHTAG_SPAM_THRESHOLD", "20"))

# --- Collections -> Karakeep lists -------------------------------------------
#
# Route Instagram collections to Karakeep lists of the same name.
SYNC_COLLECTIONS = os.environ.get("SYNC_COLLECTIONS", "1") == "1"

# Create the missing Karakeep list when a collection has no match. Off by
# default: a warning beats a tree polluted with lists created at the root.
CREATE_MISSING_LISTS = os.environ.get("CREATE_MISSING_LISTS", "0") == "1"

# Posts scanned per collection when membership is not inlined in the feed.
COLLECTION_SCAN = int(os.environ.get("COLLECTION_SCAN", "60"))

# How long the collection-membership map stays valid (hours). It costs one
# private-API call PER collection to rebuild, yet barely ever changes. 0 = off.
COLLECTION_CACHE_HOURS = float(os.environ.get("COLLECTION_CACHE_HOURS", "24"))


# --- YouTube playlists -> Karakeep --------------------------------------------
#
# OAuth2 "installed app" credentials with a long-lived refresh token. The
# YouTube Data API requires OAuth (an API key is not enough) to list *your*
# playlists (`mine=true`).
YOUTUBE_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REFRESH_TOKEN = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")

# Karakeep list receiving every imported video (the umbrella "YouTube" list).
# Empty to skip. Always excluded from the theme classifier's candidates.
KARAKEEP_YOUTUBE_LIST_ID = os.environ.get("KARAKEEP_YOUTUBE_LIST_ID", "").strip()

# Extra list ids never offered to the theme classifier (inbox/archive-style
# lists that would attract everything).
YT_EXCLUDED_LIST_IDS = {
    t.strip() for t in os.environ.get("YT_EXCLUDED_LIST_IDS", "").split(",") if t.strip()
}

# Pagination guard-rails. A playlist page is 50 videos; a Karakeep search page
# is 100 bookmarks. The playlist cap also bounds the activity-result payload
# (Temporal caps a payload at 2 MB).
YT_MAX_PLAYLIST_PAGES = int(os.environ.get("YT_MAX_PLAYLIST_PAGES", "20"))
YT_MAX_BOOKMARK_PAGES = int(os.environ.get("YT_MAX_BOOKMARK_PAGES", "80"))

# Cap on new videos bookmarked per run. The rest is picked up on a later run:
# dedup is against Karakeep itself, so nothing is ever lost.
YT_MAX_VIDEOS = int(os.environ.get("YT_MAX_VIDEOS", "200"))

# Hours between two full sweeps that re-scan every playlist regardless of its
# signature (safety net for the skip-unchanged optimisation).
YT_FULL_SWEEP_HOURS = float(os.environ.get("YT_FULL_SWEEP_HOURS", "24"))

# Playlist etag/itemCount signatures (skip-unchanged optimisation).
PLAYLIST_STATE_FILE = DATA_DIR / "playlists.json"


def get_build_id() -> str:
    """Return the git SHA to use as the ``build_id`` of the worker version.

    Resolution order:
    1. ``GIT_SHA`` (injected by CI);
    2. ``git rev-parse HEAD`` locally;
    3. ``"unknown"`` as a last resort (no repo available).
    """
    sha = os.environ.get("GIT_SHA")
    if sha:
        return sha.strip()

    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return sha.decode().strip()
    except subprocess.CalledProcessError, FileNotFoundError:
        return "unknown"
