"""Locate the Claude Code CLI.

Both `bridge.py` and `builder.py` shell out to Claude Code. Neither can rely on
`shutil.which("claude")` because launchd jobs get a minimal PATH, and neither can
hardcode the bundled path because the extension directory carries a version
number that changes on every Claude Code update (which is exactly how the
pinned `claude-code-2.1.89` path broke).

`resolve()` returns an argv prefix to run the CLI with, e.g.
`["/path/to/node", "/path/to/cli.js"]` or `["/usr/local/bin/claude"]`.
"""

import logging
import os
import shlex
import shutil
from pathlib import Path

LOG = logging.getLogger("devin_bridge")

DEVIN_SERVER_DIR = Path.home() / ".devin-server"
# Common install locations for the standalone `claude` binary, checked when the
# launchd PATH is too minimal for shutil.which to find it.
_BIN_FALLBACKS = (
    Path.home() / ".claude/local/claude",
    Path.home() / ".local/bin/claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
)

_cache = None


def _newest(paths):
    """Return the most recently modified existing path, or None."""
    existing = [p for p in paths if p.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def _nodes(node_env):
    """Yield node binaries to try running a cli.js with."""
    if node_env:
        yield node_env
    bundled = _newest(DEVIN_SERVER_DIR.glob("bin/*/node"))
    if bundled:
        yield str(bundled)
    on_path = shutil.which("node")
    if on_path:
        yield on_path


def _candidates():
    """Yield argv prefixes to try, best first."""
    cli_env = os.environ.get("CLAUDE_CLI")
    node_env = os.environ.get("CLAUDE_NODE")

    # 1. Explicit override. CLAUDE_CLI may be a full command
    # ("/path/to/node /path/to/cli.js"), a bare cli.js, or a `claude` binary.
    override = shlex.split(cli_env) if cli_env else []
    if len(override) > 1:
        yield override
    elif len(override) == 1:
        if override[0].endswith(".js"):
            for node in _nodes(node_env):
                yield [node, override[0]]
        else:
            yield override

    # 2. Newest bundled claude-code extension, run with whatever node we find.
    bundled_cli = _newest(
        DEVIN_SERVER_DIR.glob("extensions/anthropic.claude-code-*/resources/claude-code/cli.js")
    )
    if bundled_cli:
        for node in _nodes(node_env):
            yield [node, str(bundled_cli)]

    # 3. Standalone `claude` binary on PATH, then well-known install locations.
    on_path = shutil.which("claude")
    if on_path:
        yield [on_path]
    for candidate in _BIN_FALLBACKS:
        if candidate.is_file():
            yield [str(candidate)]


def resolve():
    """Return an argv prefix for invoking Claude Code, or None if unavailable."""
    global _cache
    if _cache and all(Path(part).exists() for part in _cache):
        return _cache
    for argv in _candidates():
        if all(Path(part).exists() for part in argv):
            _cache = argv
            LOG.info("Resolved Claude CLI: %s", " ".join(argv))
            return argv
    _cache = None
    return None


def unavailable_message():
    """Human-readable explanation for when resolve() returns None."""
    return (
        "Claude CLI not found. Looked for a bundled claude-code extension under "
        f"{DEVIN_SERVER_DIR}, a `claude` binary on PATH, and the usual install "
        "locations. Set CLAUDE_CLI (and CLAUDE_NODE) in secrets/devin.env to "
        "point at it explicitly."
    )
