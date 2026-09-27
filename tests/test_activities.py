"""Unit tests for the sync service layer (state, karakeep, instagram).

Service functions are tested directly (no worker, no workflow); activity-level
behavior (challenge wrapping, heartbeats, cancellation) is tested through
``ActivityEnvironment``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from karakeep_python_api import APIError
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

import app.sync.activities as activities
import app.sync.instagram as instagram_svc
import app.sync.karakeep as karakeep_svc
import app.sync.state as state_svc
from app.sync.activities import fetch_saved_page, push_to_karakeep
from app.sync.enrich import TITLE_MAX
from app.sync.facts import NOTE_MAX
from app.sync.instagram import get_client, is_challenge_error
from app.sync.karakeep import build_payload, create_bookmark, fetch_lists
from app.sync.models import (
    CHALLENGE_ERROR_TYPE,
    EnrichedBookmark,
    FetchPageParams,
    MediaItem,
    PushOutcome,
    PushParams,
)
from app.sync.state import load_seen, save_seen

MEDIA = MediaItem(pk="1", code="abc", username="someone", caption="line1\nline2")
ENRICHMENT = EnrichedBookmark(title="A recipe", note="Pancakes", tags=["food"])


# --- Seen-state ---------------------------------------------------------------


def test_seen_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(state_svc, "STATE_FILE", tmp_path / "seen.json")

    save_seen(["2", "1"])
    assert load_seen() == ["1", "2"]
    assert json.loads((tmp_path / "seen.json").read_text()) == ["1", "2"]


def test_load_seen_missing_or_corrupt_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(state_svc, "STATE_FILE", tmp_path / "seen.json")

    assert load_seen() == []  # no file yet

    (tmp_path / "seen.json").write_text("not json")
    assert load_seen() == []  # unreadable -> start fresh


# --- Karakeep -----------------------------------------------------------------


def test_build_payload_from_enrichment() -> None:
    enrichment = EnrichedBookmark(
        title="A great recipe", note="Pancakes, step by step", tags=["Food", " Baking "]
    )
    media = MediaItem(
        pk="1",
        code="abc",
        username="someone",
        caption="",
        taken_at="2024-05-01T10:00:00Z",
    )
    payload = build_payload(media, enrichment)
    assert payload["type"] == "link"
    assert payload["url"] == "https://www.instagram.com/p/abc/"
    assert payload["title"] == "A great recipe"
    assert payload["note"] == "Pancakes, step by step"
    assert payload["tags"] == ["food", "baking"]
    # The publication date becomes createdAt (absent when unknown).
    assert payload["createdAt"] == "2024-05-01T10:00:00Z"
    assert "createdAt" not in build_payload(MEDIA, enrichment)


def test_fetch_lists_parses_and_caches(monkeypatch) -> None:
    calls = {"n": 0}

    class FakeApi:
        def get_all_lists(self):
            calls["n"] += 1
            return {
                "lists": [
                    {"id": "l1", "name": "Cuisine & Vins", "icon": "🍷"},
                    {"id": "l2", "name": "Voyage"},
                ]
            }

    monkeypatch.setattr(karakeep_svc, "_lists_cache", None)
    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())

    assert fetch_lists() == [
        {"id": "l1", "name": "Cuisine & Vins"},
        {"id": "l2", "name": "Voyage"},
    ]
    fetch_lists()  # second call served from cache, no extra request
    assert calls["n"] == 1


def test_create_bookmark_unreachable_returns_failed(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "_lists_cache", [])

    class FakeApi:
        def create_a_new_bookmark(self, **kwargs):
            raise APIError("down")

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert create_bookmark(MEDIA, ENRICHMENT).status == "failed"


def test_create_bookmark_routes_to_classified_lists(monkeypatch) -> None:
    monkeypatch.setattr(
        karakeep_svc,
        "_lists_cache",
        [{"id": "l1", "name": "Cuisine & Vins"}, {"id": "l2", "name": "Voyage"}],
    )

    adds: list[tuple[str, str]] = []
    tagged: list[list[str]] = []

    class FakeApi:
        def create_a_new_bookmark(self, **kwargs):
            return {"id": "bm-1"}

        def attach_tags_to_a_bookmark(self, bookmark_id, tag_names=None, **kw):
            tagged.append(tag_names)
            return {"attached": []}

        def add_a_bookmark_to_a_list(self, list_id, bookmark_id):
            adds.append((list_id, bookmark_id))

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())

    enrichment = EnrichedBookmark(
        title="t", note="n", tags=["Food"], lists=["Cuisine & Vins"]
    )
    outcome = create_bookmark(MEDIA, enrichment)
    assert outcome.status == "imported"
    assert outcome.lists == ["Cuisine & Vins"]
    # Tags are attached in a second call (POST /bookmarks takes none).
    assert tagged == [["food"]]
    # Only the classified list is targeted (no KARAKEEP_LIST_ID in tests).
    assert adds == [("l1", "bm-1")]


def test_fetch_lists_unreachable_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "_lists_cache", None)

    class FakeApi:
        def get_all_lists(self):
            raise APIError("down")

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert fetch_lists() == []


def test_build_payload_truncates_and_drops_blank_tags() -> None:
    enrichment = EnrichedBookmark(
        title="T" * (TITLE_MAX + 50),
        note="N" * (NOTE_MAX + 100),
        tags=["a", " ", "b", "c", "d"],  # blank dropped; the cap is MAX_TAGS
    )
    payload = build_payload(MEDIA, enrichment)
    assert len(payload["title"]) == TITLE_MAX
    assert len(payload["note"]) == NOTE_MAX
    assert payload["tags"] == ["a", "b", "c", "d"]


def test_create_bookmark_bad_status_returns_failed(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "_lists_cache", [])

    class FakeApi:
        def create_a_new_bookmark(self, **kwargs):
            raise APIError("rejected", status_code=500)

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert create_bookmark(MEDIA, ENRICHMENT).status == "failed"


def test_create_bookmark_appends_default_list_and_dedups(monkeypatch) -> None:
    monkeypatch.setattr(
        karakeep_svc, "_lists_cache", [{"id": "l1", "name": "Cuisine & Vins"}]
    )
    # A configured default list is added to every bookmark, without duplicating
    # a classified list that resolves to the same id.
    monkeypatch.setattr(karakeep_svc, "KARAKEEP_LIST_ID", "l1")

    adds: list[tuple[str, str]] = []

    class FakeApi:
        def create_a_new_bookmark(self, **kwargs):
            return {"id": "bm-9"}

        def add_a_bookmark_to_a_list(self, list_id, bookmark_id):
            adds.append((list_id, bookmark_id))

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())

    enrichment = EnrichedBookmark(title="t", note="n", tags=[], lists=["Cuisine & Vins"])
    assert create_bookmark(MEDIA, enrichment).status == "imported"
    assert adds == [("l1", "bm-9")]  # l1 not added twice


def test_create_bookmark_suppresses_list_add_failure(monkeypatch) -> None:
    """A rejected list add must not fail the (already created) bookmark."""
    monkeypatch.setattr(
        karakeep_svc, "_lists_cache", [{"id": "l1", "name": "Cuisine & Vins"}]
    )

    class FakeApi:
        def create_a_new_bookmark(self, **kwargs):
            return {"id": "bm-1"}

        def add_a_bookmark_to_a_list(self, list_id, bookmark_id):
            raise APIError("rejected", status_code=500)

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())

    enrichment = EnrichedBookmark(title="t", note="n", tags=[], lists=["Cuisine & Vins"])
    assert create_bookmark(MEDIA, enrichment).status == "imported"


def test_create_bookmark_without_id_reports_imported(monkeypatch) -> None:
    """A creation that answers without an id cannot be enriched further."""
    monkeypatch.setattr(karakeep_svc, "_lists_cache", [])

    class FakeApi:
        def create_a_new_bookmark(self, **kwargs):
            return {}

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())

    outcome = create_bookmark(MEDIA, ENRICHMENT)
    assert outcome.status == "imported"
    assert outcome.assets == {} and outcome.lists == []


def test_create_bookmark_survives_tag_attach_failure(monkeypatch) -> None:
    """A failed tag attach must not fail the (already created) bookmark."""
    monkeypatch.setattr(karakeep_svc, "_lists_cache", [])

    class FakeApi:
        def create_a_new_bookmark(self, **kwargs):
            return {"id": "bm-1"}

        def attach_tags_to_a_bookmark(self, bookmark_id, tag_names=None, **kw):
            raise APIError("down")

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())

    assert create_bookmark(MEDIA, ENRICHMENT).status == "imported"


# --- Instagram ----------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        ("ChallengeRequired", True),
        ("login_required: 467", True),
        ("feedback_required", True),
        ("plain network error", False),
    ],
)
def test_is_challenge_error(message: str, expected: bool) -> None:
    assert is_challenge_error(Exception(message)) is expected


class _FakeUser:
    def __init__(self, username: str | None) -> None:
        self.username = username


class _FakeMedia:
    def __init__(self, pk, code, username, caption) -> None:
        self.pk = pk
        self.code = code
        self.user = _FakeUser(username)
        self.caption_text = caption


def test_fetch_saved_page_maps_fields(monkeypatch) -> None:
    """Legacy (chunk-less) instagrapi build: one bulk page, no next cursor."""
    captured = {}

    class FakeClient:
        def collection_medias(self, name, amount):
            captured["name"] = name
            captured["amount"] = amount
            return [
                _FakeMedia(123, "codeA", "alice", "  hello  "),
                _FakeMedia(456, "codeB", None, None),  # missing user/caption
            ]

    monkeypatch.setattr(instagram_svc, "get_client", lambda: FakeClient())
    monkeypatch.setattr(instagram_svc, "_collections_by_media", {})

    result = instagram_svc.fetch_saved_page(cursor="", backfill=False, max_items=7)
    assert captured == {"name": "ALL_MEDIA_AUTO_COLLECTION", "amount": 7}
    assert result.next_cursor == ""
    assert result.items[0] == MediaItem(
        pk="123", code="codeA", username="alice", caption="hello"
    )
    # Missing username falls back to "?"; missing caption becomes "".
    assert result.items[1] == MediaItem(pk="456", code="codeB", username="?", caption="")


def test_fetch_saved_page_backfill_requests_all(monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def collection_medias(self, name, amount):
            captured["amount"] = amount
            return []

    monkeypatch.setattr(instagram_svc, "get_client", lambda: FakeClient())
    instagram_svc.fetch_saved_page(cursor="", backfill=True, max_items=40)
    assert captured["amount"] == 0  # 0 = fetch the whole history


def test_fetch_saved_page_uses_chunk_api_and_returns_cursor(monkeypatch) -> None:
    """A cursor-aware instagrapi build pages one chunk at a time."""
    captured = {}

    class FakeClient:
        def collection_medias_v1_chunk(self, name, max_id=""):
            captured["name"] = name
            captured["max_id"] = max_id
            return [_FakeMedia(789, "codeC", "bob", "hi")], "next-42"

    monkeypatch.setattr(instagram_svc, "get_client", lambda: FakeClient())
    monkeypatch.setattr(instagram_svc, "_collections_by_media", {})

    result = instagram_svc.fetch_saved_page(cursor="cur-1", backfill=True, max_items=40)
    assert captured == {"name": "ALL_MEDIA_AUTO_COLLECTION", "max_id": "cur-1"}
    assert result.next_cursor == "next-42"
    assert [m.pk for m in result.items] == ["789"]


def test_fetch_saved_page_propagates_client_error(monkeypatch) -> None:
    class FakeClient:
        def collection_medias(self, name, amount):
            raise Exception("login_required: 467")

    monkeypatch.setattr(instagram_svc, "get_client", lambda: FakeClient())
    with pytest.raises(Exception, match="467"):
        instagram_svc.fetch_saved_page(cursor="", backfill=False, max_items=40)


def test_get_client_raises_without_instagrapi() -> None:
    # instagrapi lives in the optional "ig" group and is absent from the test
    # env, so get_client should surface a clear RuntimeError rather than a bare
    # ImportError.
    with pytest.raises(RuntimeError):
        get_client()


@pytest.mark.asyncio
async def test_fetch_saved_page_wraps_challenge(monkeypatch) -> None:
    def boom(**kwargs):
        raise Exception("login_required: checkpoint required")

    monkeypatch.setattr(instagram_svc, "fetch_saved_page", boom)

    env = ActivityEnvironment()
    with pytest.raises(ApplicationError) as exc_info:
        await env.run(fetch_saved_page, FetchPageParams())
    assert exc_info.value.type == CHALLENGE_ERROR_TYPE


@pytest.mark.asyncio
async def test_fetch_saved_page_reraises_non_challenge(monkeypatch) -> None:
    """A plain (non-challenge) error is re-raised untyped, so Temporal retries."""

    def boom(**kwargs):
        raise ValueError("transient network blip")

    monkeypatch.setattr(instagram_svc, "fetch_saved_page", boom)

    with pytest.raises(ValueError, match="transient"):
        await ActivityEnvironment().run(fetch_saved_page, FetchPageParams())


# --- ActivityEnvironment mechanics (generic) -----------------------------------


@pytest.mark.asyncio
async def test_activity_heartbeats() -> None:
    """``on_heartbeat`` captures the details passed to ``activity.heartbeat()``."""

    @activity.defn
    async def heartbeating_activity(count: int) -> None:
        for i in range(count):
            activity.heartbeat(i)

    heartbeats: list[int] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: heartbeats.append(args[0])

    await env.run(heartbeating_activity, 3)

    assert heartbeats == [0, 1, 2]


@pytest.mark.asyncio
async def test_activity_cancellation() -> None:
    """``env.cancel()`` propagates cancellation into the running activity."""

    @activity.defn
    async def cancellable_activity() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(0.01)

    env = ActivityEnvironment()

    async def cancel_soon() -> None:
        await asyncio.sleep(0.05)
        env.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(env.run(cancellable_activity), cancel_soon())


# --- Sync activity wrappers ----------------------------------------------------


@pytest.mark.asyncio
async def test_push_to_karakeep_enriches_and_creates(monkeypatch) -> None:
    seen_lists: list[str] = []
    built: list[EnrichedBookmark] = []

    async def fake_enrich(media, list_names):
        seen_lists.extend(list_names)
        return ENRICHMENT

    def fake_create(media, enrichment):
        built.append(enrichment)
        return PushOutcome(status="imported")

    monkeypatch.setattr(
        activities.karakeep, "fetch_lists", lambda: [{"id": "l1", "name": "Voyage"}]
    )
    monkeypatch.setattr(activities, "enrich_media", fake_enrich)
    monkeypatch.setattr(activities.karakeep, "create_bookmark", fake_create)

    outcome = await ActivityEnvironment().run(push_to_karakeep, PushParams(media=MEDIA))
    assert outcome.status == "imported"
    # The classifier is offered the account's real list names.
    assert seen_lists == ["Voyage"]
    # The deterministic note/tags replace the raw LLM output before creation.
    assert built[0].note.startswith("@someone")
    assert built[0].tags[0] == "someone"  # author tag comes first


@pytest.mark.asyncio
async def test_push_to_karakeep_reports_failure(monkeypatch) -> None:
    async def fake_enrich(media, list_names):
        return ENRICHMENT

    monkeypatch.setattr(activities.karakeep, "fetch_lists", lambda: [])
    monkeypatch.setattr(activities, "enrich_media", fake_enrich)
    monkeypatch.setattr(
        activities.karakeep, "create_bookmark", lambda m, e: PushOutcome(status="failed")
    )

    outcome = await ActivityEnvironment().run(push_to_karakeep, PushParams(media=MEDIA))
    assert outcome.status == "failed"


@pytest.mark.asyncio
async def test_push_to_karakeep_reconcile_completes_without_llm(monkeypatch) -> None:
    """Reconcile short-circuits the LLM when the bookmark already exists."""

    async def fail_enrich(media, list_names):  # pragma: no cover - must not run
        raise AssertionError("the LLM must not be called in reconcile mode")

    monkeypatch.setattr(activities, "enrich_media", fail_enrich)
    monkeypatch.setattr(
        activities.karakeep, "find_existing_bookmark", lambda m: {"id": "bm-1"}
    )
    monkeypatch.setattr(
        activities.karakeep,
        "complete_existing",
        lambda m, bm: PushOutcome(status="completed", assets={"screenshot": 1}),
    )

    outcome = await ActivityEnvironment().run(
        push_to_karakeep, PushParams(media=MEDIA, reconcile=True)
    )
    assert outcome.status == "completed"
    assert outcome.assets == {"screenshot": 1}


@pytest.mark.asyncio
async def test_load_and_save_seen_activities(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(state_svc, "STATE_FILE", tmp_path / "seen.json")
    env = ActivityEnvironment()

    assert await env.run(activities.load_seen) == []  # empty state
    await env.run(activities.save_seen, ["2", "1"])
    assert await env.run(activities.load_seen) == ["1", "2"]
