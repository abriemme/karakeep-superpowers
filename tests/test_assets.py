"""Tests for the Karakeep asset pipeline (download, upload, attach, reconcile)."""

from __future__ import annotations

from pathlib import Path

import requests
from karakeep_python_api import APIError

import app.sync.karakeep as karakeep_svc
from app.sync.karakeep import (
    asset_jobs,
    complete_existing,
    find_existing_bookmark,
    push_assets,
)
from app.sync.models import EnrichedBookmark, MediaItem, MediaResource


def _media(**kw) -> MediaItem:
    base = {"pk": "1", "code": "abc", "username": "chef", "caption": ""}
    base.update(kw)
    return MediaItem(**base)


def test_asset_jobs_new_bookmark_default_types() -> None:
    media = _media(
        thumbnail_url="https://cdn/thumb.jpg",
        media_type=2,
        video_url="https://cdn/v.mp4",
        resources=[
            MediaResource(thumbnail_url="https://cdn/1.jpg"),
            MediaResource(thumbnail_url="https://cdn/2.jpg"),
        ],
    )
    types = [j["type"] for j in asset_jobs(None, media)]
    # Default ASSET_TYPES = banner,screenshot,carousel (no video).
    assert types == ["bannerImage", "screenshot", "userUploaded", "userUploaded"]


def test_asset_jobs_skips_present_useruploaded_on_reconcile() -> None:
    media = _media(
        thumbnail_url="https://cdn/thumb.jpg",
        resources=[MediaResource(thumbnail_url="https://cdn/1.jpg")],
    )
    existing = {
        "id": "bm-1",
        "assets": [
            {"assetType": "bannerImage", "id": "old-banner"},
            {"assetType": "userUploaded", "id": "slide"},
        ],
    }
    jobs = asset_jobs(existing, media)
    # Carousel slide already present -> not re-uploaded; banner is replaced.
    assert not any(j["type"] == "userUploaded" for j in jobs)
    banner = next(j for j in jobs if j["type"] == "bannerImage")
    assert banner["replace"] == "old-banner"


def test_push_assets_counts_successful_jobs(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "download_cdn", lambda url: b"data")
    monkeypatch.setattr(karakeep_svc, "upload_asset", lambda c, f: "asset-id")
    monkeypatch.setattr(karakeep_svc, "attach_asset", lambda b, a, t: True)
    monkeypatch.setattr(karakeep_svc, "replace_asset", lambda b, o, n: True)

    def job(kind, replace=None):
        return {"type": kind, "replace": replace, "url": "u", "filename": "f"}

    jobs = [job("bannerImage"), job("screenshot", replace="old")]
    assert push_assets("bm-1", jobs) == {"bannerImage": 1, "screenshot": 1}


def test_push_assets_skips_failed_download(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "download_cdn", lambda url: None)
    jobs = [{"type": "bannerImage", "replace": None, "url": "u", "filename": "f"}]
    assert push_assets("bm-1", jobs) == {}


def test_find_existing_bookmark_matches_by_url(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "_bookmarks_by_url", None)

    def fake_iter(*a, **kw):
        yield [
            {"id": "bm-1", "content": {"url": "https://www.instagram.com/p/abc/"}},
            {"id": "bm-2", "content": {"url": "https://example.com/x"}},
        ]

    monkeypatch.setattr(karakeep_svc, "iter_bookmarks", fake_iter)

    found = find_existing_bookmark(_media(code="abc"))
    assert found is not None and found["id"] == "bm-1"
    assert find_existing_bookmark(_media(code="zzz")) is None


def test_complete_existing_skips_when_nothing_missing() -> None:
    media = _media()  # no thumbnail, no resources -> no asset jobs
    outcome = complete_existing(media, {"id": "bm-1", "assets": []})
    assert outcome.status == "skipped"


# --- Instagram CDN downloads (plain requests, not the Karakeep client) ---------


class _Resp:
    """Minimal requests.Response stand-in for the CDN downloads."""

    def __init__(self, status_code=200, headers=None, chunks=()) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.RequestException(f"status {self.status_code}")

    def iter_content(self, size):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_cdn_streams_and_concatenates(monkeypatch) -> None:
    monkeypatch.setattr(
        karakeep_svc.requests, "get", lambda *a, **kw: _Resp(chunks=[b"ab", b"cd"])
    )
    assert karakeep_svc.download_cdn("https://cdn/x.jpg") == b"abcd"


def test_download_cdn_rejects_oversized_content_length(monkeypatch) -> None:
    huge = str(int(karakeep_svc.MAX_ASSET_MB * 1024 * 1024) + 1)
    monkeypatch.setattr(
        karakeep_svc.requests,
        "get",
        lambda *a, **kw: _Resp(headers={"Content-Length": huge}),
    )
    assert karakeep_svc.download_cdn("https://cdn/big.mp4") is None


