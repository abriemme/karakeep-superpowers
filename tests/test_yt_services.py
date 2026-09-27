"""Unit tests for the YouTube sync service layer (youtube, karakeep, state).

Service functions are tested directly (no worker, no workflow); HTTP calls
are replaced with fakes at the ``requests`` boundary.
"""

from __future__ import annotations

import pytest
import requests
from karakeep_python_api import APIError

import app.youtube.karakeep as karakeep_svc
import app.youtube.state as state_svc
import app.youtube.youtube as youtube_svc
from app.youtube.models import PlaylistState


class _Resp:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = body
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> dict:
        return self._body


# --- Playlist state -----------------------------------------------------------


def test_playlist_state_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(state_svc, "PLAYLIST_STATE_FILE", tmp_path / "playlists.json")

    state_svc.save_playlist_state(
        PlaylistState(signatures={"p2": "b", "p1": "a"}, last_full_sweep="2026-01-01")
    )
    loaded = state_svc.load_playlist_state()
    assert loaded.signatures == {"p1": "a", "p2": "b"}
    assert loaded.last_full_sweep == "2026-01-01"


def test_playlist_state_missing_or_corrupt_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "playlists.json"
    monkeypatch.setattr(state_svc, "PLAYLIST_STATE_FILE", path)

    assert state_svc.load_playlist_state() == PlaylistState()  # no file yet

    path.write_text("not json")
    assert state_svc.load_playlist_state() == PlaylistState()  # unreadable -> fresh


# --- Karakeep -----------------------------------------------------------------


class ClientStub:
    """Stand-in for ``KarakeepAPI`` (validation-disabled: raw dict payloads)."""

    def __init__(self) -> None:
        self.search_pages: list[dict] = []
        self.search_cursors: list = []
        self.lists_payload: dict = {"lists": []}
        self.lists_calls = 0
        self.create_response: dict = {"id": "b1"}
        self.raise_on: set[str] = set()
        self.tagged: list = []
        self.added: list = []

    def _maybe_raise(self, name: str) -> None:
        if name in self.raise_on:
            raise APIError(f"{name} failed")

    def search_bookmarks(self, q, limit, cursor):
        self.search_cursors.append(cursor)
        return self.search_pages.pop(0)

    def get_all_lists(self):
        self.lists_calls += 1
        return self.lists_payload

    def create_a_new_bookmark(self, type, url, title):
        self._maybe_raise("create")
        return self.create_response

    def attach_tags_to_a_bookmark(self, bookmark_id, tag_names):
        self._maybe_raise("tag")
        self.tagged.append((bookmark_id, tag_names))

    def add_a_bookmark_to_a_list(self, list_id, bookmark_id):
        self._maybe_raise("add")
        self.added.append((list_id, bookmark_id))


@pytest.fixture
def kk(monkeypatch) -> ClientStub:
    stub = ClientStub()
    monkeypatch.setattr(karakeep_svc, "_client", stub)
    monkeypatch.setattr(karakeep_svc, "_lists_cache", None)
    return stub


