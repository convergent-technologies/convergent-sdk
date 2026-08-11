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

This clause judges the spans the app's own code opened, which are the ones from
the `convergent.sdk` scope. A library the app merely calls decides what its own
spans are and when they open, and the app cannot remove them without rewriting
private state on a span it does not own. Where a span from another scope carries
work the plan never named, report it as `ask` and not `fix`: name the span and
the scope that opened it, and say whether that library exposes a setting to stop
it. A recording is not held open for a span nobody can reach.

Content an integration's own docs page names as expected is not unplanned. For
every non-SDK scope in the recording, read `references/integrations-<scope>.md`
before judging its spans, and treat what that page lists as intended.
