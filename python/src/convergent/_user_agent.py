"""The User-Agent on every request the SDK sends, so the server can tell clients apart."""

from ._version import sdk_version

USER_AGENT = f"convergent-sdk-python/{sdk_version()}"
