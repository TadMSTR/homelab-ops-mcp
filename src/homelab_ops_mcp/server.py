"""homelab-ops-mcp — Shell and file access MCP server for the homelab ops agent.

Transport: streamable-http on 0.0.0.0:<port>/mcp

Exposes seven tools: run_command, read_file, read_multiple_files, write_file,
edit_file, read_directory, and list_processes. All server logic lives here; see
ARCHITECTURE.md for the layout.
"""

import contextlib
import difflib
import functools
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import psutil
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import telemetry
from .logging import configure_logging, tame_library_logging

log = configure_logging()

mcp = FastMCP(
    name="homelab-ops",
    instructions=(
        "Homelab operations server. Provides shell command execution, file system "
        "read/write, directory listing, and process inspection on the host."
    ),
)


# ---------------------------------------------------------------------------
# Environment sanitisation
# ---------------------------------------------------------------------------
# PM2 sets these variables in the process environment for its own IPC channel.
# If they leak into spawned children, any Node.js child (node, pnpm, tsc, or any
# node CLI) inherits a stray file descriptor and SIGABRTs during process
# teardown — 100% reproducible via run_command, 0% via a direct shell. Strip
# them before exec so shelled-out commands run in a clean environment. (HLOPS-1)
_PM2_IPC_ENV_VARS = (
    "NODE_CHANNEL_FD",
    "NODE_CHANNEL_SERIALIZATION_MODE",
    "NODE_UNIQUE_ID",
)


def _clean_env() -> dict:
    """Return a copy of the current environment with PM2 IPC vars stripped."""
    return {k: v for k, v in os.environ.items() if k not in _PM2_IPC_ENV_VARS}


# The variables a shelled-out child receives by default. This is an allowlist
# rather than a denylist deliberately: a denylist forwards everything nobody
# thought to name, so every key later added to the parent's environment reaches
# every child automatically and silently.
#
# The LC_* locale variables are enumerated rather than prefix-matched. They are
# a closed POSIX set, so enumerating costs nothing and means a future variable
# that merely starts with "LC_" cannot ride in on a pattern.
_BASE_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "TZ",
        "LC_ALL",
        "LC_ADDRESS",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_IDENTIFICATION",
        "LC_MEASUREMENT",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NAME",
        "LC_NUMERIC",
        "LC_PAPER",
        "LC_TELEPHONE",
        "LC_TIME",
    }
)

_ENFORCE_ENV_VAR = "SYSTEM_OPS_CHILD_ENV_ENFORCE"
_ALLOWLIST_ENV_VAR = "SYSTEM_OPS_CHILD_ENV_ALLOWLIST"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_GLOB_CHARS = "*?["


def _child_env_enforced() -> bool:
    """Whether the allowlist is enforced, or merely measured (the default)."""
    return os.environ.get(_ENFORCE_ENV_VAR, "").strip().lower() in _TRUTHY


def _child_env_allowlist() -> frozenset:
    """The base allowlist plus any exact names from ``SYSTEM_OPS_CHILD_ENV_ALLOWLIST``.

    Entries are exact variable names, comma-separated. Glob patterns are
    refused rather than matched, because a prefix would auto-promote every
    future key sharing it — which is precisely what the allowlist exists to
    prevent. A refused entry is logged by name (a name from local config, not a
    value) so a mistyped pattern surfaces instead of silently doing nothing.
    """
    extra = set()
    for entry in os.environ.get(_ALLOWLIST_ENV_VAR, "").split(","):
        name = entry.strip()
        if not name:
            continue
        if any(c in name for c in _GLOB_CHARS):
            log.warning("child_env.glob_rejected", entry=name)
            continue
        extra.add(name)
    return _BASE_CHILD_ENV_ALLOWLIST | frozenset(extra)


# ``$VAR`` and ``${VAR}``. The leading character class excludes ``$1``, ``$@``
# and friends, which are shell positionals rather than environment lookups.
_ENV_REF_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _referenced_withheld(command: str, allowed: frozenset) -> list:
    """Variable names the command reads that enforcement would take away.

    The withheld *count* is the same on every call — it describes the parent
    environment, not the command — so on its own it cannot say which callers
    enforcement would break. This can: it intersects what the command text
    actually references with what the allowlist would remove, which is the list
    an operator needs to build ``SYSTEM_OPS_CHILD_ENV_ALLOWLIST``.

    Names only. The values are never read, and a name that is not set in the
    parent environment is not reported, since withholding it changes nothing.

    Deliberately approximate: a ``$VAR`` inside single quotes is counted even
    though the shell would not expand it, and an indirect read such as
    ``env | grep FOO`` is missed entirely. It over-reports rather than
    under-reports, which is the right way round for a signal used to decide
    whether it is safe to enforce.
    """
    refs = {m.group(1) for m in _ENV_REF_RE.finditer(command)}
    return sorted(r for r in refs if r in os.environ and r not in allowed)


