"""Karakeep access for the YouTube sync: dedup index, lists, bookmark push.

Deliberately lean compared to the Instagram service: Karakeep crawls YouTube
pages itself (description, thumbnail, yt-dlp archive, its own AI tagging), so
the bookmark is created with url + title only — no assets, no note.
"""

from __future__ import annotations

import re

import requests

from app.config import KARAKEEP_TOKEN, KARAKEEP_URL, YT_MAX_BOOKMARK_PAGES

TIMEOUT = 30

# Existing YouTube URLs sometimes carry extra parameters (`&t=123s`), so dedup
# extracts the 11-character video id instead of comparing raw URLs.
VIDEO_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})")

# Karakeep lists are stable within a run; fetched once per worker process and
# reused so every video sees the same candidates.
_lists_cache: list[dict] | None = None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {KARAKEEP_TOKEN}",
        "Content-Type": "application/json",
    }


def extract_video_id(url: str) -> str | None:
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def existing_video_ids() -> set[str]:
    """Video ids already bookmarked, from a paginated Karakeep search."""
    ids: set[str] = set()
    cursor = ""
    for _ in range(YT_MAX_BOOKMARK_PAGES):
        params: dict = {"q": "url:youtube.com/watch", "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(
            f"{KARAKEEP_URL}/api/v1/bookmarks/search",
            params=params,
            headers=_headers(),
            timeout=60,
        )
        r.raise_for_status()
        payload = r.json()
        for bm in payload.get("bookmarks", []):
            video_id = extract_video_id((bm.get("content") or {}).get("url", ""))
            if video_id:
                ids.add(video_id)
        cursor = payload.get("nextCursor") or ""
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
    r = requests.get(
        f"{KARAKEEP_URL}/api/v1/lists",
        headers=_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
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
        r = requests.post(
            f"{KARAKEEP_URL}/api/v1/bookmarks",
            json={"type": "link", "url": url, "title": title},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return None, False
    if r.status_code not in (200, 201):
        return None, False
    body = r.json()
    return body.get("id"), bool(body.get("alreadyExists"))


def tag_bookmark(bookmark_id: str, tags: list[str]) -> bool:
    try:
        r = requests.post(
            f"{KARAKEEP_URL}/api/v1/bookmarks/{bookmark_id}/tags",
            json={"tags": [{"tagName": t} for t in tags]},
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return False
    return r.status_code in (200, 201)


def add_to_list(bookmark_id: str, list_id: str) -> bool:
    try:
        r = requests.put(
            f"{KARAKEEP_URL}/api/v1/lists/{list_id}/bookmarks/{bookmark_id}",
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return False
    return r.status_code in (200, 204)
