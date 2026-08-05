# Changelog

## 0.0.4

- `flush()` reports batches the collector refused as unauthorized: the refused
  batch and every batch withheld after it turn `ok` off and count in `dropped`.
  They were previously reported as delivered.
- `convergent.__version__` reports the installed package's version.
