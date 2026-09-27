"""Sync workflow: YouTube playlists -> Karakeep bookmarks.

The n8n original hand-rolled its durability: playlist signatures in
``$getWorkflowStaticData``, pagination caps as node settings, batch pacing as
node options. Here the orchestration is explicit and replayable: the diff and
the full-sweep decision are deterministic workflow code, the I/O lives in
activities, and pacing is ``workflow.sleep`` timers.

Failure model: playlist signatures are only persisted at the end of a fully
processed run, and dedup is against Karakeep itself (video ids extracted from
existing bookmarks), so a crashed or half-finished run re-scans some playlists
but never duplicates a bookmark.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.youtube.activities import (
        fetch_existing_video_ids,
        fetch_playlist_items,
        fetch_playlists,
        fetch_video_details,
        load_playlist_state,
        push_video,
        save_playlist_state,
    )
    from app.youtube.models import (
        PlaylistState,
        PushVideoOutcome,
        PushVideoParams,
        VideoDetails,
        VideoItem,
        YtSyncInput,
        YtSyncSummary,
    )

# Pacing between two Karakeep pushes and between two playlist fetches (the n8n
# version batched 5 bookmarks per 2 s). Must be deterministic.
PUSH_DELAY = timedelta(seconds=2)
PLAYLIST_DELAY = timedelta(seconds=2)

# videos.list accepts at most 50 ids per call.
DETAILS_BATCH = 50

_SHORT_RETRY = RetryPolicy(maximum_attempts=3)


def _sweep_due(last_full_sweep: str, hours: float, now: datetime) -> bool:
    """True when the periodic full sweep (re-scan everything) is due."""
    if not last_full_sweep:
        return True
    try:
        last = datetime.fromisoformat(last_full_sweep)
    except ValueError:
        return True
    return now - last > timedelta(hours=hours)


@workflow.defn
class YtSyncWorkflow:
    @workflow.run
    async def run(self, params: YtSyncInput) -> YtSyncSummary:
        state: PlaylistState = await workflow.execute_activity(
            load_playlist_state,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_SHORT_RETRY,
        )

        playlists = await workflow.execute_activity(
            fetch_playlists,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_SHORT_RETRY,
        )

        summary = YtSyncSummary(status="ok", playlists=len(playlists))
        now = workflow.now()
        force = params.full_sweep or _sweep_due(
            state.last_full_sweep, params.full_sweep_hours, now
        )

        # Signatures are rebuilt from the current playlists only, so deleted
        # playlists are pruned from the state for free.
        new_signatures: dict[str, str] = {}
        changed = []
        for playlist in playlists:
            signature = f"{playlist.etag}:{playlist.item_count}"
            new_signatures[playlist.id] = signature
            if force or state.signatures.get(playlist.id) != signature:
                changed.append(playlist)
        summary.changed = len(changed)

        videos: list[VideoItem] = []
        for index, playlist in enumerate(changed):
            if index:
                await workflow.sleep(PLAYLIST_DELAY)
            items = await workflow.execute_activity(
                fetch_playlist_items,
                playlist.id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_SHORT_RETRY,
            )
            videos.extend(items)
        summary.fetched = len(videos)

        if videos:
            existing = set(
                await workflow.execute_activity(
                    fetch_existing_video_ids,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_SHORT_RETRY,
                )
            )

            # Diff: drop already-bookmarked videos and cross-playlist dupes.
            seen: set[str] = set()
            new_videos: list[VideoItem] = []
            for video in videos:
                if video.video_id in existing or video.video_id in seen:
                    continue
                seen.add(video.video_id)
                new_videos.append(video)
            new_videos = new_videos[: params.max_videos]
            summary.new = len(new_videos)

            details: dict[str, VideoDetails] = {}
            ids = [v.video_id for v in new_videos]
            for start in range(0, len(ids), DETAILS_BATCH):
                batch = await workflow.execute_activity(
                    fetch_video_details,
                    ids[start : start + DETAILS_BATCH],
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=_SHORT_RETRY,
                )
                details.update(batch)

            for video in new_videos:
                extra = details.get(video.video_id)
                if extra:
                    video.duration = extra.duration
                    video.tags = extra.tags
                    video.topics = extra.topics
                    video.description = extra.description or video.description
                outcome: PushVideoOutcome = await workflow.execute_activity(
                    push_video,
                    PushVideoParams(video=video),
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=_SHORT_RETRY,
                )
                if outcome.status == "imported":
                    summary.imported += 1
                    if outcome.theme:
                        summary.themes[outcome.theme] = (
                            summary.themes.get(outcome.theme, 0) + 1
                        )
                elif outcome.status == "skipped":
                    summary.skipped += 1
                else:
                    summary.failed += 1
                await workflow.sleep(PUSH_DELAY)

        await workflow.execute_activity(
            save_playlist_state,
            PlaylistState(
                signatures=new_signatures,
                last_full_sweep=now.isoformat() if force else state.last_full_sweep,
            ),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_SHORT_RETRY,
        )
        return summary
