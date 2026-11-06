from temporalio import workflow

import convergent  # noqa: F401 -- importing the SDK is the regression scenario.


@workflow.defn
class ImportsTheSdk:
    @workflow.run
    async def run(self) -> str:
        return "ok"