def test_download_cdn_bounds_a_lying_content_length(monkeypatch) -> None:
    # No Content-Length, but the stream itself exceeds the cap.
    monkeypatch.setattr(karakeep_svc, "MAX_ASSET_MB", 0.000001)  # 1 byte
    monkeypatch.setattr(
        karakeep_svc.requests, "get", lambda *a, **kw: _Resp(chunks=[b"xxxx"])
    )
    assert karakeep_svc.download_cdn("https://cdn/x.jpg") is None


def test_download_cdn_network_error_returns_none(monkeypatch) -> None:
    def boom(*a, **kw):
        raise requests.RequestException("down")

    monkeypatch.setattr(karakeep_svc.requests, "get", boom)
    assert karakeep_svc.download_cdn("https://cdn/x.jpg") is None


# --- Karakeep client helpers ----------------------------------------------------


def test_api_builds_the_client_once_with_a_normalised_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "_api", None)
    monkeypatch.setattr(karakeep_svc, "KARAKEEP_URL", "https://kk.example.com")
    monkeypatch.setattr(karakeep_svc, "KARAKEEP_TOKEN", "secret")
    # The constructor greets the instance with GET /users/me: stub it out.
    monkeypatch.setattr(
        karakeep_svc.KarakeepAPI, "get_current_user_info", lambda self: {"id": "u1"}
    )

    client = karakeep_svc.api()
    # The client appends /api/v1/ to the base URL itself.
    assert client.api_endpoint == "https://kk.example.com/api/v1/"
    assert client.disable_response_validation is True
    assert karakeep_svc.api() is client  # created once per process


def test_upload_asset_writes_the_bytes_and_returns_id(monkeypatch) -> None:
    seen: dict = {}

    class FakeApi:
        def upload_a_new_asset(self, file):
            seen["name"] = Path(file).name
            seen["content"] = Path(file).read_bytes()
            return {"assetId": "a-1"}

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.upload_asset(b"x", "f.jpg") == "a-1"
    # The client uploads from a path: the bytes reach it as a real file whose
    # name (and thus extension/MIME) is the asset's.
    assert seen == {"name": "f.jpg", "content": b"x"}


def test_upload_asset_api_error_returns_none(monkeypatch) -> None:
    class FakeApi:
        def upload_a_new_asset(self, file):
            raise APIError("rejected", status_code=500)

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.upload_asset(b"x", "f.jpg") is None


def test_upload_asset_unexpected_body_returns_none(monkeypatch) -> None:
    class FakeApi:
        def upload_a_new_asset(self, file):
            return None

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.upload_asset(b"x", "f.jpg") is None


def test_attach_asset_maps_errors_to_false(monkeypatch) -> None:
    class FakeApi:
        def attach_asset(self, bookmark_id, asset_id, asset_type):
            return {"id": asset_id, "assetType": asset_type}

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.attach_asset("bm-1", "a-1", "bannerImage") is True

    class Rejecting:
        def attach_asset(self, bookmark_id, asset_id, asset_type):
            raise APIError("rejected", status_code=400)

    monkeypatch.setattr(karakeep_svc, "_api", Rejecting())
    assert karakeep_svc.attach_asset("bm-1", "a-1", "bannerImage") is False


def test_replace_asset_maps_errors_to_false(monkeypatch) -> None:
    class FakeApi:
        def replace_asset(self, bookmark_id, asset_id, new_asset_id):
            return None  # Karakeep answers 204 with no body

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.replace_asset("bm-1", "old", "new") is True

    class Failing:
        def replace_asset(self, bookmark_id, asset_id, new_asset_id):
            raise APIError("down")

    monkeypatch.setattr(karakeep_svc, "_api", Failing())
    assert karakeep_svc.replace_asset("bm-1", "old", "new") is False


def test_push_assets_skips_failed_upload_and_attach(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "download_cdn", lambda url: b"data")
    monkeypatch.setattr(karakeep_svc, "upload_asset", lambda c, f: None)
    job = {"type": "screenshot", "replace": None, "url": "u", "filename": "f"}
    assert push_assets("bm-1", [job]) == {}

    monkeypatch.setattr(karakeep_svc, "upload_asset", lambda c, f: "a-1")
    monkeypatch.setattr(karakeep_svc, "attach_asset", lambda b, a, t: False)
    assert push_assets("bm-1", [job]) == {}


def test_asset_jobs_includes_video_and_avatar_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        karakeep_svc, "ASSET_TYPES", ["banner", "video", "carousel", "avatar"]
    )
    media = _media(
        thumbnail_url="https://cdn/thumb.jpg",
        media_type=2,
        video_url="https://cdn/v.mp4",
        profile_pic_url="https://cdn/me.jpg",
        # The empty slide has no URL at all and is skipped.
        resources=[MediaResource(video_url="https://cdn/slide.mp4"), MediaResource()],
    )
    jobs = asset_jobs(None, media)
    assert [j["type"] for j in jobs] == [
        "bannerImage",
        "video",
        "userUploaded",
        "avatar",
    ]
    # The carousel slide is a video, so its filename keeps the mp4 extension
    # (the client derives the MIME type from it).
    assert jobs[2]["filename"].endswith(".mp4")


