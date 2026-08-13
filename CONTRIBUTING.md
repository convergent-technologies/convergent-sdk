# Contributing

Issues and pull requests are welcome.

This repository is a mirror. Every file here is copied from an internal source
tree by a sync job, so a change merged here directly is overwritten by the next
sync. A check on every pull request enforces this, which is why an outside pull
request cannot pass CI here.

That does not mean your change is unwanted. Open the pull request and keep it
open. A maintainer will carry the change into the internal source with
Co-authored-by credit, and it will appear here in a following sync. The pull
request is then closed with a note saying which sync brought it in.

To work on the Python package locally:

    cd python
    uv sync
    uv run pytest
    uv run ruff check src tests
    uv run pyright
