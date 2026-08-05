"""The package must work under a second top-level name.

The same directory can ship under another import name for a consumer that
already owns ``convergent``. That only holds
while every import inside the package is relative -- one absolute ``convergent.``
import would break the repackaged artifact and nothing else would notice.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import convergent

_PACKAGE = Path(convergent.__file__).parent
#: Copying __pycache__ would let the child run bytecode compiled under the other
#: name. The cache is keyed on source mtime and size, so an edit that changes
#: neither -- which the mutation these tests guard against does not -- is invisible.
_IGNORE = shutil.ignore_patterns("__pycache__")


def test_no_absolute_self_imports(tmp_path: Path) -> None:
    """The mechanism, checked directly: an absolute self-import would survive the
    copy below only to fail for a consumer whose own package owns the name."""
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted(_PACKAGE.glob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if line.lstrip().startswith(("from convergent", "import convergent"))
    ]
    assert not offenders, "absolute self-imports break the repackaged artifact:\n" + "\n".join(
        offenders
    )


def test_imports_under_a_different_top_level_name(tmp_path: Path) -> None:
    """The guarantee itself: mounted as convergent_sdk, the package imports and
    exposes the same surface."""
    shutil.copytree(_PACKAGE, tmp_path / "convergent_sdk", ignore=_IGNORE)
    script = textwrap.dedent(
        """
        import json
        import convergent_sdk
        print(json.dumps(convergent_sdk.__all__))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("]")
    import json

    assert json.loads(result.stdout.strip().splitlines()[-1]) == list(convergent.__all__)


def test_public_submodules_resolve_within_the_repackaged_name(tmp_path: Path) -> None:
    """The same guarantee for ``convergent.otel``, the public submodule.

    An absolute ``convergent.otel`` import there does not fail: it finds whatever
    else owns the ``convergent`` name and hands that back, which is a second copy
    of this code on its own module globals. So assert where the submodule came
    from, not merely that it loaded.
    """
    shutil.copytree(_PACKAGE, tmp_path / "convergent_sdk", ignore=_IGNORE)
    script = textwrap.dedent(
        """
        import convergent_sdk.otel
        print(convergent_sdk.otel.__name__)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["convergent_sdk.otel"]
