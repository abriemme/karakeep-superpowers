"""(Re)generate the JSON histories used by the replay test.

We run each workflow on the test server (time-skipping) with stub activities,
then export the full history as JSON into ``tests/histories/``.

Usage::

    python scripts/generate_history.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.config import TASK_QUEUE
from app.sync import IgSyncWorkflow, SyncInput
from app.youtube import YtSyncInput, YtSyncWorkflow

HISTORIES_DIR = Path(__file__).resolve().parents[1] / "tests" / "histories"


# --- Instagram stubs ----------------------------------------------------------


@activity.defn(name="fetch_saved_page")
async def fetch_saved_page(params) -> dict:
    return {
        "items": [
            {"pk": "2", "code": "c2", "username": "a", "caption": "new"},
            {"pk": "1", "code": "c1", "username": "a", "caption": "old"},
        ],
        "next_cursor": "",
    }


@activity.defn(name="load_seen")
async def load_seen() -> list[str]:
    return []


@activity.defn(name="save_seen")
async def save_seen(seen: list[str]) -> None:
    pass


@activity.defn(name="push_to_karakeep")
async def push_to_karakeep(params) -> dict:
    return {"status": "imported", "assets": {}, "lists": []}


# --- YouTube stubs -------------------------------------------------------------


@activity.defn(name="load_playlist_state")
async def load_playlist_state() -> dict:
    return {"signatures": {}, "last_full_sweep": ""}


@activity.defn(name="save_playlist_state")
async def save_playlist_state(state) -> None:
    pass


@activity.defn(name="fetch_playlists")
async def fetch_playlists() -> list:
    return [{"id": "p1", "title": "Liked", "etag": "e1", "item_count": 2}]


@activity.defn(name="fetch_playlist_items")
async def fetch_playlist_items(playlist_id: str) -> list:
    return [
        {
            "video_id": "abcdefghijk",
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "title": "A video",
        },
        {
            "video_id": "ABCDEFGHIJ1",
            "url": "https://www.youtube.com/watch?v=ABCDEFGHIJ1",
            "title": "Another video",
        },
    ]


@activity.defn(name="fetch_existing_video_ids")
async def fetch_existing_video_ids() -> list[str]:
    return ["ABCDEFGHIJ1"]


@activity.defn(name="fetch_video_details")
async def fetch_video_details(ids: list[str]) -> dict:
    return {vid: {"duration": "PT1M", "tags": [], "topics": []} for vid in ids}


@activity.defn(name="push_video")
async def push_video(params) -> dict:
    return {"status": "imported", "theme": ""}


async def _export(env: WorkflowEnvironment, run, filename: str) -> None:
    handle = await run
    await handle.result()
    history = await handle.fetch_history()
    HISTORIES_DIR.mkdir(parents=True, exist_ok=True)
    out = HISTORIES_DIR / filename
    out.write_text(json.dumps(history.to_json_dict(), indent=2, sort_keys=True))
    print(f"wrote {out.relative_to(Path.cwd())}")


async def main() -> None:
    env = await WorkflowEnvironment.start_time_skipping()
    try:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[IgSyncWorkflow, YtSyncWorkflow],
            activities=[
                fetch_saved_page,
                load_seen,
                save_seen,
                push_to_karakeep,
                load_playlist_state,
                save_playlist_state,
                fetch_playlists,
                fetch_playlist_items,
                fetch_existing_video_ids,
                fetch_video_details,
                push_video,
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await _export(
                env,
                env.client.start_workflow(
                    IgSyncWorkflow.run,
                    SyncInput(),
                    id="ig-sync-1",
                    task_queue=TASK_QUEUE,
                ),
                "ig_sync_workflow.json",
            )
            await _export(
                env,
                env.client.start_workflow(
                    YtSyncWorkflow.run,
                    YtSyncInput(),
                    id="yt-sync-1",
                    task_queue=TASK_QUEUE,
                ),
                "yt_sync_workflow.json",
            )
    finally:
        await env.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
