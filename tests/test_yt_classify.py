"""Tests for the pydantic-ai theme classification of YouTube videos.

The agent picks one thematic Karakeep list (or none); its answer is matched
against the real list names accent-, case- and punctuation-insensitively, so
a hallucinated name simply yields no theme.
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from app.youtube.classify import ThemeChoice, classify_video
from app.youtube.models import VideoItem

VIDEO = VideoItem(
    video_id="abcdefghijk",
    url="https://www.youtube.com/watch?v=abcdefghijk",
    title="Building a Proxmox cluster",
    channel="Some Channel",
    duration="PT12M34S",
    tags=["homelab", "proxmox"],
    topics=["Technology"],
    description="A tour of a small homelab.",
)

CANDIDATES = [
    {"id": "l1", "name": "Tech & Homelab", "description": "Servers, self-hosting"},
    {"id": "l2", "name": "Écologie", "description": ""},
]


class _FakeAgent:
    """Agent stub returning a fixed ``ThemeChoice`` as its output."""

    def __init__(self, list_name: str) -> None:
        self._list_name = list_name
        self.calls = 0

    async def run(self, _prompt: str):
        self.calls += 1
        output = ThemeChoice(list_name=self._list_name)
        return type("Run", (), {"output": output})()


@pytest.mark.asyncio
async def test_classify_with_test_model_runs_end_to_end() -> None:
    agent = Agent(TestModel(), output_type=ThemeChoice)
    # TestModel fills the schema with dummy strings: the structured-output
    # path is exercised end-to-end; a non-matching name yields no theme.
    theme = await classify_video(VIDEO, CANDIDATES, agent=agent)
    assert theme is None


@pytest.mark.asyncio
async def test_classify_matches_names_loosely() -> None:
    """Case, accents and punctuation must not break the match (n8n parity)."""
    theme = await classify_video(VIDEO, CANDIDATES, agent=_FakeAgent("tech homelab"))
    assert theme == ("l1", "Tech & Homelab")

    theme = await classify_video(VIDEO, CANDIDATES, agent=_FakeAgent("ecologie"))
    assert theme == ("l2", "Écologie")


@pytest.mark.asyncio
async def test_classify_drops_hallucinated_or_empty_answers() -> None:
    assert await classify_video(VIDEO, CANDIDATES, agent=_FakeAgent("Gardening")) is None
    assert await classify_video(VIDEO, CANDIDATES, agent=_FakeAgent("")) is None


@pytest.mark.asyncio
async def test_classify_without_candidates_skips_the_llm() -> None:
    agent = _FakeAgent("whatever")
    assert await classify_video(VIDEO, [], agent=agent) is None
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_classify_agent_failure_propagates() -> None:
    class FailingAgent:
        async def run(self, *a, **kw):
            raise RuntimeError("LLM provider down")

    with pytest.raises(RuntimeError):
        await classify_video(VIDEO, CANDIDATES, agent=FailingAgent())
