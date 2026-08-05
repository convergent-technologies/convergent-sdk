# Contributing

Issues and pull requests are welcome.

To work on the Python package:

    cd python
    uv sync
    uv run pytest
    uv run ruff check src tests
    uv run pyright
