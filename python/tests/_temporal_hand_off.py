"""Shared Temporal hand-off setup for SDK tests and replay fixtures.

The workflow agent runs through pydantic-ai's Temporal integration, so its model
calls and its tool run as activities, and the tool runs the specialist agent.
pydantic-ai builds a durable agent's activities when the agent is built, so the
agents live at module level. Tests import this module lazily because CI collects
them on an OpenTelemetry release pydantic-ai cannot import.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow, TemporalDurability
from pydantic_ai.models.test import TestModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy


async def execute_hand_off(client: Client, *, id: str, task_queue: str) -> None:
    # The backstop if the workflow task still retries.
    await client.execute_workflow(
        HandOffWorkflow.run,
        id=id,
        task_queue=task_queue,
        execution_timeout=timedelta(seconds=30),
    )


specialist_agent = Agent(TestModel(), name="specialist-agent")


async def ask_specialist(question: str) -> str:
    """Ask the specialist agent."""
    return (await specialist_agent.run(question, conversation_id="specialist")).output


workflow_agent = Agent(
    TestModel(call_tools=["ask_specialist"]),
    name="workflow-agent",
    tools=[ask_specialist],
    capabilities=[
        TemporalDurability(
            activity_config={
                "start_to_close_timeout": timedelta(seconds=30),
                # Temporal retries a failed activity forever by default.
                "retry_policy": RetryPolicy(maximum_attempts=1),
            }
        )
    ],
)


@workflow.defn
class HandOffWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = (workflow_agent,)

    @workflow.run
    async def run(self) -> str:
        # pydantic-ai records the conversation id as the session. Its default
        # comes from the clock or the run id, so the replay recordings fix it.
        result = await workflow_agent.run("hand off to the specialist", conversation_id="hand-off")
        return result.output
