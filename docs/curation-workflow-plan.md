# Plan — Daily "librarian" curation workflow with human-in-the-loop

## Goal

A daily Temporal workflow that analyzes every Karakeep list, proposes a
reorganization (route new/orphan bookmarks, surface overlapping or drifting
lists), and applies it **only after human approval**. The agent proposes,
a human decides, deterministic code executes.

This is also a learning vehicle: each phase introduces exactly one agentic
pattern — structured plan output → durable human gate → tiered autonomy →
feedback memory.

## Architecture (target state)

```
CurationWorkflow  (Schedule "kk-curation-scheduled", daily 07:00 — after the 03:00 sync)
│
├─ 1. snapshot_library        activity   dump lists + bookmarks to DATA_DIR, return path + stats
├─ 2. propose_reorg           activity   pydantic-ai agent → ReorgPlan (typed operations)
├─ 3. validate_plan           in-workflow / pure fn   dry-run ops against snapshot, drop no-ops,
│                                        enforce guard-rails (op count caps, no destructive ops)
├─ 4. HUMAN GATE              @workflow.update "decide"  approve / reject / amend
│                             wait_condition, timeout 48h → plan expires (reject)
├─ 5. apply_operations        activity   idempotent execution, one Karakeep call at a time
└─ 6. record_outcome          activity   append decisions to curation-log.jsonl (feedback memory)
```

Key decisions (settled in the architecture discussion):

- **Plan-then-execute.** The LLM never mutates Karakeep. It emits a
  `ReorgPlan` — data, not actions. Apply is plain deterministic code.
- **`@workflow.update` over a bare signal** for the decision: it has a
  validator (reject malformed decisions) and returns a result to the caller.
- **Payloads by reference.** The library snapshot can exceed Temporal's
  ~2 MB event limit. `snapshot_library` writes JSON under `DATA_DIR` and
  activities exchange the *path*. The `ReorgPlan` itself is small and lives
  in workflow state.
- **Idempotent apply.** Every operation re-checks state before acting
  (same culture as `find_existing_bookmark` / `complete_existing` in
  `karakeep.py`), so activity retries are safe.
- **Two independent Schedules** (sync 03:00, curation 07:00). No chaining:
  simpler to reason about, replay, and pause independently.
- **Workflow ID** `curation-YYYY-MM-DD`: natural dedup, readable history.
- **Same worker, same task queue** (`ig-sync-task-queue`), registered next
  to `IgSyncWorkflow` — one deployment, one versioning story.

## New files

| File | Contents |
|---|---|
| `app/curation/__init__.py` | public exports (workflow + activities) |
| `app/curation/models.py` | `CurationInput`, `ReorgOp` (tagged union), `ReorgPlan`, `Decision`, `CurationReport` — dataclasses, no I/O, sandbox-safe (mirrors `app/sync/models.py`) |
| `app/curation/snapshot.py` | build the library snapshot from `karakeep.iter_bookmarks()` + `fetch_lists()`; write/read `DATA_DIR/curation/snapshot-<date>.json` |
| `app/curation/propose.py` | pydantic-ai agent (lazy import, same pattern as `enrich.py`); prompt gets a *compact* digest (per-list: name, size, sample titles/tags; orphans list), not the raw dump |
| `app/curation/validate.py` | pure functions: dry-run each op against the snapshot, drop no-ops, cap op counts, forbid op types not yet allowed |
| `app/curation/apply.py` | execute approved ops via `karakeep.py` primitives (`_add_to_list`, `create_list`, …), collect per-op outcome |
| `app/curation/activities.py` | thin Temporal wrappers: `snapshot_library`, `propose_reorg`, `apply_operations`, `record_outcome` |
| `app/curation/workflow.py` | `CurationWorkflow`: orchestration + `@workflow.update decide` + `@workflow.query current_plan` |
| `tests/test_curation_models.py` | op semantics, plan (de)serialization |
| `tests/test_curation_validate.py` | dry-run, guard-rails, no-op elimination |
| `tests/test_curation_workflow.py` | time-skipping env: approve / reject / amend / timeout paths |
| `docs/curation-workflow-plan.md` | this plan |

Touched files: `app/worker.py` (register workflow + activities),
`app/starter.py` (add `curate` and `curate-schedule` commands),
`app/config.py` (new `# --- Curation ---` block), `README.md` (short section).

## Core models (sketch)

