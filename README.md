# karakeep-superpowers

[![CI](https://github.com/abriemme/karakeep-superpowers/actions/workflows/ci.yml/badge.svg)](https://github.com/abriemme/karakeep-superpowers/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](.python-version)
[![Temporal](https://img.shields.io/badge/Temporal-Worker%20Versioning-orange)](https://docs.temporal.io/develop/python/worker-versioning)
[![pydantic-ai](https://img.shields.io/badge/LLM-pydantic--ai-green)](https://ai.pydantic.dev)

**Agentic superpowers for [Karakeep](https://karakeep.app/), orchestrated with
[Temporal](https://temporal.io/).**

Karakeep is great at storing bookmarks; this repo makes the instance *curate
itself*. LLM agents ([pydantic-ai](https://ai.pydantic.dev)) do the thinking —
titling, summarising, tagging, classifying into lists — and Temporal does the
running: durable, replayable, versioned workflows that survive crashes,
rate-limits, and redeploys without hand-rolled state files or cron glue.

The pattern every superpower follows:

- **Workflows** stay pure and deterministic — pagination cursors, dedup,
  pacing, and circuit-breakers live in workflow state, not on disk;
- **Activities** are thin wrappers over a unit-testable service layer (all the
  Karakeep and source-API I/O);
- **Agents** produce structured output (pydantic models), are tested offline
  with `TestModel`, and pick from the instance's *actual* lists/tags rather
  than hallucinating new ones;
- **Deterministic facts** are derived without an LLM wherever metadata is
  enough — the agent only handles what genuinely needs judgment.

## Superpowers

| Superpower | Status | What it does |
|---|---|---|
| **Instagram saved-posts sync** | ✅ shipped | Nightly, every saved post becomes an enriched Karakeep bookmark: LLM title/summary/tags/list routing, hybrid deterministic+LLM tags, media uploaded as assets, collection→list mapping. |
| **YouTube playlists sync** | ✅ shipped | Every 12 h, new videos in the account's playlists become Karakeep bookmarks: skip-unchanged playlist signatures, video-id dedup against Karakeep, LLM theme classification into the instance's actual lists. |
| More enrichment/organisation workflows | 🔜 | The worker, versioning, testing, and agent scaffolding here are the foundation for further agentic curation of the instance. |

## Superpower: Instagram saved-posts sync

A production-grade rewrite of a real automation: every night, the posts saved
on an Instagram account are turned into enriched bookmarks in Karakeep. It was
previously a Python script driven by n8n over HTTP; it is now a Temporal
application with LLM-powered enrichment.

Each saved post becomes a bookmark with:

- a **hybrid tag set** — deterministic metadata tags (hashtags, author, media
  type, music, location) merged with the LLM's tags;
- **uploaded media** — the post's thumbnail (banner + screenshot, replacing
  Karakeep's login-wall crawl), every carousel slide, optionally the Reel video
  and the author's avatar, all pulled from the Instagram CDN at import time
  (the signed URLs expire) and uploaded to Karakeep as assets;
- **list routing** from both the Instagram collections the post belongs to and
  the LLM's classification;
- the publication date as `createdAt`, so the Karakeep timeline reflects the
  content, not the sync.

A **reconcile** mode (`--reconcile`) confronts each post with the real state of
Karakeep instead of `seen.json`, completing older bookmarks whose thumbnails
were never uploaded. Backfills page through the whole history with a cursor
that lives in the **workflow state** — an interrupted run resumes on its own,
no `cursor.json` on disk.

### Why Temporal (what the n8n version had to hand-roll)

| Hand-rolled in the n8n script | Temporal primitive |
|---|---|
| HTTP endpoint + n8n cron trigger | Schedule (`python -m app.starter schedule`) |
| `cooldown.json` circuit-breaker on Instagram challenges | `workflow.sleep(cooldown)` timer, survives restarts |
| Threads + lock to prevent concurrent syncs | one workflow execution at a time, observable in the UI |
| Manual retry booleans | per-activity `RetryPolicy` + timeouts |
| `time.sleep` pacing, lost on crash | `workflow.sleep` timers (deterministic, replayable) |
| "did it work?" status endpoint | workflow result + history in the Temporal UI |

### Architecture

```mermaid
flowchart LR
    subgraph Schedule
        S[Temporal Schedule\nnightly 03:00]
    end
    subgraph Worker ["Worker (versioned, build_id = git SHA)"]
        W[IgSyncWorkflow]
        A1[fetch_saved_page]
        A2[push_to_karakeep]
        A3[load_seen / save_seen]
    end
    IG[Instagram\ninstagrapi + CDN]
    LLM[LLM agent\npydantic-ai]
    KK[Karakeep API\nbookmarks + assets + lists]

    S --> W
    W -->|page by page| A1 --> IG
    W --> A2 --> LLM
    A2 -->|bookmark + media assets| KK
    W --> A3 --> F[(seen.json)]
```

- **Workflow** (`app/sync/workflow.py`) — pure, deterministic orchestration:
  page-by-page cursor pagination (cursor held in workflow state), dedup against
  the seen-state, oldest-first ordering, pacing timers between pushes and
  pages, circuit-breaker cooldown when Instagram raises a challenge, and a
  reconcile branch that reprocesses every fetched post.
- **Activities** (`app/sync/activities.py`) — thin Temporal wrappers; all I/O
  lives in a service layer (`instagram.py`, `karakeep.py`, `state.py`),
  unit-testable without any worker.
- **Enrichment** (`app/sync/enrich.py`) — a pydantic-ai agent (GPT-5-mini)
  turns each caption into a structured `EnrichedBookmark`: a clean title, a
  summary, tags, and the Karakeep list(s) the post belongs to — chosen from the
  account's actual lists (fetched once and cached), copied verbatim, and left
  empty when nothing clearly fits.
- **Deterministic facts** (`app/sync/facts.py`) — LLM-free note and tag
  derivation from metadata (hashtags with a spam guard, author, media type,
  music, location). The push activity merges these tags with the LLM's and uses
  the rich note as the bookmark's searchable text.
- **Assets & collections** (`app/sync/karakeep.py`) — CDN download + Karakeep
  upload (banner, screenshot, carousel, video, avatar; per-file size cap), plus
  Instagram-collection → Karakeep-list routing. Transient errors are absorbed
  by the push activity's retry policy.

## Superpower: YouTube playlists sync

A rewrite of the "YouTube Playlists to Karakeep" n8n workflow. Every 12 hours,
new videos in the account's playlists become Karakeep bookmarks, each routed
into one thematic list by a pydantic-ai classifier.

The pipeline:

- **Skip-unchanged playlists** — each playlist carries an `etag:itemCount`
  signature; only playlists whose signature moved since the last run are
  re-fetched, with a daily **full sweep** as a safety net (n8n kept this in
  `$getWorkflowStaticData`; here it is `playlists.json` + workflow logic).
- **Dedup by video id** — existing YouTube bookmarks are paged out of Karakeep
  once per run and the 11-character video id is extracted from each URL, so a
  `&t=123s` suffix or a `youtu.be` short link never causes a duplicate.
- **Detail enrichment** — durations, keywords and topic categories are batch
  fetched (50 ids per call) and fed to the classifier.
- **Theme classification** — the agent picks *one* list among the instance's
  actual manual lists (umbrella + excluded lists filtered out), or none; the
  answer is matched accent- and punctuation-insensitively, so a hallucinated
  name degrades to "no theme" instead of misfiling. A classification failure
  never blocks an import: the LLM output here is optional routing metadata.
- **Minimal payload** — the bookmark is created with url + title only:
  Karakeep crawls YouTube itself (description, thumbnail, yt-dlp archive, its
  own AI tagging), then the `youtube` tag and list memberships are attached.

Failure model: playlist signatures are only persisted at the end of a fully
processed run and dedup is against Karakeep itself, so a crashed run re-scans
some playlists but never duplicates a bookmark. Karakeep's own URL dedup
(`alreadyExists`) is the belt to those braces.

## Engineering practices

- **Worker Deployment Versioning**: the worker registers with `build_id` = git
  SHA; each execution stays pinned to its version (`VersioningBehavior.PINNED`).
- **Tests (131)**: time-skipping workflow tests (`WorkflowEnvironment`), service
  unit tests (facts, assets, reconcile, maintenance, YouTube/Karakeep clients),
  `ActivityEnvironment` tests, and **replay tests** that re-run committed JSON
  histories (one per workflow) to catch determinism-breaking changes before
  deploy.
- **Observability**: optional [Logfire](https://logfire.pydantic.dev)
  instrumentation of the pydantic-ai agent and system metrics
  (`app/observability.py`), enabled at worker start; a no-op unless a token is
  present, so tests and CI stay silent.
- **LLM tests without network**: the agent is exercised with pydantic-ai's
  `TestModel` (structured output, no API key needed).
- **CI** ([ci.yml](.github/workflows/ci.yml)): the pre-commit hooks, then
  tests + replay, then a Docker image tagged with the SHA, pushed to GHCR.
- **Pre-commit** ([.pre-commit-config.yaml](.pre-commit-config.yaml)): ruff
  check + format, `uv.lock` freshness, YAML/TOML syntax and GitHub Actions
  workflow validation. The CI lint job runs the same hooks at the same pinned
  revs, so a clean local commit means a green lint job.
- **Unsandboxed workflow runner**: pydantic-ai depends on beartype, which
  monkey-patches the import machinery and is incompatible with the workflow
  sandbox; the workflow code stays deterministic, so this is safe (and
  documented [here](app/worker.py)).

## Layout

```
app/
  config.py            # env-based settings + build_id resolution (git SHA)
  observability.py     # optional Logfire instrumentation (pydantic-ai + metrics)
  sync/                # the Instagram saved-posts superpower
    models.py          # SyncInput/FetchPage*/Push*/SyncSummary, MediaItem, EnrichedBookmark (no I/O)
    workflow.py        # IgSyncWorkflow: pagination, dedup, ordering, pacing, cooldown, reconcile
    activities.py      # thin @activity.defn wrappers + per-post orchestration
    instagram.py       # instagrapi session, paged fetch, collection membership (service)
    karakeep.py        # bookmark + assets + list routing + reconcile index (service)
    facts.py           # deterministic note + metadata tags (pure, no I/O)
    enrich.py          # pydantic-ai agent: title/note/tags + list routing (service)
    maintenance.py     # one-off retag / collections reconcile (CLI, no workflow)
    state.py           # seen.json dedup state (service)
  youtube/             # the YouTube playlists superpower
    models.py          # YtSyncInput/PlaylistInfo/VideoItem/PushVideo*/YtSyncSummary (no I/O)
    workflow.py        # YtSyncWorkflow: signatures, full sweep, dedup, batching, pacing
    activities.py      # thin @activity.defn wrappers + classify-then-push orchestration
    youtube.py         # YouTube Data API: OAuth refresh, playlists, items, details (service)
    karakeep.py        # dedup index, lists, bookmark push via karakeep-python-api (service)
    classify.py        # pydantic-ai agent: one thematic list per video (service)
    state.py           # playlists.json signature state (service)
  starter.py           # manual runs + Schedule creation (replaces the n8n triggers)
  worker.py            # versioned worker (use_worker_versioning=True)
tests/
  conftest.py           # start_time_skipping() fixture
  test_enrich.py        # pydantic-ai agent tests (TestModel) + list routing
  test_facts.py         # deterministic note + tag derivation
  test_assets.py        # asset jobs, CDN upload pipeline, reconcile lookup
  test_maintenance.py   # retag + collections reconcile utilities
  test_activities.py    # service-layer unit tests + ActivityEnvironment mechanics
  test_sync_workflow.py # time-skipping workflow tests (mocked activities)
  test_yt_workflow.py   # YtSyncWorkflow time-skipping tests (mocked activities)
  test_yt_activities.py # push_video orchestration (ActivityEnvironment)
  test_yt_classify.py   # theme classifier tests (TestModel + loose name matching)
  test_yt_services.py   # YouTube/Karakeep client + playlist-state unit tests
  test_replay.py        # replays tests/histories/*.json against current code
scripts/
  generate_history.py   # (re)generates the replay histories
```

## Quickstart

Requires [`uv`](https://docs.astral.sh/uv/) (Python 3.14 installed
automatically).

```bash
uv sync --group ig        # deps incl. instagrapi (worker machine)
uv run pytest             # 131 tests: unit + time-skipping + replay
uv run pre-commit install # commit gate: ruff, uv.lock, YAML/TOML, workflows
```

Run against a local Temporal server:

```bash
temporal server start-dev
GIT_SHA=$(git rev-parse HEAD) uv run python -m app.worker     # worker
uv run python -m app.starter schedule                        # nightly schedule
uv run python -m app.starter sync                            # one-off sync
uv run python -m app.starter sync --backfill                 # full history
uv run python -m app.starter sync --reconcile                # complete missing assets
uv run python -m app.starter yt-schedule                     # every 12h at :12
uv run python -m app.starter yt                              # one-off YouTube sync
uv run python -m app.starter yt --full-sweep                 # re-scan every playlist
```

Maintenance utilities (run by hand, not workflows — dry-run by default):

```bash
uv run python -m app.sync.maintenance collections            # collections ↔ lists report
uv run python -m app.sync.maintenance collections --apply    # create missing lists
uv run python -m app.sync.maintenance retag                  # count bookmarks missing tags
uv run python -m app.sync.maintenance retag --apply          # re-derive & attach tags
```

Environment: copy `.env.example` to `.env` and fill it in. `app/config.py`
auto-loads `.env` at import (via `python-dotenv`), so `uv run` picks it up with
no `export`; a real shell variable or CI value takes precedence over the file.
`.env` is git-ignored. In Docker/prod, pass the variables through the
environment (e.g. `docker run --env-file .env`) rather than baking them in.

| Variable | Purpose |
|---|---|
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` | Temporal server (default `localhost:7233` / `default`) |
| `KARAKEEP_URL`, `KARAKEEP_TOKEN` | Karakeep instance + API token (required) |
| `KARAKEEP_LIST_ID` | optional list added to *every* bookmark, on top of the classified ones |
| `DATA_DIR` | `session.json` (instagrapi), `seen.json` (dedup), `collections.json` (cache) |
| `MAX_ITEMS` | how many recent saved posts to examine per run |
| `MAX_PAGES` | backfill guard-rail: pages walked per run (cursor resumes next run) |
| `OPENAI_API_KEY` | provider key for the enrichment agent (required) |
| `ENRICH_MODEL` | override the model (default `openai:gpt-5-mini`) |
| `LOGFIRE_TOKEN` | optional; enables Logfire tracing/metrics on the worker (no-op if unset) |
| `ASSET_TYPES` | assets to upload: `banner,screenshot,carousel,video,avatar` (default `banner,screenshot,carousel`) |
| `MAX_ASSET_MB`, `MAX_CAROUSEL` | per-file size cap and max carousel slides |
| `REPLACE_BANNER` | overwrite Karakeep's crawler banner with the post thumbnail (default on) |
| `AUTO_TAGS`, `MAX_TAGS` | deterministic tag sources and per-bookmark tag cap |
| `HASHTAG_SPAM_THRESHOLD` | above this hashtag count, the caption is treated as SEO noise |
| `SYNC_COLLECTIONS`, `CREATE_MISSING_LISTS` | route collections to lists; create missing lists |
| `COLLECTION_SCAN`, `COLLECTION_CACHE_HOURS` | collection scan depth and membership cache TTL |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | OAuth2 credentials for the YouTube Data API (playlists sync) |
| `KARAKEEP_YOUTUBE_LIST_ID` | umbrella list receiving every imported video (excluded from theme classification) |
| `YT_EXCLUDED_LIST_IDS` | extra list ids never offered to the theme classifier |
| `YT_MAX_PLAYLIST_PAGES`, `YT_MAX_BOOKMARK_PAGES` | pagination guard-rails (50 videos / 100 bookmarks per page) |
| `YT_MAX_VIDEOS`, `YT_FULL_SWEEP_HOURS` | per-run import cap; hours between two full playlist re-scans |

Instagram login (once, interactive — produces `DATA_DIR/session.json`):
`instagrapi` session bootstrap; see `app/sync/instagram.py`.

## (Re)generate the replay histories

After a *deliberate*, replay-compatible workflow change:

```bash
uv run python scripts/generate_history.py
```

The replay test fails on any determinism-breaking divergence — before
anything reaches production.

## Docker

```bash
docker build --build-arg GIT_SHA=$(git rev-parse HEAD) -t karakeep-superpowers:$(git rev-parse HEAD) .
```

CI builds and pushes `ghcr.io/<repo>:<sha>` (default branch only).
