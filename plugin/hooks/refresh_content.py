#!/usr/bin/env python3
"""SessionStart hook: refresh the skill content from the public repository.

The plugin ships code; the instructions it enforces are content, fetched from
the repository's `stable` ref at session start and cached under the plugin
data directory. One conditional request decides whether anything changed, a
changed manifest pulls the files it lists, and every failure falls back to the
cache, then to the snapshot built into the plugin. Fetched files are data:
this hook writes them to the cache and never executes them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://raw.githubusercontent.com/convergent-technologies/convergent-sdk/stable/skills/instrument/"
TIMEOUT = 3.0
MAX_FILES = 64
MAX_FILE_BYTES = 2_000_000

#: Bundle paths are relative, shallow, and plainly named, or they are refused.
SAFE_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*){0,3}$")

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))


def data_dir() -> Path:
    configured = os.environ.get("CLAUDE_PLUGIN_DATA")
    if configured:
        return Path(configured)
    return Path.home() / ".claude" / "plugins" / "data" / "convergent-instrument"


def _get(url: str, etag: str | None = None) -> tuple[int, bytes, str | None]:
    request = urllib.request.Request(url, headers={"User-Agent": "convergent-instrument-plugin"})
    if etag:
        request.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read(MAX_FILE_BYTES + 1), response.headers.get("ETag")
    except urllib.error.HTTPError as error:
        return error.code, b"", None


def refresh(base_url: str = BASE_URL, cache_root: Path | None = None) -> bool:
    """Bring the cache up to date. Returns True when content changed.

    Raises on any network or validation problem; the caller treats every
    raise as "keep what we have".
    """
    cache_root = cache_root if cache_root is not None else data_dir()
    cache = cache_root / "content"
    meta_path = cache_root / "content-meta.json"
    old_etag = None
    # The stored tag describes the cache. Sending it with no cache to describe
    # earns a 304 that leaves the plugin on its snapshot for good.
    if (cache / "manifest.json").is_file():
        try:
            old_etag = json.loads(meta_path.read_text(encoding="utf-8")).get("etag")
        except (OSError, ValueError):
            pass

    status, manifest_bytes, etag = _get(base_url + "manifest.json", old_etag)
    if status == 304:
        return False
    if status != 200:
        raise RuntimeError(f"manifest fetch returned {status}")
    manifest = json.loads(manifest_bytes)
    try:
        if manifest_bytes.decode("utf-8") == (cache / "manifest.json").read_text(encoding="utf-8"):
            _write_meta(meta_path, etag)
            return False
    except (OSError, ValueError):
        pass

    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise RuntimeError("manifest names no usable file list")
    staging = cache_root / "content.staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    (staging / "manifest.json").write_bytes(manifest_bytes)
    for name in files:
        if not isinstance(name, str) or not SAFE_PATH.match(name):
            raise RuntimeError(f"manifest names an unsafe path: {name!r}")
        status, body, _ = _get(base_url + name)
        if status != 200 or len(body) > MAX_FILE_BYTES:
            raise RuntimeError(f"fetch of {name} returned {status}")
        target = staging / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    previous = cache_root / "content.previous"
    shutil.rmtree(previous, ignore_errors=True)
    if cache.exists():
        cache.rename(previous)
    staging.rename(cache)
    shutil.rmtree(previous, ignore_errors=True)
    _write_meta(meta_path, etag)
    return True


def _write_meta(meta_path: Path, etag: str | None) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"etag": etag}) + "\n", encoding="utf-8")


def plugin_version() -> str:
    try:
        manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        return str(manifest.get("version", "0"))
    except (OSError, ValueError):
        return "0"


def version_warning(content_dir: Path, version: str) -> str | None:
    """A warning when the content asks for a newer plugin than this one."""
    try:
        manifest = json.loads((content_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    required = str(manifest.get("requires-plugin", ""))
    match = re.match(r"^>=\s*(\d+)\.(\d+)$", required)
    if not match:
        return None
    needed = (int(match.group(1)), int(match.group(2)))
    have = tuple(int(part) for part in re.findall(r"\d+", version)[:2]) or (0,)
    if tuple(have) >= needed:
        return None
    return (
        f"convergent-instrument: the skill content asks for plugin {required} and this "
        f"plugin is {version}; update the plugin to keep enforcement and instructions aligned."
    )


def main() -> int:
    changed = False
    try:
        changed = refresh()
    except Exception:  # noqa: BLE001 - offline or a bad fetch means keep the cache
        pass

    cache = data_dir() / "content"
    content_dir = cache if (cache / "manifest.json").is_file() else PLUGIN_ROOT / "content-snapshot"
    response: dict = {}
    warning = version_warning(content_dir, plugin_version())
    if warning:
        response["systemMessage"] = warning
    if changed:
        response["hookSpecificOutput"] = {"hookEventName": "SessionStart", "reloadSkills": True}
    if response:
        print(json.dumps(response))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
