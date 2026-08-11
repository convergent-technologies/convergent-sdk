---
description: Leave one verifier finding open on purpose, without another verification pass
disable-model-invocation: true
argument-hint: <criteria-name> <why you are leaving it>
---

The user has decided to leave a verifier finding as it is. Record it so the gate
stops holding the session for that finding. From the project root:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/waive.py" $ARGUMENTS
```

The first argument is the criteria file the finding came from, which the findings
block names in its second column. The rest is the reason, which the release
message quotes. Add `--match "<substring>"` where the criteria raised more than
one finding and only one is being waived.

Then tell the user what it printed. Append the same waiver to the ledger's pass
log with its reasoning and the condition that would make it no longer apply, so a
later reader knows what was accepted and when it should be revisited.

This does not make the recording verified. It records that a finding was left
open by a decision, and every release message names each waiver it applied.
