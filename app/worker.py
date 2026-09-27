"""Temporal worker using Worker Deployment Versioning.

The ``build_id`` comes from the git SHA (see ``app.config.get_build_id``). Each
build/deployment therefore exposes a new worker *version* under the same
``deployment_name``, which enables controlled rolling deployments on the server
side (traffic ramp, rollback, etc.).
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import (
    UnsandboxedWorkflowRunner,
    Worker,
    WorkerDeploymentConfig,
    WorkerDeploymentVersion,
)

from app.config import (
    DEPLOYMENT_NAME,
    TASK_QUEUE,
    TEMPORAL_ADDRESS,
    TEMPORAL_NAMESPACE,
    get_build_id,
)
from app.observability import setup_logfire
from app.sync import (
    IgSyncWorkflow,
    fetch_saved_page,
    load_seen,
    push_to_karakeep,
    save_seen,
)
from app.youtube import (
    YtSyncWorkflow,
    fetch_existing_video_ids,
    fetch_playlist_items,
    fetch_playlists,
    fetch_video_details,
    load_playlist_state,
    push_video,
    save_playlist_state,
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    setup_logfire()

    build_id = get_build_id()
    logging.info(
        "Starting worker deployment=%s build_id=%s task_queue=%s",
        DEPLOYMENT_NAME,
        build_id,
        TASK_QUEUE,
    )

    client = await Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[IgSyncWorkflow, YtSyncWorkflow],
        activities=[
            fetch_saved_page,
            load_seen,
            push_to_karakeep,
            save_seen,
            fetch_existing_video_ids,
            fetch_playlist_items,
            fetch_playlists,
            fetch_video_details,
            load_playlist_state,
            push_video,
            save_playlist_state,
        ],
        # pydantic-ai (LLM enrichment in the push activity) relies on
        # beartype, which monkey-patches the import machinery and is
        # incompatible with the workflow sandbox. The workflow code itself
        # stays deterministic (no I/O, no wall-clock), so running it
        # unsandboxed is safe.
        workflow_runner=UnsandboxedWorkflowRunner(),
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=DEPLOYMENT_NAME,
                build_id=build_id,
            ),
            # Enable Worker Deployment Versioning: the server routes tasks to the
            # right version based on the declared behavior.
            use_worker_versioning=True,
            # Safety net for workflows that don't explicitly declare their
            # ``versioning_behavior``.
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
    )

    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
