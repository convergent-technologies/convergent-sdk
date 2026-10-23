# Python

The package is `convergent-sdk`. Import it as `convergent`. Its docs are in
`python/docs/` of the public repository, next to the shared `docs/` folder.

## Find the installed version

```bash
python -m pip show convergent-sdk
```

Use the project's own tool when it has one, such as `uv pip show convergent-sdk`
or `poetry show convergent-sdk`. The version picks the docs tag: `python-v<version>`,
or `v<version>` for releases up to 0.0.11.

If the project does not have the package, add it with the project's package
manager, such as `uv add convergent-sdk` or `pip install convergent-sdk`.

## Language docs

`llms.txt` lists the Python docs under `python/docs/`. Read the integration
page before you add any span by hand. An integration that covers the executed
model client records the model spans for you.
