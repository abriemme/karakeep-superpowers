"""YouTube Data API access: playlists, playlist items, video details.

OAuth2 with a long-lived refresh token (the n8n version used the same grant
via its credential store): the access token is refreshed lazily and cached per
worker process. Errors propagate as exceptions; the calling activity's
``RetryPolicy`` decides whether to retry.
"""

from __future__ import annotations

import time
import urllib.parse

import requests

from app.config import (
    YOUTUBE_CLIENT_ID,
    YOUTUBE_CLIENT_SECRET,
    YOUTUBE_REFRESH_TOKEN,
    YT_MAX_PLAYLIST_PAGES,
)
from app.youtube.models import PlaylistInfo, VideoDetails, VideoItem

API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 30

# Titles the playlistItems endpoint returns for tombstoned entries; they carry
# no metadata and their watch URL is dead, so they are dropped at fetch time.
TOMBSTONE_TITLES = {"Deleted video", "Private video", ""}

# Pause between two pages of one playlist (the n8n node used 300 ms).
PAGE_INTERVAL = 0.3

# Access token cache, per worker process.
_token: str | None = None
_token_expiry: float = 0.0


def _access_token() -> str:
    """Return a valid access token, refreshing it when (nearly) expired."""
    global _token, _token_expiry
    if _token and time.time() < _token_expiry - 60:
        return _token
    r = requests.post(
        TOKEN_URL,
        data={
            "client_id": YOUTUBE_CLIENT_ID,
            "client_secret": YOUTUBE_CLIENT_SECRET,
            "refresh_token": YOUTUBE_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    _token = body["access_token"]
    _token_expiry = time.time() + float(body.get("expires_in", 3600))
    return _token


def _get(path: str, params: dict) -> dict:
    r = requests.get(
        f"{API}/{path}",
        params=params,
        headers={"Authorization": f"Bearer {_access_token()}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _paginate(path: str, params: dict, max_pages: int, interval: float = 0.0):
    """Yield the pages of a YouTube list endpoint, following ``nextPageToken``."""
    token = ""
    for page in range(max_pages):
        if page and interval:
            time.sleep(interval)
        query = dict(params)
        if token:
            query["pageToken"] = token
        body = _get(path, query)
        yield body
        token = body.get("nextPageToken") or ""
        if not token:
            return


def fetch_playlists() -> list[PlaylistInfo]:
    """Return the account's playlists with their change signature inputs."""
    playlists: list[PlaylistInfo] = []
    pages = _paginate(
        "playlists",
        {"mine": "true", "part": "contentDetails,snippet", "maxResults": 50},
        max_pages=10,
    )
    for body in pages:
        for item in body.get("items", []):
            if not item.get("id"):
                continue
            playlists.append(
                PlaylistInfo(
                    id=item["id"],
                    title=(item.get("snippet") or {}).get("title", ""),
                    etag=item.get("etag", ""),
                    item_count=int(
                        (item.get("contentDetails") or {}).get("itemCount") or 0
                    ),
                )
            )
    return playlists


def fetch_playlist_items(playlist_id: str) -> list[VideoItem]:
    """Return the videos of one playlist (tombstoned entries dropped)."""
    videos: list[VideoItem] = []
    pages = _paginate(
        "playlistItems",
        {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        },
        max_pages=YT_MAX_PLAYLIST_PAGES,
        interval=PAGE_INTERVAL,
    )
    for body in pages:
        for item in body.get("items", []):
            video_id = (item.get("contentDetails") or {}).get("videoId")
            if not video_id:
                continue
            sn = item.get("snippet") or {}
            title = sn.get("title", "")
            if title in TOMBSTONE_TITLES:
                continue
            videos.append(
                VideoItem(
                    video_id=video_id,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=title,
                    channel=sn.get("videoOwnerChannelTitle", ""),
                    description=(sn.get("description") or "")[:600],
                    published_at=sn.get("publishedAt", ""),
                )
            )
    return videos


def _decode_topics(topic_urls: list[str]) -> list[str]:
    """Wikipedia topic URLs -> human labels ("Rock_music" -> "Rock music")."""
    topics = []
    for url in topic_urls:
        last = str(url).split("/")[-1]
        if last:
            topics.append(urllib.parse.unquote(last).replace("_", " "))
    return topics


def fetch_video_details(ids: list[str]) -> dict[str, VideoDetails]:
    """Return duration/tags/topics/description for up to 50 video ids."""
    if not ids:
        return {}
    body = _get(
        "videos",
        {
            "part": "snippet,contentDetails,topicDetails",
            "id": ",".join(ids),
            "maxResults": 50,
        },
    )
    details: dict[str, VideoDetails] = {}
    for item in body.get("items", []):
        sn = item.get("snippet") or {}
        details[item["id"]] = VideoDetails(
            duration=(item.get("contentDetails") or {}).get("duration", ""),
            tags=(sn.get("tags") or [])[:15],
            topics=_decode_topics(
                (item.get("topicDetails") or {}).get("topicCategories") or []
            ),
            description=(sn.get("description") or "")[:1200],
        )
    return details
