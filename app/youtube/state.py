"""Playlist-signature persistence (skip-unchanged optimisation).

Replaces the n8n ``$getWorkflowStaticData`` blob: a playlist whose
etag/itemCount signature has not moved since the previous run is not
re-fetched. Losing this file is harmless — the next run re-scans everything
and the dedup against Karakeep absorbs it.
"""

from __future__ import annotations

import json

from app.config import PLAYLIST_STATE_FILE
from app.youtube.models import PlaylistState


def load_playlist_state() -> PlaylistState:
    if not PLAYLIST_STATE_FILE.exists():
        return PlaylistState()
    try:
        raw = json.loads(PLAYLIST_STATE_FILE.read_text())
        return PlaylistState(
            signatures=dict(raw.get("signatures", {})),
            last_full_sweep=str(raw.get("last_full_sweep", "")),
        )
    except json.JSONDecodeError, TypeError, ValueError:
        return PlaylistState()


def save_playlist_state(state: PlaylistState) -> None:
    PLAYLIST_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAYLIST_STATE_FILE.write_text(
        json.dumps(
            {
                "signatures": dict(sorted(state.signatures.items())),
                "last_full_sweep": state.last_full_sweep,
            },
            indent=0,
        )
    )
