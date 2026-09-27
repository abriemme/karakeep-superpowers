"""Karakeep access for the YouTube sync, via the ``karakeep-python-api`` client.

The client (generated from Karakeep's OpenAPI spec) owns the HTTP layer:
auth, error typing (network failures included, wrapped as ``APIError``), and
optional rate limiting. This module keeps only the sync-specific logic:
video-id extraction, search pagination, smart-list filtering and the outcome
mapping the activities rely on.

Deliberately lean compared to the Instagram service: Karakeep crawls YouTube
pages itself (description, thumbnail, yt-dlp archive, its own AI tagging), so
the bookmark is created with url + title only — no assets, no note.
"""

from __future__ import annotations

import re

from karakeep_python_api import APIError, KarakeepAPI

from app.config import KARAKEEP_TOKEN, KARAKEEP_URL, YT_MAX_BOOKMARK_PAGES

# Existing YouTube URLs sometimes carry extra parameters (`&t=123s`), so dedup
# extracts the 11-character video id instead of comparing raw URLs.
VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})")

# Client and lists are stable within a run; cached per worker process so
# every video sees the same candidates.
_client: KarakeepAPI | None = None
_lists_cache: list[dict] | None = None


def client() -> KarakeepAPI:
    """API client, cached per worker process.

    Response validation is disabled on purpose: the POST /bookmarks answer
    carries an ``alreadyExists`` flag that is not part of the client's
    ``Bookmark`` model and would be silently dropped by validation — and the
    sync only reads a handful of fields anyway.
    """
    global _client
    if _client is None:
        _client = KarakeepAPI(
            api_key=KARAKEEP_TOKEN,
            api_endpoint=f"{KARAKEEP_URL}/api/v1/",
            disable_response_validation=True,
        )
    return _client


def extract_video_id(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def existing_video_ids() -> set[str]:
    """Video ids already bookmarked, from a paginated Karakeep search."""
    ids: set[str] = set()
    cursor: str | None = None
    for _ in range(YT_MAX_BOOKMARK_PAGES):
        page = client().search_bookmarks(
            q="url:youtube.com/watch", limit=100, cursor=cursor
        )
        for bm in page.get("bookmarks", []):
            video_id = extract_video_id((bm.get("content") or {}).get("url", ""))
            if video_id:
                ids.add(video_id)
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return ids


def fetch_lists(*, force: bool = False) -> list[dict]:
    """Manual lists as ``[{"id", "name", "description"}]``, cached per process.

    Smart (query-based) lists are dropped: bookmarks cannot be added to them,
    so offering them to the classifier would only waste a slot.
    """
    global _lists_cache
    if _lists_cache is not None and not force:
        return _lists_cache
    payload = client().get_all_lists()
    raw = payload.get("lists", []) if isinstance(payload, dict) else payload
    _lists_cache = [
        {
            "id": item["id"],
            "name": item["name"],
            "description": item.get("description") or "",
        }
        for item in raw
        if item.get("type", "manual") == "manual"
    ]
    return _lists_cache


def create_bookmark(url: str, title: str) -> tuple[str | None, bool]:
    """Create a link bookmark; return ``(bookmark_id, already_existed)``.

    Karakeep dedups by URL itself and answers with the existing bookmark
    (``alreadyExists``) instead of a duplicate — the belt to the video-id
    diff's braces.
    """
    try:
        body = client().create_a_new_bookmark(type="link", url=url, title=title)
    except APIError:
        return None, False
    return body.get("id"), bool(body.get("alreadyExists"))


def tag_bookmark(bookmark_id: str, tags: list[str]) -> bool:
    try:
        client().attach_tags_to_a_bookmark(bookmark_id, tag_names=tags)
    except APIError:
        return False
    return True


def add_to_list(bookmark_id: str, list_id: str) -> bool:
    try:
        client().add_a_bookmark_to_a_list(list_id=list_id, bookmark_id=bookmark_id)
    except APIError:
        return False
    return True
