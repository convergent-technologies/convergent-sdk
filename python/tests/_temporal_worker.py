"""A local Temporal server and worker for SDK tests and replay fixtures.

It imports only temporalio, so the public repo's floor job can run the
interceptor at the ``temporal`` extra's oldest OpenTelemetry without pydantic-ai.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from temporalio.client import Client, Interceptor, Plugin
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

SANDBOX_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules("convergent")
)
# Other workflow errors fail at once instead of retrying until the timeout.
# The interceptor catches its own errors and falls back to temporalio's, so a
# broken one shows in the placement assertions, not as a failed task.
WORKFLOW_FAILURE_EXCEPTION_TYPES = (Exception,)


@asynccontextmanager
async def local_worker(
    *,
    task_queue: str,
    workflows: Sequence[type],
    activities: Sequence[Any] = (),
    interceptors: Sequence[Interceptor] = (),
    plugins: Sequence[Plugin] = (),
    **worker_options: Any,
) -> AsyncIterator[Client]:
    dev_server_path = os.environ.get("TEMPORAL_DEV_SERVER_PATH")
    # This file also runs in the public SDK repo, whose CI keeps start_local's download.
    in_monorepo_ci = os.environ.get("GITHUB_REPOSITORY") == "convergent-technologies/convergent"
    if not dev_server_path and in_monorepo_ci:
        pytest.fail(
            "TEMPORAL_DEV_SERVER_PATH must point to the installed Temporal dev server; "
            "run scripts/install-native-dev-services.sh --install-temporal first"
        )

    from temporalio.testing import WorkflowEnvironment

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=dev_server_path
    ) as env:
        client = await Client.connect(
            env.client.service_client.config.target_host,
            namespace=env.client.namespace,
            interceptors=list(interceptors),
            plugins=list(plugins),
        )
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=workflows,
            activities=activities,
            workflow_runner=SANDBOX_RUNNER,
            **worker_options,
        ):
            yield client