def test_extract_video_id_variants() -> None:
    extract = karakeep_svc.extract_video_id
    assert extract("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
    # Extra parameters (&t=123s) must not defeat the dedup.
    assert extract("https://www.youtube.com/watch?v=abcdefghijk&t=123s") == "abcdefghijk"
    assert extract("https://youtu.be/ABCDEFGHIJ1") == "ABCDEFGHIJ1"
    assert extract("https://example.com/article") is None
    assert extract("") is None


def test_existing_video_ids_paginates_and_extracts(kk: ClientStub) -> None:
    kk.search_pages = [
        {
            "bookmarks": [
                {"content": {"url": "https://www.youtube.com/watch?v=abcdefghijk"}},
                {"content": {"url": "https://example.com/not-youtube"}},
            ],
            "nextCursor": "c2",
        },
        {
            "bookmarks": [
                {"content": {"url": "https://youtu.be/ABCDEFGHIJ1?t=9"}},
            ],
            "nextCursor": None,
        },
    ]

    assert karakeep_svc.existing_video_ids() == {"abcdefghijk", "ABCDEFGHIJ1"}
    assert kk.search_cursors == [None, "c2"]


def test_fetch_lists_filters_smart_lists_and_caches(kk: ClientStub) -> None:
    kk.lists_payload = {
        "lists": [
            {"id": "l1", "name": "Tech", "description": "Servers"},
            {"id": "l2", "name": "All videos", "type": "smart"},
            {"id": "l3", "name": "Cuisine", "type": "manual"},
        ]
    }

    assert karakeep_svc.fetch_lists() == [
        {"id": "l1", "name": "Tech", "description": "Servers"},
        {"id": "l3", "name": "Cuisine", "description": ""},
    ]
    karakeep_svc.fetch_lists()  # second call served from cache, no extra request
    assert kk.lists_calls == 1


def test_create_bookmark_success_and_already_exists(kk: ClientStub) -> None:
    assert karakeep_svc.create_bookmark("https://x", "t") == ("b1", False)

    kk.create_response = {"id": "b1", "alreadyExists": True}
    assert karakeep_svc.create_bookmark("https://x", "t") == ("b1", True)


def test_create_bookmark_failure_returns_failed(kk: ClientStub) -> None:
    # The client wraps HTTP and network errors alike into APIError.
    kk.raise_on = {"create"}
    assert karakeep_svc.create_bookmark("https://x", "t") == (None, False)


def test_tag_and_list_membership(kk: ClientStub) -> None:
    assert karakeep_svc.tag_bookmark("b1", ["youtube"]) is True
    assert karakeep_svc.add_to_list("b1", "l1") is True
    assert kk.tagged == [("b1", ["youtube"])]
    assert kk.added == [("l1", "b1")]

    kk.raise_on = {"tag", "add"}
    assert karakeep_svc.tag_bookmark("b1", ["youtube"]) is False
    assert karakeep_svc.add_to_list("b1", "l1") is False


# --- YouTube Data API ---------------------------------------------------------


def test_access_token_is_cached(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        return _Resp({"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr(youtube_svc, "_token", None)
    monkeypatch.setattr(youtube_svc, "_token_expiry", 0.0)
    monkeypatch.setattr("app.youtube.youtube.requests.post", fake_post)

    assert youtube_svc._access_token() == "tok"
    assert youtube_svc._access_token() == "tok"  # cached, no second refresh
    assert calls["n"] == 1


@pytest.fixture
def yt_get(monkeypatch):
    """Route the service's GETs to a canned page sequence (token stubbed)."""
    monkeypatch.setattr(youtube_svc, "_access_token", lambda: "tok")
    monkeypatch.setattr(youtube_svc, "PAGE_INTERVAL", 0.0)
    state = {"pages": [], "params": []}

    def fake_get(url, params=None, **kw):
        state["params"].append(params)
        return state["pages"].pop(0)

    monkeypatch.setattr("app.youtube.youtube.requests.get", fake_get)
    return state


def test_fetch_playlists_paginates(yt_get) -> None:
    yt_get["pages"] = [
        _Resp(
            {
                "items": [
                    {
                        "id": "p1",
                        "etag": "e1",
                        "snippet": {"title": "Liked"},
                        "contentDetails": {"itemCount": 3},
                    }
                ],
                "nextPageToken": "t2",
            }
        ),
        _Resp({"items": [{"id": "p2", "etag": "e2"}]}),
    ]

    playlists = youtube_svc.fetch_playlists()

    assert [(p.id, p.title, p.etag, p.item_count) for p in playlists] == [
        ("p1", "Liked", "e1", 3),
        ("p2", "", "e2", 0),
    ]
    assert yt_get["params"][1]["pageToken"] == "t2"


def test_fetch_playlist_items_drops_tombstones(yt_get) -> None:
    yt_get["pages"] = [
        _Resp(
            {
                "items": [
                    {
                        "contentDetails": {"videoId": "abcdefghijk"},
                        "snippet": {
                            "title": "A real video",
                            "videoOwnerChannelTitle": "Chan",
                            "description": "d" * 700,
                            "publishedAt": "2026-01-01T00:00:00Z",
                        },
                    },
                    {
                        "contentDetails": {"videoId": "deleted00000"},
                        "snippet": {"title": "Deleted video"},
                    },
                    {
                        "contentDetails": {"videoId": "private00000"},
                        "snippet": {"title": "Private video"},
                    },
                    {"snippet": {"title": "No video id"}},
                ]
            }
        )
    ]

    videos = youtube_svc.fetch_playlist_items("p1")

    assert len(videos) == 1
    video = videos[0]
    assert video.video_id == "abcdefghijk"
    assert video.url == "https://www.youtube.com/watch?v=abcdefghijk"
    assert video.channel == "Chan"
    assert len(video.description) == 600  # sliced like the n8n diff node
    assert video.published_at == "2026-01-01T00:00:00Z"


def test_fetch_video_details_decodes_topics(yt_get) -> None:
    yt_get["pages"] = [
        _Resp(
            {
                "items": [
                    {
                        "id": "abcdefghijk",
                        "snippet": {
                            "tags": [f"t{i}" for i in range(20)],
                            "description": "full description",
                        },
                        "contentDetails": {"duration": "PT12M34S"},
                        "topicDetails": {
                            "topicCategories": [
                                "https://en.wikipedia.org/wiki/Rock_music",
                                "https://en.wikipedia.org/wiki/Do_it_yourself",
                            ]
                        },
                    }
                ]
            }
        )
    ]

    details = youtube_svc.fetch_video_details(["abcdefghijk"])

    d = details["abcdefghijk"]
    assert d.duration == "PT12M34S"
    assert len(d.tags) == 15  # capped like the n8n merge node
    assert d.topics == ["Rock music", "Do it yourself"]
    assert d.description == "full description"


def test_fetch_video_details_empty_ids_makes_no_call(yt_get) -> None:
    assert youtube_svc.fetch_video_details([]) == {}
    assert yt_get["params"] == []