def test_asset_jobs_keeps_banner_when_replace_disabled(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "REPLACE_BANNER", False)
    monkeypatch.setattr(karakeep_svc, "ASSET_TYPES", ["banner"])
    media = _media(thumbnail_url="https://cdn/thumb.jpg")
    existing = {"id": "bm-1", "assets": [{"assetType": "bannerImage", "id": "old"}]}
    assert asset_jobs(existing, media) == []


def test_complete_existing_pushes_the_gaps(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "ASSET_TYPES", ["banner"])
    monkeypatch.setattr(karakeep_svc, "push_assets", lambda bid, jobs: {"bannerImage": 1})
    media = _media(thumbnail_url="https://cdn/thumb.jpg")
    outcome = complete_existing(media, {"id": "bm-1", "assets": []})
    assert outcome.status == "completed"
    assert outcome.assets == {"bannerImage": 1}


def test_iter_bookmarks_follows_the_cursor(monkeypatch) -> None:
    pages = [
        {"bookmarks": [{"id": "b1"}], "nextCursor": "c2"},
        {"bookmarks": [{"id": "b2"}]},  # no cursor -> stop
    ]
    seen_cursors: list[str | None] = []

    class FakeApi:
        def get_all_bookmarks(self, limit=None, cursor=None):
            seen_cursors.append(cursor)
            return pages.pop(0)

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert [p[0]["id"] for p in karakeep_svc.iter_bookmarks()] == ["b1", "b2"]
    assert seen_cursors == [None, "c2"]


def test_iter_bookmarks_stops_on_empty_page(monkeypatch) -> None:
    class FakeApi:
        def get_all_bookmarks(self, limit=None, cursor=None):
            return {"bookmarks": []}

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert list(karakeep_svc.iter_bookmarks()) == []


def test_iter_bookmarks_stops_on_unexpected_payload(monkeypatch) -> None:
    class FakeApi:
        def get_all_bookmarks(self, limit=None, cursor=None):
            return None

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert list(karakeep_svc.iter_bookmarks()) == []


# --- Lists --------------------------------------------------------------------


def test_create_list_returns_id_and_refreshes_cache(monkeypatch) -> None:
    created: list[dict] = []

    class FakeApi:
        def create_a_new_list(self, **kwargs):
            created.append(kwargs)
            return {"id": "l9"}

    monkeypatch.setattr(karakeep_svc, "_lists_cache", [])
    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.create_list("Voyage", parent_id="p1") == "l9"
    assert karakeep_svc._lists_cache == [{"id": "l9", "name": "Voyage"}]
    assert created[0]["name"] == "Voyage"
    assert created[0]["parent_id"] == "p1"


def test_create_list_api_error_returns_none(monkeypatch) -> None:
    class FakeApi:
        def create_a_new_list(self, **kwargs):
            raise APIError("down")

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.create_list("Voyage") is None


def test_attach_tags_noop_on_empty_and_posts_otherwise(monkeypatch) -> None:
    assert karakeep_svc.attach_tags("bm-1", []) is True

    posted: list = []

    class FakeApi:
        def attach_tags_to_a_bookmark(self, bookmark_id, tag_names=None, **kw):
            posted.append(tag_names)
            return {"attached": []}

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.attach_tags("bm-1", ["food"]) is True
    assert posted == [["food"]]


def test_attach_tags_api_error_returns_false(monkeypatch) -> None:
    class FakeApi:
        def attach_tags_to_a_bookmark(self, bookmark_id, tag_names=None, **kw):
            raise APIError("down")

    monkeypatch.setattr(karakeep_svc, "_api", FakeApi())
    assert karakeep_svc.attach_tags("bm-1", ["food"]) is False


def test_resolve_list_abstains_when_ambiguous(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "CREATE_MISSING_LISTS", False)
    index = {"voyage": ["l1", "l2"]}
    assert karakeep_svc._resolve_list("Voyage", index) is None
    assert karakeep_svc._resolve_list("Ghost", index) is None  # missing, creation off


def test_resolve_list_creates_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(karakeep_svc, "CREATE_MISSING_LISTS", True)
    monkeypatch.setattr(karakeep_svc, "create_list", lambda name: "l9")
    index: dict[str, list[str]] = {}
    assert karakeep_svc._resolve_list("Ghost", index) == "l9"
    assert index["ghost"] == ["l9"]


def test_target_lists_merges_collections_and_default(monkeypatch) -> None:
    monkeypatch.setattr(
        karakeep_svc,
        "_lists_cache",
        [{"id": "l1", "name": "Voyage"}, {"id": "l2", "name": "Cuisine"}],
    )
    monkeypatch.setattr(karakeep_svc, "KARAKEEP_LIST_ID", "l3")
    media = _media(collection_names=["Cuisine", "Voyage"])  # Voyage also from the LLM
    enrichment = EnrichedBookmark(title="t", note="n", tags=[], lists=["Voyage"])
    assert karakeep_svc._target_lists(media, enrichment) == [
        ("l1", "Voyage"),
        ("l2", "Cuisine"),
        ("l3", ""),
    ]
