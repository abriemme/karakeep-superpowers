"""YouTube sync activities: thin Temporal wrappers around the service layer.

All I/O lives in app.youtube.youtube / karakeep / state; these decorators add
the activity contract (logging, error propagation to the retry policy) and
the per-video classify-then-push orchestration.
"""

from __future__ import annotations

from temporalio import activity

from app.config import KARAKEEP_YOUTUBE_LIST_ID, YT_EXCLUDED_LIST_IDS
from app.youtube import karakeep, state, youtube
from app.youtube.classify import classify_video
from app.youtube.models import (
    PlaylistInfo,
    PlaylistState,
    PushVideoOutcome,
    PushVideoParams,
    VideoDetails,
    VideoItem,
)


@activity.defn
async def fetch_playlists() -> list[PlaylistInfo]:
    """List the account's playlists (id + change signature)."""
    return youtube.fetch_playlists()


@activity.defn
async def fetch_playlist_items(playlist_id: str) -> list[VideoItem]:
    """Fetch the videos of one playlist (paginated inside the activity)."""
    return youtube.fetch_playlist_items(playlist_id)


@activity.defn
async def fetch_existing_video_ids() -> list[str]:
    """Video ids already bookmarked in Karakeep (dedup index, one per run)."""
    return sorted(karakeep.existing_video_ids())


@activity.defn
async def fetch_video_details(ids: list[str]) -> dict[str, VideoDetails]:
    """Batch-fetch duration/tags/topics for up to 50 videos."""
    return youtube.fetch_video_details(ids)


def _theme_candidates() -> list[dict]:
    """Lists offered to the classifier: every manual list except the umbrella
    YouTube list and the explicitly excluded ones."""
    excluded = set(YT_EXCLUDED_LIST_IDS)
    if KARAKEEP_YOUTUBE_LIST_ID:
        excluded.add(KARAKEEP_YOUTUBE_LIST_ID)
    return [item for item in karakeep.fetch_lists() if item["id"] not in excluded]


@activity.defn
async def push_video(params: PushVideoParams) -> PushVideoOutcome:
    """Classify the video's theme, then create + route the bookmark.

    Only url + title are sent: Karakeep crawls the page itself (description,
    thumbnail, yt-dlp archive, its own AI tagging). Unlike the Instagram
    enrichment, the LLM output here is optional routing metadata, so a
    classification failure degrades to "no theme" instead of blocking the
    import (transient provider errors were already absorbed upstream by the
    agent's own retries within this attempt).
    """
    video = params.video

    theme: tuple[str, str] | None = None
    try:
        theme = await classify_video(video, _theme_candidates())
    except Exception:
        activity.logger.warning(
            "Theme classification failed for %s; importing without a theme",
            video.video_id,
            exc_info=True,
        )

    bookmark_id, already_exists = karakeep.create_bookmark(video.url, video.title)
    if bookmark_id is None:
        activity.logger.error("Karakeep push failed for %s", video.url)
        return PushVideoOutcome(status="failed")
    if already_exists:
        # The diff missed it (e.g. a youtu.be bookmark): leave it untouched.
        activity.logger.info("= %s", video.url)
        return PushVideoOutcome(status="skipped")

    karakeep.tag_bookmark(bookmark_id, ["youtube"])
    if KARAKEEP_YOUTUBE_LIST_ID:
        karakeep.add_to_list(bookmark_id, KARAKEEP_YOUTUBE_LIST_ID)
    theme_name = ""
    if theme:
        theme_id, theme_name = theme
        if not karakeep.add_to_list(bookmark_id, theme_id):
            theme_name = ""
    activity.logger.info("+ %s", video.url)
    return PushVideoOutcome(status="imported", theme=theme_name)


@activity.defn
async def load_playlist_state() -> PlaylistState:
    return state.load_playlist_state()


@activity.defn
async def save_playlist_state(new_state: PlaylistState) -> None:
    state.save_playlist_state(new_state)
