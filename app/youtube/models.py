"""Shared data structures for the YouTube playlists sync.

Kept free of any I/O or Temporal decorator so both the workflow sandbox and
the activity side can import them safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import YT_FULL_SWEEP_HOURS, YT_MAX_VIDEOS


@dataclass
class YtSyncInput:
    """Input of ``YtSyncWorkflow``."""

    # Re-scan every playlist regardless of its signature (same effect as the
    # periodic full sweep, on demand).
    full_sweep: bool = False
    max_videos: int = YT_MAX_VIDEOS
    full_sweep_hours: float = YT_FULL_SWEEP_HOURS


@dataclass
class PlaylistInfo:
    """One playlist of the account, as returned by ``fetch_playlists``.

    ``etag`` + ``item_count`` form the change signature: when neither moved
    since the previous run, the playlist's items are not re-fetched.
    """

    id: str
    title: str = ""
    etag: str = ""
    item_count: int = 0


@dataclass
class PlaylistState:
    """Persisted skip-unchanged state (``playlists.json``)."""

    # playlist id -> "etag:item_count" signature seen on the last scan.
    signatures: dict[str, str] = field(default_factory=dict)
    # ISO-8601 timestamp of the last full sweep ("" = never swept).
    last_full_sweep: str = ""


@dataclass
class VideoItem:
    """One playlist video, flattened for serialization.

    ``duration``, ``tags`` and ``topics`` start empty and are filled from the
    videos endpoint (``fetch_video_details``) before classification.
    """

    video_id: str
    url: str
    title: str
    channel: str = ""
    description: str = ""
    published_at: str = ""
    duration: str = ""  # ISO-8601 (e.g. "PT12M34S"), as YouTube returns it
    tags: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


@dataclass
class VideoDetails:
    """Extra metadata for one video (videos endpoint), keyed by video id."""

    duration: str = ""
    tags: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ThemeChoice:
    """Structured LLM output: the single thematic list a video belongs to.

    ``list_name`` is copied verbatim from the offered candidates, or "" when
    no list clearly fits (the model must never force a classification).
    """

    list_name: str = ""


@dataclass
class PushVideoParams:
    """Input of the ``push_video`` activity."""

    video: VideoItem


@dataclass
class PushVideoOutcome:
    """Result of ``push_video``.

    ``status`` is one of ``imported`` (new bookmark), ``skipped`` (Karakeep
    already knew the URL) or ``failed`` (creation rejected). ``theme`` is the
    name of the thematic list the bookmark was routed to ("" when none).
    """

    status: str
    theme: str = ""


@dataclass
class YtSyncSummary:
    """Result of ``YtSyncWorkflow``."""

    status: str  # always "ok" for now; mirrors SyncSummary's shape
    playlists: int = 0
    changed: int = 0
    fetched: int = 0
    new: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    themes: dict[str, int] = field(default_factory=dict)
