"""Tests for the ``push_video`` activity orchestration.

The service layer is stubbed; under test is the classify-then-push contract:
theme routing, the graceful degradation when classification fails, the
already-exists short-circuit, and the candidate-list exclusions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from temporalio.testing import ActivityEnvironment

import app.youtube.activities as activities
from app.youtube.models import PushVideoParams, VideoItem

VIDEO = VideoItem(
    video_id="abcdefghijk",
    url="https://www.youtube.com/watch?v=abcdefghijk",
    title="A video",
)

LISTS = [
    {"id": "yt-list", "name": "YouTube", "description": ""},
    {"id": "inbox", "name": "Inbox", "description": ""},
    {"id": "l1", "name": "Tech & Homelab", "description": ""},
]


@dataclass
class KarakeepStub:
    create_result: tuple = ("b1", False)
    add_ok: bool = True
    tagged: list = field(default_factory=list)
    added_to: list = field(default_factory=list)

    def fetch_lists(self):
        return LISTS

    def create_bookmark(self, url, title):
        return self.create_result

    def tag_bookmark(self, bookmark_id, tags):
        self.tagged.append((bookmark_id, tags))
        return True

    def add_to_list(self, bookmark_id, list_id):
        self.added_to.append((bookmark_id, list_id))
        return self.add_ok


@pytest.fixture
def stub(monkeypatch) -> KarakeepStub:
    stub = KarakeepStub()
    monkeypatch.setattr(activities, "karakeep", stub)
    monkeypatch.setattr(activities, "KARAKEEP_YOUTUBE_LIST_ID", "yt-list")
    monkeypatch.setattr(activities, "YT_EXCLUDED_LIST_IDS", {"inbox"})
    return stub


def _classifier(result):
    async def classify(video, candidates, agent=None):
        _classifier.candidates = candidates
        return result

    return classify


@pytest.mark.asyncio
async def test_push_routes_to_youtube_and_theme_lists(stub, monkeypatch) -> None:
    monkeypatch.setattr(
        activities, "classify_video", _classifier(("l1", "Tech & Homelab"))
    )

    outcome = await ActivityEnvironment().run(
        activities.push_video, PushVideoParams(video=VIDEO)
    )

    assert outcome.status == "imported"
    assert outcome.theme == "Tech & Homelab"
    assert stub.tagged == [("b1", ["youtube"])]
    assert stub.added_to == [("b1", "yt-list"), ("b1", "l1")]
    # The umbrella and excluded lists are never offered to the classifier.
    assert [c["id"] for c in _classifier.candidates] == ["l1"]


@pytest.mark.asyncio
async def test_push_degrades_to_no_theme_when_classification_fails(
    stub, monkeypatch
) -> None:
    """The LLM only picks an optional theme: its failure must not block imports."""

    async def failing(video, candidates, agent=None):
        raise RuntimeError("LLM provider down")

    monkeypatch.setattr(activities, "classify_video", failing)

    outcome = await ActivityEnvironment().run(
        activities.push_video, PushVideoParams(video=VIDEO)
    )

    assert outcome.status == "imported"
    assert outcome.theme == ""
    assert stub.added_to == [("b1", "yt-list")]


@pytest.mark.asyncio
async def test_push_skips_when_karakeep_already_knows_the_url(stub, monkeypatch) -> None:
    monkeypatch.setattr(activities, "classify_video", _classifier(None))
    stub.create_result = ("b1", True)

    outcome = await ActivityEnvironment().run(
        activities.push_video, PushVideoParams(video=VIDEO)
    )

    assert outcome.status == "skipped"
    assert stub.tagged == []
    assert stub.added_to == []


@pytest.mark.asyncio
async def test_push_reports_failure_when_creation_is_rejected(stub, monkeypatch) -> None:
    monkeypatch.setattr(activities, "classify_video", _classifier(None))
    stub.create_result = (None, False)

    outcome = await ActivityEnvironment().run(
        activities.push_video, PushVideoParams(video=VIDEO)
    )

    assert outcome.status == "failed"


@pytest.mark.asyncio
async def test_push_drops_theme_when_list_add_fails(stub, monkeypatch) -> None:
    monkeypatch.setattr(
        activities, "classify_video", _classifier(("l1", "Tech & Homelab"))
    )
    stub.add_ok = False

    outcome = await ActivityEnvironment().run(
        activities.push_video, PushVideoParams(video=VIDEO)
    )

    # Imported, but the summary must not claim a theme that was not applied.
    assert outcome.status == "imported"
    assert outcome.theme == ""
