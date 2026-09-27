"""Tests for the YouTube playlists -> Karakeep workflow (time-skipping).

The real activities (YouTube / Karakeep HTTP calls, LLM) are replaced by
fakes registered under the same activity names: the workflow logic under test
is the orchestration (skip-unchanged signatures, full sweep, dedup, details
merge, pacing, state persistence), not the network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.config import TASK_QUEUE
from app.youtube import YtSyncInput, YtSyncWorkflow
from tests.conftest import new_workflow_id


@dataclass
class YtMocks:
    playlists: list = field(default_factory=list)
    items: dict[str, list] = field(default_factory=dict)
    existing: list[str] = field(default_factory=list)
    details: dict[str, dict] = field(default_factory=dict)
    push_results: list[dict] = field(default_factory=list)
    state: dict = field(default_factory=lambda: {"signatures": {}, "last_full_sweep": ""})
    saved_states: list[dict] = field(default_factory=list)
    pushed: list = field(default_factory=list)
    items_calls: list[str] = field(default_factory=list)
    existing_calls: int = 0


def _make_activities(mocks: YtMocks) -> list:
    @activity.defn(name="load_playlist_state")
    async def load_playlist_state() -> dict:
        return mocks.state

    @activity.defn(name="save_playlist_state")
    async def save_playlist_state(new_state) -> None:
        mocks.saved_states.append(new_state)

    @activity.defn(name="fetch_playlists")
    async def fetch_playlists() -> list:
        return mocks.playlists

    @activity.defn(name="fetch_playlist_items")
    async def fetch_playlist_items(playlist_id: str) -> list:
        mocks.items_calls.append(playlist_id)
        return mocks.items.get(playlist_id, [])

    @activity.defn(name="fetch_existing_video_ids")
    async def fetch_existing_video_ids() -> list[str]:
        mocks.existing_calls += 1
        return mocks.existing

    @activity.defn(name="fetch_video_details")
    async def fetch_video_details(ids: list[str]) -> dict:
        return {vid: mocks.details[vid] for vid in ids if vid in mocks.details}

    @activity.defn(name="push_video")
    async def push_video(params) -> dict:
        mocks.pushed.append(params["video"])
        if mocks.push_results:
            return mocks.push_results.pop(0)
        return {"status": "imported", "theme": ""}

    return [
        load_playlist_state,
        save_playlist_state,
        fetch_playlists,
        fetch_playlist_items,
        fetch_existing_video_ids,
        fetch_video_details,
        push_video,
    ]


def _playlist(pid: str, etag: str = "e", count: int = 1) -> dict:
    return {"id": pid, "title": f"Playlist {pid}", "etag": etag, "item_count": count}


def _video(vid: str) -> dict:
    return {
        "video_id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": f"Video {vid}",
    }


async def _run(env: WorkflowEnvironment, mocks: YtMocks, **input_kwargs):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[YtSyncWorkflow],
        activities=_make_activities(mocks),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        return await env.client.execute_workflow(
            YtSyncWorkflow.run,
            YtSyncInput(**input_kwargs),
            id=new_workflow_id("yt-sync"),
            task_queue=TASK_QUEUE,
        )


@pytest.mark.asyncio
async def test_imports_new_videos_and_saves_signatures(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    mocks = YtMocks(
        playlists=[_playlist("p1", etag="e1", count=3)],
        items={"p1": [_video("v1"), _video("v2"), _video("v3")]},
        existing=["v2"],  # already bookmarked in Karakeep
    )

    result = await _run(time_skipping_env, mocks)

    assert [v["video_id"] for v in mocks.pushed] == ["v1", "v3"]
    assert result.status == "ok"
    assert result.playlists == 1
    assert result.changed == 1
    assert result.fetched == 3
    assert result.new == 2
    assert result.imported == 2
    # The playlist signature and the full-sweep timestamp are persisted.
    assert mocks.saved_states[-1]["signatures"] == {"p1": "e1:3"}
    assert mocks.saved_states[-1]["last_full_sweep"]


@pytest.mark.asyncio
async def test_unchanged_playlists_are_skipped(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    """A playlist whose etag/itemCount signature did not move is not re-fetched."""
    mocks = YtMocks(
        playlists=[_playlist("p1", etag="e1", count=3)],
        state={
            "signatures": {"p1": "e1:3"},
            "last_full_sweep": datetime.now(UTC).isoformat(),
        },
    )

    result = await _run(time_skipping_env, mocks)

    assert mocks.items_calls == []
    assert mocks.existing_calls == 0  # no videos, no dedup index needed
    assert mocks.pushed == []
    assert result.changed == 0
    # State is still saved (prunes deleted playlists, keeps the sweep clock).
    assert mocks.saved_states[-1]["signatures"] == {"p1": "e1:3"}


@pytest.mark.asyncio
async def test_full_sweep_rescans_unchanged_playlists(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    mocks = YtMocks(
        playlists=[_playlist("p1", etag="e1", count=1)],
        items={"p1": [_video("v1")]},
        state={
            "signatures": {"p1": "e1:1"},  # unchanged...
            "last_full_sweep": datetime.now(UTC).isoformat(),
        },
    )

    result = await _run(time_skipping_env, mocks, full_sweep=True)

    assert mocks.items_calls == ["p1"]
    assert result.imported == 1
    # A forced sweep refreshes the sweep timestamp.
    assert mocks.saved_states[-1]["last_full_sweep"] != mocks.state["last_full_sweep"]


@pytest.mark.asyncio
async def test_dedup_across_playlists_and_max_videos_cap(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    """The same video in two playlists is pushed once; the cap bounds the run."""
    mocks = YtMocks(
        playlists=[_playlist("p1"), _playlist("p2")],
        items={
            "p1": [_video("v1"), _video("v2")],
            "p2": [_video("v1"), _video("v3")],
        },
    )

    result = await _run(time_skipping_env, mocks, max_videos=2)

    assert [v["video_id"] for v in mocks.pushed] == ["v1", "v2"]
    assert result.fetched == 4
    assert result.new == 2


@pytest.mark.asyncio
async def test_outcomes_are_counted_with_theme_breakdown(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    mocks = YtMocks(
        playlists=[_playlist("p1")],
        items={"p1": [_video("v1"), _video("v2"), _video("v3")]},
        push_results=[
            {"status": "imported", "theme": "Tech & Homelab"},
            {"status": "failed", "theme": ""},
            {"status": "skipped", "theme": ""},
        ],
    )

    result = await _run(time_skipping_env, mocks)

    assert result.imported == 1
    assert result.failed == 1
    assert result.skipped == 1
    assert result.themes == {"Tech & Homelab": 1}


@pytest.mark.asyncio
async def test_video_details_are_merged_before_push(
    time_skipping_env: WorkflowEnvironment,
) -> None:
    mocks = YtMocks(
        playlists=[_playlist("p1")],
        items={"p1": [_video("v1")]},
        details={
            "v1": {
                "duration": "PT12M34S",
                "tags": ["homelab"],
                "topics": ["Technology"],
                "description": "long description",
            }
        },
    )

    await _run(time_skipping_env, mocks)

    pushed = mocks.pushed[0]
    assert pushed["duration"] == "PT12M34S"
    assert pushed["tags"] == ["homelab"]
    assert pushed["topics"] == ["Technology"]
    assert pushed["description"] == "long description"
