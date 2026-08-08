# Changelog

## 0.0.5b1

A pre-release. The package is unchanged from 0.0.4. The version moves so the
repository's instrument skill and plugin can be tested against a published
wheel without disturbing the release people install by default. A plain
`pip install convergent-sdk` still resolves to 0.0.4; `pip install
convergent-sdk==0.0.5b1` or `--pre` installs this one.

## 0.0.4

- `flush()` reports batches the collector refused as unauthorized: the refused
  batch and every batch withheld after it turn `ok` off and count in `dropped`.
  They were previously reported as delivered.
- `convergent.__version__` reports the installed package's version.
