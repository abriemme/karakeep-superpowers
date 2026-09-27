"""YouTube playlists -> Karakeep sync package.

Public surface: workflow, activities and models, re-exported lazily
(``from app.youtube import YtSyncWorkflow, ...``). Lazy re-exports keep the
package importable from inside the workflow sandbox: importing ``app.youtube``
must not pull I/O modules (requests, pydantic-ai) eagerly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - static analysis only
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
        PlaylistInfo,
        PlaylistState,
        PushVideoOutcome,
        PushVideoParams,
        ThemeChoice,
        VideoDetails,
        VideoItem,
        YtSyncInput,
        YtSyncSummary,
    )
    from app.youtube.workflow import PLAYLIST_DELAY, PUSH_DELAY, YtSyncWorkflow

_LAZY_EXPORTS = {
    "PLAYLIST_DELAY": "app.youtube.workflow",
    "PUSH_DELAY": "app.youtube.workflow",
    "PlaylistInfo": "app.youtube.models",
    "PlaylistState": "app.youtube.models",
    "PushVideoOutcome": "app.youtube.models",
    "PushVideoParams": "app.youtube.models",
    "ThemeChoice": "app.youtube.models",
    "VideoDetails": "app.youtube.models",
    "VideoItem": "app.youtube.models",
    "YtSyncInput": "app.youtube.models",
    "YtSyncSummary": "app.youtube.models",
    "YtSyncWorkflow": "app.youtube.workflow",
    "fetch_existing_video_ids": "app.youtube.activities",
    "fetch_playlist_items": "app.youtube.activities",
    "fetch_playlists": "app.youtube.activities",
    "fetch_video_details": "app.youtube.activities",
    "load_playlist_state": "app.youtube.activities",
    "push_video": "app.youtube.activities",
    "save_playlist_state": "app.youtube.activities",
}


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value  # cache for subsequent accesses
    return value


__all__ = sorted(_LAZY_EXPORTS)
