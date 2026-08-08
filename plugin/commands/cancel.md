---
description: Disarm the enforced verification loop for this project
disable-model-invocation: true
---

The user wants to stop the enforced verification loop in this project. Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/disarm.py"
```

from the project root. Then tell the user what it printed, and remind them the
recording is not verified: the checks in the instrument skill's step 4 still
describe how to verify it by hand when they want to.
