---
name: coverage-plan-fidelity
description: The recording and the coverage plan describe the same app, item for item.
---

# Coverage plan fidelity

The coverage plan in the ledger names the entry point, every model call, every
tool, and every sub-agent, each with a file and line. These clauses judge
whether the recording carries the same app the plan describes.

## Every plan item recorded spans

Every entry point in the plan has an agent span. Every model call in the plan
has a model span. Every tool in the plan has an `execute_tool` span carrying
its name. Every sub-agent in the plan appears in the recording under its own
name. A plan item that recorded nothing is a finding, named with the file and
line the plan gives for it.

## Every span traces back to a plan item

Every agent name, model call, and tool in the recording corresponds to an item
in the plan. A span carrying work the plan never named means the plan was
incomplete, and the finding is on the plan: name the span and say what the plan
is missing.
