"""Replay test based on committed JSON histories.

Replay re-runs a real execution history against the *current* workflow code. If
a change breaks determinism (removing a command, reordering, dropping an
activity without ``workflow.patched``...), replay raises a
``NondeterminismError`` and the test fails — before any deployment.

Histories live in ``tests/histories/*.json`` (Temporal JSON export format, the
one from ``temporal workflow show --output json`` or ``WorkflowHistory.to_json``).
See ``scripts/generate_history.py`` to (re)generate them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner

from app.sync import IgSyncWorkflow
from app.youtube import YtSyncWorkflow

HISTORIES_DIR = Path(__file__).parent / "histories"


def _history_files() -> list[Path]:
    return sorted(HISTORIES_DIR.glob("*.json"))


@pytest.mark.parametrize(
    "history_path",
    _history_files(),
    ids=lambda p: p.name,
)
@pytest.mark.asyncio
async def test_replay_history(history_path: Path) -> None:
    history = WorkflowHistory.from_json(
        # The workflow id does not matter for replay; we use the file name for
        # readable error messages.
        workflow_id=history_path.stem,
        history=json.loads(history_path.read_text()),
    )

    # Unsandboxed for the same reason as the worker: pydantic-ai's beartype
    # dependency patches the import machinery, incompatible with the sandbox.
    replayer = Replayer(
        workflows=[IgSyncWorkflow, YtSyncWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    # Raises NondeterminismError if the current code diverges from the history.
    await replayer.replay_workflow(history)


def test_histories_present() -> None:
    """Guardrail: at least one history must be committed."""
    assert _history_files(), (
        "No history in tests/histories/. "
        "Run `python scripts/generate_history.py` to generate one."
    )
