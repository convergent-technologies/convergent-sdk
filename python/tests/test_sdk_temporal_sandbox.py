"""A sandboxed workflow import must tolerate SDK modules using restricted stdlib members."""

import asyncio
from collections.abc import AsyncIterator

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from _temporal_sdk_workflow import ImportsTheSdk


async def _empty_histories() -> AsyncIterator[WorkflowHistory]:
    if False:
        yield None


def test_sdk_imports_in_temporal_default_workflow_sandbox() -> None:
    """A customer's workflow file that imports the SDK loads under the default sandbox.

    Temporal's default restrictions refuse any use of ``urllib.request`` (and other
    stdlib members) while the sandbox re-imports the workflow's module, so an SDK
    module that subclasses or calls one at import time fails validation here.
    """

    async def replay() -> list[object]:
        replayer = Replayer(
            workflows=[ImportsTheSdk],
            workflow_runner=SandboxedWorkflowRunner(),
        )
        async with replayer.workflow_replay_iterator(_empty_histories()) as results:
            return [result async for result in results]

    assert asyncio.run(replay()) == []