```python
@dataclass
class ReorgOp:
    kind: str  # "move_bookmark" | "add_to_list" | "create_list"
    # (V3+: "rename_list", "merge_lists")
    rationale: str
    confidence: float  # 0..1
    bookmark_id: str | None = None
    list_name: str | None = None
    target_list: str | None = None


@dataclass
class ReorgPlan:
    generated_at: str
    ops: list[ReorgOp]
    summary: str  # human-readable digest shown to the approver


@dataclass
class Decision:
    verdict: str  # "approve" | "reject" | "amend"
    approved_indices: list[int] | None = None  # amend: subset of plan.ops
    reason: str | None = None  # feeds the memory log


@dataclass
class CurationReport:
    plan: ReorgPlan
    decision: Decision | None  # None = expired
    applied: int
    failed: int
    skipped: int
```

## Human gate (sketch)

```python
@workflow.defn
class CurationWorkflow:
    @workflow.update
    def decide(self, decision: Decision) -> str: ...
    @decide.validator
    def _validate(self, decision: Decision) -> None:
        # verdict in {approve, reject, amend}; amend requires valid indices
    @workflow.query
    def current_plan(self) -> ReorgPlan | None: ...

    # in run():
    await workflow.wait_condition(
        lambda: self._decision is not None,
        timeout=timedelta(hours=self.input.approval_timeout_hours),
    )  # TimeoutError -> plan expired -> report and finish
```

Approval channel is decoupled from the workflow: anything that can call
`client.get_workflow_handle(...).execute_update("decide", Decision(...))`
works. V2 ships a CLI (`python -m app.starter decide approve`); Temporal UI
works too. Telegram/Slack/n8n buttons are a later, additive layer.

## New config (`app/config.py`)

```python
# --- Curation (daily librarian) ----------------------------------------------
CURATION_MODEL = os.environ.get("CURATION_MODEL", "openai:gpt-5-mini")
CURATION_MAX_OPS = int(os.environ.get("CURATION_MAX_OPS", "30"))
CURATION_APPROVAL_TIMEOUT_HOURS = float(
    os.environ.get("CURATION_APPROVAL_TIMEOUT_HOURS", "48")
)
CURATION_ALLOWED_OPS = [...]  # default: add_to_list, move_bookmark
CURATION_AUTO_APPLY_THRESHOLD = float(
    os.environ.get("CURATION_AUTO_APPLY_THRESHOLD", "1.1")
)  # >1 = never (V3)
CURATION_DIR = DATA_DIR / "curation"  # snapshots + curation-log.jsonl
```

## Phases

### V1 — report only (no writes to Karakeep)

The workflow runs steps 1–3, returns the validated `ReorgPlan` as its result,
and stops. No human gate, no apply. Purpose: judge proposal quality safely.

- models + snapshot + propose + validate
- `CurationWorkflow` (linear, no update handler yet)
- worker registration, `starter curate` (manual run), `starter curate-schedule`
- tests: models, validate, workflow happy path (mocked activities)
- **Exit criterion:** a week of daily plans that mostly look right.

### V2 — approve/reject, all-or-nothing

Adds the durable human gate and the apply step.

- `@workflow.update decide` + validator, `@workflow.query current_plan`
- 48h `wait_condition` timeout → expired plan, nothing applied
- `apply.py` + `apply_operations` activity (idempotent, per-op outcomes)
- `record_outcome` → `curation-log.jsonl`
- `starter decide approve|reject [--date …]` CLI
- tests: approve, reject, timeout, apply idempotence, replay determinism
- **Exit criterion:** one full cycle (propose → approve via CLI → applied in
  Karakeep) in production.

### V3 — amend + tiered autonomy

- `Decision(verdict="amend", approved_indices=[…])`: apply a subset
- auto-apply ops with `confidence >= CURATION_AUTO_APPLY_THRESHOLD` **and**
  a low-risk kind (`add_to_list`); everything else still waits
- unlock `create_list` (guard-railed), keep destructive ops out
- optional: notification webhook (Telegram/Slack/n8n) with the plan summary
  and buttons that hit a tiny endpoint calling `execute_update`
- **Exit criterion:** most days need zero or one human touch.

### V4 — feedback memory + evaluation

- `record_outcome` already logs every (op, decision, reason); inject a digest
  of recent *rejections* into the propose prompt ("the user refused to merge
  Recettes and Food")
- metrics from the log: approval rate over time, ops proposed vs applied —
  the agent's precision curve, visible in Logfire
- unlock `rename_list` / `merge_lists` once approval rate is consistently high
- **Exit criterion:** approval rate trending up across weeks.

## Testing strategy

Same stack as the sync: pure functions tested directly (`validate.py`,
snapshot digest), workflow paths under the time-skipping test environment
(updates + timers), replay test from a recorded history (extend
`scripts/generate_history.py`), coverage gate already enforced by CI.

## Out of scope (explicitly)

- Deleting bookmarks or lists — never proposed, never applied.
- Editing bookmark content (title/note/tags) — the sync owns enrichment.
- A web approval UI — CLI + Temporal UI first; chat buttons are V3 optional.
