"""LLM theme classification of YouTube videos, powered by pydantic-ai.

The agent picks *one* thematic Karakeep list per video (or none), from the
account's actual lists. Unlike the Instagram enrichment, the LLM output here
is purely optional routing metadata: everything else on the bookmark comes
from YouTube or from Karakeep's own crawler.

The answer is matched against the real list names with an accent- and
punctuation-insensitive comparison (the n8n version needed the same
normalisation): a hallucinated or empty answer simply yields no theme.
"""

from __future__ import annotations

import os
import re
import unicodedata

from app.youtube.models import ThemeChoice, VideoItem

__all__ = ["ThemeChoice", "build_agent", "classify_video"]

_SYSTEM_PROMPT = (
    "You route YouTube videos into a single thematic list of a read-it-later "
    "app. From the video metadata, reply with the name of the one list the "
    "video clearly belongs to, chosen among the candidate lists.\n"
    "Rules:\n"
    "- Copy the list name exactly as given, accents and ampersands included.\n"
    "- When several lists fit, choose the most specific one.\n"
    "- When no list clearly fits, return an empty string — never force a "
    "classification."
)

# Same model knob as the Instagram enrichment: one env var drives both agents.
DEFAULT_MODEL = os.environ.get("ENRICH_MODEL", "openai:gpt-5-mini")


def build_agent():
    """Agent producing a structured ``ThemeChoice`` for one video.

    pydantic-ai is imported lazily: its import chain (beartype...) is
    incompatible with the workflow sandbox's import restrictions.
    """
    from pydantic_ai import Agent

    return Agent(
        DEFAULT_MODEL,
        output_type=ThemeChoice,
        system_prompt=_SYSTEM_PROMPT,
    )


def _norm(name: str) -> str:
    """Accent-, case- and punctuation-insensitive key for name matching."""
    decomposed = unicodedata.normalize("NFD", name or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", stripped.casefold())


def _prompt(video: VideoItem, candidates: list[dict]) -> str:
    lists_block = "\n".join(
        f'- "{item["name"]}": {item.get("description") or item["name"]}'
        for item in candidates
    )
    return (
        f"Title: {video.title}\n"
        f"Channel: {video.channel}\n"
        f"ISO-8601 duration: {video.duration}\n"
        f"Video keywords: {', '.join(video.tags)}\n"
        f"YouTube categories: {', '.join(video.topics)}\n"
        f"Description: {video.description}\n\n"
        f"Candidate lists:\n{lists_block}"
    )


async def classify_video(
    video: VideoItem,
    candidates: list[dict],
    agent=None,
) -> tuple[str, str] | None:
    """Return the ``(id, name)`` of the chosen thematic list, or ``None``.

    ``candidates`` are ``{"id", "name", "description"}`` dicts. Raises on LLM
    failure; the caller decides whether that degrades to "no theme".
    """
    if not candidates:
        return None
    run = await (agent or build_agent()).run(_prompt(video, candidates))
    key = _norm(run.output.list_name)
    if not key:
        return None
    for item in candidates:
        if _norm(item["name"]) == key:
            return item["id"], item["name"]
    return None