def _child_env() -> tuple[dict, int]:
    """Return the child's environment, and how many variables were withheld.

    In shadow mode — the default — the full parent environment is returned but
    the withheld count is still computed, so the blast radius of enforcement can
    be measured before enforcement is switched on. The count means the same
    thing in both modes: how many variables the allowlist excludes.

    The PM2 IPC variables are dropped before the allowlist is applied, so they
    stay out in both modes and cannot be re-added via
    ``SYSTEM_OPS_CHILD_ENV_ALLOWLIST`` (HLOPS-1).
    """
    parent = _clean_env()
    allowed = _child_env_allowlist()
    kept = {k: v for k, v in parent.items() if k in allowed}
    withheld = len(parent) - len(kept)
    return (kept if _child_env_enforced() else parent), withheld


def _instrumented(fn):
    """Time every call to a tool and record its outcome as telemetry.

    These tools report failure by *returning* ``{"error": ...}`` rather than by
    raising, so the wrapper inspects the returned value. Counting only
    exceptions would report a 0% error rate for a tool failing every call.

    The label is a fixed ``tool_error`` rather than the message: the messages
    embed paths and command text, which is both unbounded cardinality for a
    metrics backend and content that has no business leaving the host.

    A non-zero ``exit_code`` from ``run_command`` is deliberately not an error.
    The command failed; the tool worked.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with telemetry.record_tool_call(fn.__name__) as outcome:
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("error"):
                outcome["error"] = "tool_error"
            return result

    return wrapper


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------
class PathValidationError(ValueError):
    """Raised when a tool is handed a path it must refuse to act on."""


def _resolve_path(path: str) -> Path:
    """Validate ``path`` as absolute and return it as a ``Path``.

    Every file tool's docstring already promises an absolute path, but nothing
    enforced it. ``Path("~/x")`` is *not* absolute — Python does no tilde
    expansion — so it resolved against the process cwd, silently creating a
    literal ``~`` directory under the server's working directory and returning
    success. Rejecting enforces the documented contract; expanding would quietly
    change it. The message names the offending path so a calling agent can
    self-correct in one turn.
    """
    p = Path(path)
    if not p.is_absolute():
        raise PathValidationError(f"path must be absolute, got {path!r}")
    # Backstop, independent of the check above: never act on a path whose own
    # components include a literal ``~``. An absolute path can still carry one.
    if "~" in p.parts:
        raise PathValidationError(f"path component '~' is not allowed, got {path!r}")
    return p


def _atomic_write(path: Path, content: str) -> int:
    """Write ``content`` to ``path`` atomically and return the byte count.

    Writes to a temp file in the same directory (so ``os.replace`` stays on one
    filesystem), then renames over the target — a crash mid-write leaves either
    the old file or the new one, never a truncated mix. The mode of an existing
    target is preserved; new files keep the secure 0600 mode from ``mkstemp``.
    """
    data = content.encode()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        with contextlib.suppress(OSError):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(data)


# ---------------------------------------------------------------------------
# Bounded output capture
# ---------------------------------------------------------------------------
_OUTPUT_LIMIT_ENV_VAR = "SYSTEM_OPS_OUTPUT_LIMIT_BYTES"
_DEFAULT_OUTPUT_LIMIT = 1024 * 1024  # 1 MiB per stream
_READ_CHUNK = 65536


def _output_limit() -> int:
    """Per-stream capture cap in bytes, from the environment or the default."""
    raw = os.environ.get(_OUTPUT_LIMIT_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_OUTPUT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        log.warning("output_limit.invalid", value=raw)
        return _DEFAULT_OUTPUT_LIMIT
    if value <= 0:
        log.warning("output_limit.invalid", value=raw)
        return _DEFAULT_OUTPUT_LIMIT
    return value


def _run_capped(command: str, cwd: str, env: dict, timeout: int, limit: int) -> tuple:
    """Run ``command`` under bash, capturing at most ``limit`` bytes per stream.

    ``subprocess.run(capture_output=True)`` reads both pipes to EOF with no
    bound, so one ``cat`` of a large file lands whole in this process's memory
    and then whole in the caller's context. Read incrementally instead and stop
    the process as soon as either stream passes the cap.

    Returns ``(stdout, stderr, exit_code, truncated, timed_out)``. On a cap
    breach or a timeout the process group is killed and reaped before returning,
    so nothing is left behind.

    The child gets its own session, so the kill reaches the whole pipeline
    rather than just the ``bash`` that spawned it. Without that, killing bash
    leaves its children running — which was already true of the timeout path
    when it used ``subprocess.run``.
    """
    proc = subprocess.Popen(
        ["bash", "-c", command],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    truncated = False
    timed_out = False
    deadline = time.monotonic() + timeout

    sel = selectors.DefaultSelector()
    try:
        for stream in buffers:
            sel.register(stream, selectors.EVENT_READ)
        while sel.get_map() and not truncated:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in sel.select(timeout=min(remaining, 0.2)):
                stream = key.fileobj
                chunk = os.read(stream.fileno(), _READ_CHUNK)
                if not chunk:
                    sel.unregister(stream)
                    continue
                buf = buffers[stream]
                if len(buf) < limit:
                    buf.extend(chunk[: limit - len(buf)])
                if len(buf) >= limit:
                    truncated = True
    finally:
        sel.close()

    if truncated or timed_out:
        _kill_group(proc)
    exit_code = proc.wait()
    for stream in buffers:
        stream.close()

    return (
        buffers[proc.stdout].decode(errors="replace"),
        buffers[proc.stderr].decode(errors="replace"),
        exit_code,
        truncated,
        timed_out,
    )


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group, tolerating an already-dead child."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


def _mark_truncated(text: str, limit: int) -> str:
    """Append an explicit marker so truncation is distinguishable from brevity."""
    return f"{text}\n[truncated: output reached the {limit}-byte per-stream cap]"


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        idempotent_hint=False,
        destructive_hint=True,
        # The only tool here that can reach the network: it runs arbitrary
        # shell, so curl, git push and apt are all inside its envelope.
        open_world_hint=True,
    )
)
@_instrumented
def run_command(
    command: str,
    cwd: str | None = None,
    timeout: int = 30,
) -> dict:
    """Execute a shell command and return stdout, stderr, and exit code.

    Args:
        command: Shell command to run (executed via bash -c).
        cwd: Working directory for the command. Defaults to the home directory.
        timeout: Max seconds to wait before killing the process (default 30).

    Each stream is captured up to SYSTEM_OPS_OUTPUT_LIMIT_BYTES (default 1 MiB).
    If either reaches the cap the process is killed and ``truncated`` is true,
    so a capped result is distinguishable from a genuinely short one.

    Every response carries ``execution_time``, the wall-clock seconds the
    command took.
    """
    working_dir = cwd or str(Path.home())
    # Only the working directory is validated. Tilde paths *inside* ``command``
    # are fine — those go through bash, which expands them correctly.
    try:
        _resolve_path(working_dir)
    except PathValidationError as e:
        log.warning("run_command.invalid_cwd", cwd=working_dir)
        return {"stdout": "", "stderr": f"cwd: {e}", "exit_code": -1}
    # Of the environment itself only the count is logged — never a name, never
    # a value. The separate `at_risk` list holds names the *caller* wrote into
    # the command text, which is caller-authored config rather than a secret.
    child_env, withheld = _child_env()
    enforced = _child_env_enforced()
    at_risk = _referenced_withheld(command, _child_env_allowlist())
    if at_risk:
        log.info(
            "run_command.env_referenced_withheld",
            cwd=working_dir,
            names=at_risk,
            enforced=enforced,
        )
    # Command text is sensitive; log it at DEBUG only.
    log.debug("run_command.start", cwd=working_dir, timeout=timeout, command=command)
    limit = _output_limit()
    started = time.perf_counter()
    try:
        stdout, stderr, exit_code, truncated, timed_out = _run_capped(
            command, working_dir, child_env, timeout, limit
        )
        elapsed = round(time.perf_counter() - started, 4)
        if timed_out:
            log.warning(
                "run_command.timeout",
                cwd=working_dir,
                timeout=timeout,
                env_withheld_count=withheld,
                env_enforced=enforced,
            )
            return {
                "stdout": stdout,
                "stderr": f"Command timed out after {timeout} seconds.",
                "exit_code": -1,
                "truncated": truncated,
                "execution_time": elapsed,
            }
        if truncated:
            if len(stdout.encode(errors="replace")) >= limit:
                stdout = _mark_truncated(stdout, limit)
            if len(stderr.encode(errors="replace")) >= limit:
                stderr = _mark_truncated(stderr, limit)
            log.warning(
                "run_command.truncated",
                cwd=working_dir,
                limit_bytes=limit,
                env_withheld_count=withheld,
                env_enforced=enforced,
            )
        log.info(
            "run_command.done",
            cwd=working_dir,
            exit_code=exit_code,
            env_withheld_count=withheld,
            env_enforced=enforced,
        )
        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "truncated": truncated,
            "execution_time": elapsed,
        }
    except Exception as e:
        log.error(
            "run_command.error",
            cwd=working_dir,
            error=str(e),
            env_withheld_count=withheld,
            env_enforced=enforced,
        )
        return {
            "stdout": "",
            "stderr": str(e),
            "exit_code": -1,
            "truncated": False,
            "execution_time": round(time.perf_counter() - started, 4),
        }


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_instrumented
def read_file(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict:
    """Read a file and return its contents, with optional line range.

    Args:
        path: Absolute path to the file.
        start_line: First line to return (1-indexed, inclusive). Omit for start of file.
        end_line: Last line to return (1-indexed, inclusive). Omit for end of file.
    """
    try:
        p = _resolve_path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        if not p.is_file():
            return {"error": f"Path is not a file: {path}"}

        lines = p.read_text(errors="replace").splitlines(keepends=True)
        total = len(lines)

        if start_line is not None or end_line is not None:
            sl = (start_line - 1) if start_line else 0
            el = end_line if end_line else total
            sl = max(0, sl)
            el = min(total, el)
            selected = lines[sl:el]
            log.info("read_file.done", path=path, total_lines=total, ranged=True)
            return {
                "path": path,
                "total_lines": total,
                "returned_lines": f"{sl + 1}-{el}",
                "content": "".join(selected),
            }

        log.info("read_file.done", path=path, total_lines=total, ranged=False)
        return {"path": path, "total_lines": total, "content": "".join(lines)}
    except PathValidationError as e:
        log.warning("read_file.invalid_path", path=path)
        return {"error": str(e)}
    except PermissionError:
        log.warning("read_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        log.error("read_file.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# read_multiple_files
# ---------------------------------------------------------------------------
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_instrumented
def read_multiple_files(paths: list[str]) -> dict:
    """Read several files in one call. Returns a result per path.

    One failing path does not fail the others — each entry is either a file
    result or an ``error``, so a partial read is still useful.

    Args:
        paths: Absolute paths to read.
    """
    results = {}
    for path in paths:
        results[path] = read_file(path)
    ok = sum(1 for r in results.values() if "error" not in r)
    log.info("read_multiple_files.done", count=len(paths), ok=ok)
    return {"count": len(paths), "ok": ok, "failed": len(paths) - ok, "files": results}


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        # Writing the same content twice leaves the same file.
        idempotent_hint=True,
        destructive_hint=True,
        open_world_hint=False,
    )
)
@_instrumented
def write_file(
    path: str,
    content: str,
    create_dirs: bool = True,
) -> dict:
    """Write (overwrite) a file at the given path.

    Args:
        path: Absolute path to the file.
        content: Full content to write. Replaces any existing content.
        create_dirs: If True, create parent directories if they don't exist (default True).
    """
    try:
        p = _resolve_path(path)
        if create_dirs:
            p.parent.mkdir(parents=True, exist_ok=True)
        written = _atomic_write(p, content)
        log.info("write_file.done", path=path, bytes_written=written)
        return {"path": path, "bytes_written": written}
    except PathValidationError as e:
        log.warning("write_file.invalid_path", path=path)
        return {"error": str(e)}
    except PermissionError:
        log.warning("write_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        log.error("write_file.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# edit_file support
# ---------------------------------------------------------------------------
# Searching every window of a very large file is not worth the wall time, and
# the caller is better served by a fast "not found" than a slow near-miss.
_CLOSEST_MATCH_MAX_BYTES = 2 * 1024 * 1024
# Below this similarity the "closest" window is noise and quoting it misleads.
_CLOSEST_MATCH_MIN_RATIO = 0.5
_DIFF_MAX_LINES = 40


def _closest_match(content: str, old_str: str) -> dict | None:
    """Find the passage most like ``old_str`` and diff it against the request.

    A failed edit previously returned only a match count, which tells the caller
    that something is wrong but not what. Faced with that, an agent tends to
    give up on editing and rewrite the whole file — which is a much larger and
    riskier write than the edit it was trying to make.

    Returns None when the file is too large to scan or nothing resembles
    ``old_str`` closely enough to be worth quoting.
    """
    if len(content) > _CLOSEST_MATCH_MAX_BYTES:
        return None

    lines = content.splitlines(keepends=True)
    old_lines = old_str.splitlines(keepends=True) or [""]
    span = len(old_lines)
    if not lines:
        return None

    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq1(old_str)
    best_ratio, best_index, best_text = 0.0, 0, ""
    for i in range(max(1, len(lines) - span + 1)):
        window = "".join(lines[i : i + span])
        matcher.set_seq2(window)
        # quick_ratio is an upper bound, so a window that cannot beat the
        # current best is skipped without the quadratic comparison.
        if matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio, best_index, best_text = ratio, i, window

    if best_ratio < _CLOSEST_MATCH_MIN_RATIO:
        return None

    diff = list(difflib.ndiff(old_lines, best_text.splitlines(keepends=True)))
    truncated = len(diff) > _DIFF_MAX_LINES
    if truncated:
        diff = [*diff[:_DIFF_MAX_LINES], f"... {len(diff) - _DIFF_MAX_LINES} more diff lines"]

    return {
        "closest_match": best_text,
        "closest_match_line": best_index + 1,
        "similarity": round(best_ratio, 3),
        # ndiff marks changed characters on its "?" lines, so this shows which
        # characters differ, not merely which lines.
        "diff": "".join(line if line.endswith("\n") else line + "\n" for line in diff),
    }


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations=ToolAnnotations(
        read_only_hint=False,
        # Not idempotent: the second call finds no match and fails.
        idempotent_hint=False,
        destructive_hint=True,
        open_world_hint=False,
    )
)
@_instrumented
def edit_file(
    path: str,
    old_str: str,
    new_str: str,
    dry_run: bool = False,
) -> dict:
    """Find-and-replace edit: replace old_str with new_str in a file.

    old_str must match exactly once. Fails if zero or multiple matches found.
    When no match is found, the result carries the closest passage in the file,
    its line number, and a diff against what was asked for.

    Args:
        path: Absolute path to the file.
        old_str: Exact string to find (must match exactly once).
        new_str: Replacement string.
        dry_run: If True, report what the edit would do and write nothing.
    """
    try:
        p = _resolve_path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}

        original = p.read_text(errors="replace")
        count = original.count(old_str)

        if count == 0:
            result = {"error": "old_str not found in file. No changes made."}
            hint = _closest_match(original, old_str)
            if hint:
                result.update(hint)
            log.info("edit_file.no_match", path=path, had_hint=bool(hint))
            return result
        if count > 1:
            return {
                "error": f"old_str matched {count} times. Must match exactly once. No changes made."
            }

        updated = original.replace(old_str, new_str, 1)
        if dry_run:
            log.info("edit_file.dry_run", path=path, matches_replaced=1)
            return {
                "path": path,
                "status": "ok",
                "matches_replaced": 1,
                "dry_run": True,
                "bytes_before": len(original.encode()),
                "bytes_after": len(updated.encode()),
            }
        _atomic_write(p, updated)
        log.info("edit_file.done", path=path, matches_replaced=1)
        return {"path": path, "status": "ok", "matches_replaced": 1, "dry_run": False}
    except PathValidationError as e:
        log.warning("edit_file.invalid_path", path=path)
        return {"error": str(e)}
    except PermissionError:
        log.warning("edit_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        log.error("edit_file.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# read_directory
# ---------------------------------------------------------------------------
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_instrumented
def read_directory(
    path: str,
    recursive: bool = False,
    max_depth: int = 2,
) -> dict:
    """List contents of a directory.

    Args:
        path: Absolute path to the directory.
        recursive: If True, recurse into subdirectories up to max_depth.
        max_depth: Maximum recursion depth when recursive=True (default 2).
    """
    try:
        p = _resolve_path(path)
        if not p.exists():
            return {"error": f"Path not found: {path}"}
        if not p.is_dir():
            return {"error": f"Path is not a directory: {path}"}

        def _list(directory: Path, depth: int) -> list:
            entries = []
            try:
                for item in sorted(directory.iterdir()):
                    entry = {
                        "name": item.name,
                        "type": "dir" if item.is_dir() else "file",
                        "path": str(item),
                    }
                    if item.is_file():
                        with contextlib.suppress(Exception):
                            entry["size_bytes"] = item.stat().st_size
                    if recursive and item.is_dir() and depth < max_depth:
                        entry["children"] = _list(item, depth + 1)
                    entries.append(entry)
            except PermissionError:
                entries.append({"name": str(directory), "error": "permission denied"})
            return entries

        entries = _list(p, 1)
        log.info("read_directory.done", path=path, count=len(entries), recursive=recursive)
        return {"path": path, "entries": entries, "count": len(entries)}
    except PathValidationError as e:
        log.warning("read_directory.invalid_path", path=path)
        return {"error": str(e)}
    except Exception as e:
        log.error("read_directory.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# list_processes
# ---------------------------------------------------------------------------
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
@_instrumented
def list_processes(
    sort_by: str = "cpu",
    limit: int = 30,
    name_filter: str | None = None,
) -> dict:
    """List running processes with PID, name, CPU%, and memory%.

    Args:
        sort_by: Sort field — "cpu", "mem", or "pid" (default "cpu").
        limit: Max number of processes to return (default 30).
        name_filter: Optional substring to filter process names (case-insensitive).
    """
    try:
        procs = []
        attrs = ["pid", "name", "cpu_percent", "memory_percent", "status"]
        for proc in psutil.process_iter(attrs):
            try:
                info = proc.info
                if name_filter and name_filter.lower() not in (info["name"] or "").lower():
                    continue
                procs.append(
                    {
                        "pid": info["pid"],
                        "name": info["name"],
                        "cpu_percent": round(info["cpu_percent"] or 0.0, 2),
                        "mem_percent": round(info["memory_percent"] or 0.0, 3),
                        "status": info["status"],
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        sort_key = {"cpu": "cpu_percent", "mem": "mem_percent", "pid": "pid"}.get(
            sort_by, "cpu_percent"
        )
        procs.sort(key=lambda x: x[sort_key], reverse=(sort_by != "pid"))
        log.debug("list_processes.done", count=len(procs[:limit]), sort_by=sort_by)
        return {"count": len(procs[:limit]), "sort_by": sort_by, "processes": procs[:limit]}
    except Exception as e:
        log.error("list_processes.error", error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Console entry point — parse CLI args and start the streamable-http server."""
    import argparse

    parser = argparse.ArgumentParser(description="homelab-ops MCP server")
    # Default to loopback: this server offers unauthenticated arbitrary shell exec
    # and filesystem access, so network isolation is the only control. Pass
    # --host 0.0.0.0 explicitly to expose it (e.g. for container reachability via
    # host.docker.internal). See SECURITY.md / audit homelab-ops-mcp-v02 (MEDIUM).
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1; pass 0.0.0.0 to expose to containers)",
    )
    parser.add_argument("--port", type=int, default=8282, help="Bind port (default: 8282)")
    parser.add_argument("--path", default="/mcp", help="HTTP path (default: /mcp)")
    args = parser.parse_args()

    tame_library_logging()
    telemetry.init()
    log.info("server.start", host=args.host, port=args.port, path=args.path)
    mcp.run(  # pragma: no cover
        transport="streamable-http",
        # 18 lines of ASCII-art banner per start, on a headless service whose
        # log is read with grep.
        show_banner=False,
        host=args.host,
        port=args.port,
        path=args.path,
        uvicorn_config={
            # Drop the per-request access line — see tame_library_logging().
            "access_log": False,
            # log_config=None stops uvicorn running dictConfig, which would
            # install its own handlers with propagate=False and put its records
            # beyond reach of the JSON formatter on the root logger. With it
            # unset, uvicorn's records propagate and come out as JSON.
            "log_config": None,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    main()
