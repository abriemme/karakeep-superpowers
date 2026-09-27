"""Start a sync manually or (re)create the recurring schedules.

Replaces the n8n triggers of the old scripts: the crons become Temporal
Schedules, and a manual run is one command instead of an HTTP call.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec
from temporalio.common import RetryPolicy

from app.config import TASK_QUEUE, TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE
from app.sync import IgSyncWorkflow, SyncInput
from app.youtube import YtSyncInput, YtSyncWorkflow

SCHEDULE_ID = "ig-to-karakeep-nightly"
YT_SCHEDULE_ID = "yt-to-karakeep-12h"


async def _client() -> Client:
    return await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)


async def sync(args: list[str]) -> None:
    backfill = "--backfill" in args
    reconcile = "--reconcile" in args
    handle = await (await _client()).start_workflow(
        IgSyncWorkflow.run,
        SyncInput(backfill=backfill, reconcile=reconcile),
        id=f"ig-sync-{'backfill' if backfill else 'run'}-{uuid.uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    print(f"started {handle.id}")
    print(await handle.result())


async def schedule(args: list[str]) -> None:
    client = await _client()
    action = ScheduleActionStartWorkflow(
        IgSyncWorkflow.run,
        SyncInput(),
        id="ig-sync-scheduled",
        task_queue=TASK_QUEUE,
        retry_policy=RetryPolicy(maximum_attempts=3),
    )
    handle = await client.create_schedule(
        SCHEDULE_ID,
        Schedule(
            action=action,
            spec=ScheduleSpec(cron_expressions=["0 3 * * *"]),
        ),
    )
    print(f"schedule {handle.id} created (03:00 daily)")


async def yt_sync(args: list[str]) -> None:
    full_sweep = "--full-sweep" in args
    handle = await (await _client()).start_workflow(
        YtSyncWorkflow.run,
        YtSyncInput(full_sweep=full_sweep),
        id=f"yt-sync-{'sweep' if full_sweep else 'run'}-{uuid.uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    print(f"started {handle.id}")
    print(await handle.result())


async def yt_schedule(args: list[str]) -> None:
    client = await _client()
    action = ScheduleActionStartWorkflow(
        YtSyncWorkflow.run,
        YtSyncInput(),
        id="yt-sync-scheduled",
        task_queue=TASK_QUEUE,
        retry_policy=RetryPolicy(maximum_attempts=3),
    )
    handle = await client.create_schedule(
        YT_SCHEDULE_ID,
        Schedule(
            action=action,
            # Same cadence as the n8n schedule trigger (every 12 h at :12).
            spec=ScheduleSpec(cron_expressions=["12 */12 * * *"]),
        ),
    )
    print(f"schedule {handle.id} created (every 12h at :12)")


async def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "sync"
    args = sys.argv[2:]
    if command == "sync":
        await sync(args)
    elif command == "schedule":
        await schedule(args)
    elif command == "yt":
        await yt_sync(args)
    elif command == "yt-schedule":
        await yt_schedule(args)
    else:
        sys.exit(f"unknown command: {command}")


if __name__ == "__main__":
    asyncio.run(main())
