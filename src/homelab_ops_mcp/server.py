"""homelab-ops-mcp — Shell and file access MCP server for the homelab ops agent.

Transport: streamable-http on 127.0.0.1:<port>/mcp by default. The bind host is
loopback unless ``--host`` says otherwise; see ``main()`` and SECURITY.md.

Exposes seven tools: run_command, read_file, read_multiple_files, write_file,
edit_file, read_directory, and list_processes. All server logic lives here; see
ARCHITECTURE.md for the layout.

SECURITY[accepted]: tools return the underlying error text (``str(e)``, OS error
strings, offending paths) rather than a generic message. That is normally a
disclosure concern (OE-02), and it is deliberate here. This server's entire
contract is arbitrary shell execution and unrestricted filesystem access for the
caller, so there is nothing an error message can disclose that the caller cannot
read directly with the tools themselves. Suppressing the detail would cost real
diagnosability and buy no confidentiality. The controls that do the work are the
loopback bind and the network boundary, not the error text.

The environment is the exception to that, and it is handled separately: the
withheld-variable logging records counts only, never a name and never a value,
and the referenced-variable log carries only names the caller itself wrote into
the command text. Pinned by test.
"""

import codecs
import contextlib
import difflib
import errno
import functools
import logging
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path

import psutil
from fastmcp import FastMCP
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.middleware import Middleware
from starlette.requests import HTTPConnection

from . import telemetry
from .logging import configure_logging, tame_library_logging
from .verbclass import classify_verb

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


class LinkedFileError(OSError):
    """Raised when the target has other hard links that a rename would detach."""


class UnsafeLinkError(PermissionError):
    """Raised for a symlink the kernel's ``protected_symlinks`` rule would not follow."""


_MAX_LINK_HOPS = 40  # the kernel's own limit (ELOOP)


def _link_hop_allowed(link_uid: int, dir_mode: int, dir_uid: int, euid: int) -> bool:
    """The ``fs.protected_symlinks`` rule, for one link.

    In a sticky, world-writable directory (``/tmp``), a link is followed only if
    the follower owns it or the link's owner owns the directory. Anyone can
    plant a link in such a directory, so without this rule a write to
    ``/tmp/report.txt`` (or ``/tmp/somedir/report.txt``) could be steered into
    any file the server can write.
    """
    shared = dir_mode & stat.S_ISVTX and dir_mode & stat.S_IWOTH
    return not shared or link_uid in (euid, dir_uid)


def _checked_resolve(path: Path) -> Path:
    """Resolve ``path`` one component at a time, applying the link rule to every link met.

    ``os.path.realpath`` follows links in userspace, so the kernel's
    ``protected_symlinks`` check never runs on them, and a host may have it off
    anyway. Every symlink on the way, in a directory component or the final
    one and at every hop of a chain, is checked here instead. A component that
    does not exist ends the walk: nothing below it can be a link, so the rest
    is joined lexically.

    This is a check, not the resolution the write uses. The write then opens
    the resolved directory through the kernel (``_open_dir``), so a link
    swapped in after this walk is still subject to the kernel's rule, and the
    final component is handled relative to that directory without following
    links.
    """
    parts = list(path.parts[1:])
    resolved = Path("/")
    hops = 0
    while parts:
        name = parts.pop(0)  # Path.parts never yields "" or "."
        if name == "..":
            resolved = resolved.parent
            continue
        candidate = resolved / name
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            # Nothing below a missing component can be a link, so the rest is
            # joined as is. A ".." in it is refused, as the kernel would
            # (ENOENT): collapsing it lexically would climb back into existing
            # directories this walk never checked, links included.
            if ".." in parts:
                raise FileNotFoundError(
                    errno.ENOENT, "a '..' follows a component that does not exist", str(path)
                ) from None
            return candidate.joinpath(*parts)
        if not stat.S_ISLNK(st.st_mode):
            resolved = candidate
            continue
        hops += 1
        if hops > _MAX_LINK_HOPS:
            raise OSError(f"too many levels of symbolic links: {path}")
        holder = os.stat(resolved)
        if not _link_hop_allowed(st.st_uid, holder.st_mode, holder.st_uid, os.geteuid()):
            raise UnsafeLinkError(
                f"{candidate} is a symlink in a shared sticky directory, owned by neither "
                "you nor the directory's owner; it was not followed and nothing was written."
            )
        target = Path(os.readlink(candidate))
        if target.is_absolute():
            resolved = Path("/")
            parts = list(target.parts[1:]) + parts
        else:
            parts = list(target.parts) + parts
    return resolved


def _open_dir(directory: Path, create: bool = False) -> int:
    """Open ``directory`` through the kernel and return the fd.

    The kernel walks the path itself, so ``protected_symlinks`` applies to any
    link in it, including one swapped in after ``_checked_resolve`` looked.
    """
    if create:
        os.makedirs(directory, exist_ok=True)
    return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


class _Target:
    """Where a write to a path lands: an open directory fd, a name in it, and its state."""

    def __init__(self, path: Path, create_dirs: bool = False):
        self.resolved = _checked_resolve(path)
        self.name = self.resolved.name
        self.dfd = _open_dir(self.resolved.parent, create_dirs)
        try:
            try:
                self.st = os.stat(self.name, dir_fd=self.dfd, follow_symlinks=False)
            except FileNotFoundError:
                self.st = None
            if self.st is not None and stat.S_ISDIR(self.st.st_mode):
                raise IsADirectoryError(f"{path} is a directory")
            if self.st is not None and stat.S_ISREG(self.st.st_mode) and self.st.st_nlink > 1:
                raise LinkedFileError(
                    f"{path} has {self.st.st_nlink} hard links; an atomic write would detach "
                    "this name from the others, so nothing was written. Edit it with "
                    "run_command (which writes in place) if that is what you want."
                )
        except BaseException:
            os.close(self.dfd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self.dfd)
        return False


def _create_temp(dfd: int, name: str) -> tuple[int, str]:
    """Create a private temp file beside ``name`` in ``dfd``, never through a link."""
    while True:
        tmp = f".{name}.{secrets.token_hex(6)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            return os.open(tmp, flags, 0o600, dir_fd=dfd), tmp
        except FileExistsError:  # pragma: no cover — 48 random bits colliding
            continue


def _write_into(target: _Target, data: bytes) -> int:
    """Write ``data`` atomically to ``target``; return the byte count.

    Temp file in the same directory (so the rename stays on one filesystem),
    then a rename over the name — a crash leaves the old file or the new one,
    never a truncated mix. The rename replaces the directory entry and never
    follows a link, so a link planted at the name after the check is replaced,
    not written through. An existing file's mode is kept; a new file is 0600.
    """
    fd, tmp = _create_temp(target.dfd, target.name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if target.st is not None and stat.S_ISREG(target.st.st_mode):
                os.fchmod(f.fileno(), target.st.st_mode & 0o777)
        os.replace(tmp, target.name, src_dir_fd=target.dfd, dst_dir_fd=target.dfd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=target.dfd)
        raise
    return len(data)


# Whole-file reads for edit_file. read_file streams; an edit needs the file in
# memory, so it is refused past this size rather than allowed to exhaust the
# process every client shares.
_EDIT_MAX_BYTES = 16 * 1024 * 1024


def _read_regular(target: _Target) -> bytes:
    """Read a regular file named in ``target``: no links followed, no FIFOs, bounded.

    ``O_NONBLOCK`` so a FIFO cannot block the open, ``O_NOFOLLOW`` so a link
    swapped in since the check is refused rather than followed, and ``fstat`` on
    what was actually opened decides whether it is a regular file.
    """
    limit = _EDIT_MAX_BYTES
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = os.open(target.name, flags, dir_fd=target.dfd)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{target.resolved} is not a regular file")
        if st.st_size > limit:
            raise OSError(
                f"{target.resolved} is {st.st_size} bytes, over edit_file's {limit}-byte "
                "limit; nothing was changed. Use run_command for a file this size."
            )
        data = f.read(limit + 1)
    if len(data) > limit:
        raise OSError(f"{target.resolved} grew past edit_file's {limit}-byte limit while read")
    return data


def _written_to(path: Path, target: _Target) -> dict:
    """``{"written_to": ...}`` when the write landed somewhere other than ``path`` itself."""
    if target.resolved != Path(os.path.normpath(path)):
        return {"written_to": str(target.resolved)}
    return {}


# ---------------------------------------------------------------------------
# Bounded output capture
# ---------------------------------------------------------------------------
_OUTPUT_LIMIT_ENV_VAR = "SYSTEM_OPS_OUTPUT_LIMIT_BYTES"
_DEFAULT_OUTPUT_LIMIT = 1024 * 1024  # 1 MiB per stream
_READ_CHUNK = 65536
_REAP_TIMEOUT = 5


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

    # Both pipes at EOF does not mean the command has finished: one that
    # redirects its own stdio (``exec >>"$LOG" 2>&1``) closes them at once and
    # keeps running. So the deadline also bounds the wait for the exit itself.
    if not truncated and not timed_out:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
    if truncated or timed_out:
        _kill_group(proc)
    # Bounded even after SIGKILL: a process stuck in uninterruptible sleep (an
    # NFS stall) cannot be reaped until the kernel lets go of it, and the tool
    # call must return regardless. Its exit code is then unknown.
    try:
        exit_code = proc.wait(timeout=_REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("run_command.unreaped", pid=proc.pid)
        exit_code = -1
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


def _command_result(
    stdout: str,
    stderr: str,
    exit_code: int,
    truncated: bool,
    started: float,
    error: str | None = None,
) -> dict:
    """The one shape every ``run_command`` response has, success or failure.

    ``error`` is set only when the *tool* failed (the cwd was refused, or the
    command could not be spawned), which is what ``_instrumented`` counts. A
    non-zero exit or a timeout is the command's outcome, not the tool's, and
    leaves it None.
    """
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "truncated": truncated,
        "execution_time": round(time.perf_counter() - started, 4),
        "error": error,
    }


def _mark_truncated(text: str, limit: int) -> str:
    """Append an explicit marker so truncation is distinguishable from brevity."""
    return f"{text}\n[truncated: output reached the {limit}-byte per-stream cap]"


# Tool annotations below are declared as plain dicts using the MCP spec's own
# camelCase key names, rather than by constructing an mcp.types.ToolAnnotations
# with snake_case keyword arguments.
#
# The two are not interchangeable across versions. In mcp 1.x (1.27.1 when this
# was written, 1.30.0 in uv.lock now) that model's fields are literally named `readOnlyHint`, there
# are no aliases, and it is configured `extra="allow"`. A snake_case keyword
# does not raise there: it is silently kept as an unrelated extra attribute
# while the real `readOnlyHint` stays None, so the tool ships with no usable
# annotation at all and the wire response carries a junk key instead. Newer mcp
# renames the fields to snake_case with camelCase aliases, where the keyword
# form works and this dict still validates by alias.
#
# The dict is correct on both: it matches the field name on the old version and
# the alias on the new one. Verified against both.


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "idempotentHint": False,
        "destructiveHint": True,
        # The only tool here that can reach the network: it runs arbitrary
        # shell, so curl, git push and apt are all inside its envelope.
        "openWorldHint": True,
    }
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

    Every response carries the same keys: ``stdout``, ``stderr``,
    ``exit_code``, ``truncated``, ``execution_time`` (wall-clock seconds) and
    ``error``. ``error`` is null unless the tool itself failed — the cwd was
    refused or the command could not be started. A non-zero exit code or a
    timeout leaves it null.
    """
    started = time.perf_counter()
    working_dir = cwd or str(Path.home())
    # Only the working directory is validated. Tilde paths *inside* ``command``
    # are fine — those go through bash, which expands them correctly.
    try:
        _resolve_path(working_dir)
    except PathValidationError as e:
        log.warning("run_command.invalid_cwd", cwd=working_dir)
        msg = f"cwd: {e}"
        return _command_result("", msg, -1, False, started, error=msg)
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
    # The text stays at DEBUG; only its class travels. This is what makes an agent
    # working around githost-mcp visible without publishing what it typed — a class
    # is bounded cardinality and cannot carry a token pasted onto a command line.
    verb_class = classify_verb(command)
    limit = _output_limit()
    try:
        stdout, stderr, exit_code, truncated, timed_out = _run_capped(
            command, working_dir, child_env, timeout, limit
        )
        if timed_out:
            log.warning(
                "run_command.timeout",
                cwd=working_dir,
                timeout=timeout,
                verb_class=verb_class,
                env_withheld_count=withheld,
                env_enforced=enforced,
            )
            return _command_result(
                stdout, f"Command timed out after {timeout} seconds.", -1, truncated, started
            )
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
            verb_class=verb_class,
            env_withheld_count=withheld,
            env_enforced=enforced,
        )
        return _command_result(stdout, stderr, exit_code, truncated, started)
    except Exception as e:
        log.error(
            "run_command.error",
            cwd=working_dir,
            error=str(e),
            verb_class=verb_class,
            env_withheld_count=withheld,
            env_enforced=enforced,
        )
        return _command_result("", str(e), -1, False, started, error=str(e))


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
def _read_lines_capped(path: Path, first: int, last: int | None, limit: int) -> dict:
    """Stream ``path`` and return lines ``first``..``last`` (1-indexed), at most ``limit`` bytes.

    The file is read in bounded chunks rather than whole, so memory stays at
    about ``limit`` however large the file is. Reading stops as soon as the
    range is complete or the budget is spent, so a short range near the start
    of a huge file costs a short read; ``total_lines`` is then None, because
    the rest of the file was not counted.

    ``limit`` counts the returned text as UTF-8, after decoding: an invalid
    byte becomes U+FFFD, three bytes, so counting raw bytes would let a binary
    file return three times the cap.

    Lines end at LF only, the same as ``wc -l`` and ``sed -n``, and the bytes
    are returned as they are on disk: a CRLF file reads back with its CRs, so
    what ``read_file`` shows is what ``edit_file`` will match.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pieces: list[str] = []
    used = 0
    truncated = False
    complete = True
    returned_first = returned_last = None
    line_no = 1
    pending = False  # the current line has bytes but no newline yet
    with open(path, "rb") as f:
        while chunk := f.readline(_READ_CHUNK):
            if truncated or (last is not None and line_no > last):
                complete = False  # there is more file, and it was not read
                break
            if line_no >= first:
                room = limit - used
                text = decoder.decode(chunk)
                encoded = text.encode()
                if len(encoded) > room:
                    # Cut on a character boundary, never mid-sequence.
                    text = encoded[:room].decode(errors="ignore")
                    encoded = text.encode()
                    truncated = True
                pieces.append(text)
                used += len(encoded)
                # A line counts as returned only if some of it was: a budget
                # that runs out on a line boundary, or inside the line's first
                # character, returns none of it, and a caller resuming at
                # last + 1 must not skip it.
                if text:
                    if returned_first is None:
                        returned_first = line_no
                    returned_last = line_no
            if chunk.endswith(b"\n"):
                line_no += 1
                pending = False
            else:
                pending = True
    if not truncated:
        tail = decoder.decode(b"", final=True)  # a dangling partial sequence
        if tail and len(tail.encode()) <= limit - used:
            pieces.append(tail)
        elif tail:
            truncated = True  # left out, so say so
    total = (line_no if pending else line_no - 1) if complete else None
    return {
        "content": "".join(pieces),
        "total_lines": total,
        "first": returned_first,
        "last": returned_last,
        "truncated": truncated,
    }


def _read_file(path: str, start_line: int | None, end_line: int | None, limit: int) -> dict:
    """``read_file``'s body, with the byte budget passed in (see ``read_multiple_files``)."""
    try:
        p = _resolve_path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}
        if not p.is_file():
            return {"error": f"Path is not a file: {path}"}

        ranged = start_line is not None or end_line is not None
        first = max(1, start_line or 1)
        if end_line is not None and end_line < 1:
            # 0 and negatives used to mean "whole file" and "slice from the end",
            # silently. Neither is a line number.
            return {"error": f"end_line must be at least 1, got {end_line}"}
        if end_line is not None and end_line < first:
            return {"error": f"end_line ({end_line}) is before start_line ({first})"}

        r = _read_lines_capped(p, first, end_line, limit)
        if r["truncated"]:
            log.warning("read_file.truncated", path=path, limit_bytes=limit)
        log.info("read_file.done", path=path, total_lines=r["total_lines"], ranged=ranged)
        result = {
            "path": path,
            "total_lines": r["total_lines"],
            "content": r["content"],
            "truncated": r["truncated"],
        }
        if ranged or r["truncated"]:
            # "none" when the range starts past the end of the file.
            result["returned_lines"] = (
                f"{r['first']}-{r['last']}" if r["first"] is not None else "none"
            )
        return result
    except PathValidationError as e:
        log.warning("read_file.invalid_path", path=path)
        return {"error": str(e)}
    except PermissionError:
        log.warning("read_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        log.error("read_file.error", path=path, error=str(e))
        return {"error": str(e)}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
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
            Values below 1 are treated as 1.
        end_line: Last line to return (1-indexed, inclusive). Omit for end of file.
            Must be at least 1 and not less than start_line.

    At most SYSTEM_OPS_OUTPUT_LIMIT_BYTES (default 1 MiB) of content is returned,
    counted as UTF-8. Past that ``truncated`` is true and ``returned_lines`` says
    where it stopped; read the rest with a later ``start_line``. Reading stops
    once the range or the budget is done, and ``total_lines`` is then null
    (the rest of the file was not counted). Content is returned byte for byte
    (CRLF stays CRLF); invalid UTF-8 is shown as U+FFFD.
    """
    return _read_file(path, start_line, end_line, _output_limit())


# ---------------------------------------------------------------------------
# read_multiple_files
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
@_instrumented
def read_multiple_files(paths: list[str]) -> dict:
    """Read several files in one call. Returns a result per path.

    One failing path does not fail the others — each entry is either a file
    result or an ``error``, so a partial read is still useful.

    The whole call shares one SYSTEM_OPS_OUTPUT_LIMIT_BYTES budget (default
    1 MiB), so asking for more files does not multiply what is returned. Once
    it is spent, the remaining paths are not read and their entries say so.

    Args:
        paths: Absolute paths to read.
    """
    results = {}
    budget = _output_limit()
    for path in paths:
        if path in results:
            continue
        if budget <= 0:
            results[path] = {
                "error": "Not read: earlier files used up this call's output budget. "
                "Read it in a separate call."
            }
            continue
        results[path] = _read_file(path, None, None, budget)
        budget -= len(results[path].get("content", "").encode(errors="replace"))
    # Counted over the distinct paths actually reported: a repeated path is
    # read once, and counting it twice made a phantom failure with no entry.
    ok = sum(1 for r in results.values() if "error" not in r)
    log.info("read_multiple_files.done", count=len(results), ok=ok)
    return {"count": len(results), "ok": ok, "failed": len(results) - ok, "files": results}


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
@mcp.tool(
    annotations={
        "readOnlyHint": False,
        # Writing the same content twice leaves the same file.
        "idempotentHint": True,
        "destructiveHint": True,
        "openWorldHint": False,
    }
)
@_instrumented
def write_file(
    path: str,
    content: str,
    create_dirs: bool = True,
) -> dict:
    """Write (overwrite) a file at the given path.

    The write is atomic (temp file + rename). If ``path`` is a symlink the write
    goes through it to the real file, the link is kept, and ``written_to`` names
    the file written. A file with more than one hard link is refused, since a
    rename would detach this name from the others. So is a path through a
    symlink that another user planted in a shared sticky directory such as
    /tmp, the case the kernel's protected_symlinks rule exists for.

    Args:
        path: Absolute path to the file.
        content: Full content to write. Replaces any existing content.
        create_dirs: If True, create parent directories if they don't exist (default True).
    """
    try:
        p = _resolve_path(path)
        # create_dirs applies to the directory the write lands in, so a link to
        # a file in a directory that does not exist yet gets that directory.
        with _Target(p, create_dirs=create_dirs) as target:
            written = _write_into(target, content.encode())
            log.info("write_file.done", path=path, bytes_written=written)
            return {"path": path, "bytes_written": written, **_written_to(p, target)}
    except PathValidationError as e:
        log.warning("write_file.invalid_path", path=path)
        return {"error": str(e)}
    except UnsafeLinkError as e:
        log.warning("write_file.unsafe_link_refused", path=path)
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
# Bounds on the hint's work. Each window costs a difflib ratio(), roughly
# quadratic in len(old_str) and pure Python, so it holds the GIL and slows every
# other agent's call while it runs. Measured before these bounds: 11 s for a
# 1 KiB old_str against a 35 KiB file, 168 s for 2 KiB against 80 KiB. The
# budget is checked between windows, so the length cap is what bounds a single
# comparison (one ratio() at 4 KiB against a 12 KiB window measured ~3 s, so
# the cap is 2 KiB). A scan that runs out of budget offers no hint rather than
# a partial one.
_CLOSEST_MATCH_MAX_OLD = 2048
_CLOSEST_MATCH_BUDGET_S = 1.0
# Below this similarity the "closest" window is noise and quoting it misleads.
_CLOSEST_MATCH_MIN_RATIO = 0.5
_DIFF_MAX_LINES = 40


def _closest_match(content: str, old_str: str) -> dict | None:
    """Find the passage most like ``old_str`` and diff it against the request.

    A failed edit previously returned only a match count, which tells the caller
    that something is wrong but not what. Faced with that, an agent tends to
    give up on editing and rewrite the whole file — which is a much larger and
    riskier write than the edit it was trying to make.

    Returns None when the file or ``old_str`` is too large to scan, the scan
    runs out of its time budget, or nothing resembles ``old_str`` closely enough
    to be worth quoting.
    """
    if len(content) > _CLOSEST_MATCH_MAX_BYTES or len(old_str) > _CLOSEST_MATCH_MAX_OLD:
        return None
    deadline = time.monotonic() + _CLOSEST_MATCH_BUDGET_S

    lines = content.splitlines(keepends=True)
    old_lines = old_str.splitlines(keepends=True) or [""]
    span = len(old_lines)
    if not lines:
        return None

    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq1(old_str)
    best_ratio, best_index, best_text = 0.0, 0, ""
    # ratio() is 2*M/(len(a)+len(b)) with M <= len(a), so a window more than
    # three times old_str's length scores below 0.5 and can never be quoted.
    # Skipping it before set_seq2 keeps one huge line from costing anything.
    max_window = 3 * max(1, len(old_str))
    for i in range(max(1, len(lines) - span + 1)):
        if time.monotonic() > deadline:
            return None
        window = "".join(lines[i : i + span])
        if len(window) > max_window:
            continue
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
    annotations={
        "readOnlyHint": False,
        # Not idempotent: the second call finds no match and fails.
        "idempotentHint": False,
        "destructiveHint": True,
        "openWorldHint": False,
    }
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

    Only the matched span changes; every other byte, line endings included, is
    written back as it was. In a CRLF file, an old_str written with plain LF
    line breaks is matched against the CRLF form, and new_str is given CRLF
    breaks to match (``line_endings`` is then "crlf"). A file that is not valid
    UTF-8 is refused rather than re-encoded, as is anything that is not a
    regular file or is over 16 MiB. Symlinks and hard links are handled as in
    write_file.

    Args:
        path: Absolute path to the file.
        old_str: Exact string to find (must match exactly once).
        new_str: Replacement string.
        dry_run: If True, report what the edit would do and write nothing.
    """
    try:
        p = _resolve_path(path)
        with _Target(p) as target:
            if target.st is None:
                return {"error": f"File not found: {path}"}
            return _edit(p, target, old_str, new_str, dry_run)
    except PathValidationError as e:
        log.warning("edit_file.invalid_path", path=path)
        return {"error": str(e)}
    except UnsafeLinkError as e:
        log.warning("edit_file.unsafe_link_refused", path=path)
        return {"error": str(e)}
    except PermissionError:
        log.warning("edit_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except FileNotFoundError:
        return {"error": f"File not found: {path}"}
    except Exception as e:
        log.error("edit_file.error", path=path, error=str(e))
        return {"error": str(e)}


def _edit(p: Path, target: _Target, old_str: str, new_str: str, dry_run: bool) -> dict:
    """``edit_file``'s body, against a target already resolved and checked."""
    path = str(p)
    raw = _read_regular(target)
    try:
        # Strict, and without newline translation: the file is rewritten from
        # this string, so any lossy step here would change bytes the edit
        # never touched.
        original = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        log.info("edit_file.not_utf8", path=path)
        return {
            "error": f"File is not valid UTF-8 (byte {e.start}); edit_file would have "
            "to re-encode it, so no changes were made. Use run_command for this file."
        }
    count = original.count(old_str)
    crlf = False
    if count == 0 and "\r\n" in original and "\n" in old_str and "\r" not in old_str:
        crlf_old = old_str.replace("\n", "\r\n")
        crlf_count = original.count(crlf_old)
        if crlf_count:
            old_str, count, crlf = crlf_old, crlf_count, True
            if "\r" not in new_str:
                new_str = new_str.replace("\n", "\r\n")

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
    extra = {"line_endings": "crlf"} if crlf else {}
    if dry_run:
        # The target was resolved and checked already, so a hard-linked file
        # or an unsafe link fails the dry run the same way as the real one.
        log.info("edit_file.dry_run", path=path, matches_replaced=1)
        return {
            "path": path,
            "status": "ok",
            "matches_replaced": 1,
            "dry_run": True,
            "bytes_before": len(raw),
            "bytes_after": len(updated.encode()),
            **extra,
        }
    _write_into(target, updated.encode())
    log.info("edit_file.done", path=path, matches_replaced=1)
    return {
        "path": path,
        "status": "ok",
        "matches_replaced": 1,
        "dry_run": False,
        **extra,
        **_written_to(p, target),
    }


# ---------------------------------------------------------------------------
# read_directory
# ---------------------------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
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
@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
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
# Host / Origin check (DNS-rebinding protection)
# ---------------------------------------------------------------------------
# The server is unauthenticated, and its control is that only local processes
# can reach the loopback port. A browser on this host breaks that: a page whose
# hostname an attacker re-points at 127.0.0.1 is, to the browser, same-origin
# with this port, and could drive run_command. The browser still sends the
# attacker's hostname in Host, so refusing any Host that is not a loopback name
# closes it.
#
# Any port, not just the bind port. A client that arrives through a port
# forward sends the forward's local port: an SSH tunnel from 127.0.0.1:8290 to a
# remote instance bound to 8282 makes that instance receive
# `Host: 127.0.0.1:8290`. The port adds nothing against rebinding; the hostname
# is what an attacker controls.
#
# mcp ships this check (TransportSecurityMiddleware), but FastMCP constructs the
# transport with it switched off. FastMCP 3.4 added a guard of its own, which is
# also off by default (``http_host_origin_protection = False``) and absent from
# 3.3. So mcp's check is reused here, explicitly, as a Starlette middleware
# through mcp.run(middleware=...): it does not depend on a FastMCP setting, and
# tests/test_host_origin.py pins it against the real app.
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
_ALLOWED_HOSTS_ENV_VAR = "SYSTEM_OPS_ALLOWED_HOSTS"


def _transport_security_settings() -> TransportSecuritySettings:
    """Loopback names on any port, plus exact Host values from ``SYSTEM_OPS_ALLOWED_HOSTS``.

    The extra entries exist for ``--host 0.0.0.0`` deployments, whose clients
    arrive under another name (``host.docker.internal:8282``, say).

    No Origin is allowed at all. No browser is a legitimate client, and only
    browsers send Origin, so a request carrying one is refused (403) whatever
    it names. Allowing loopback origins would have trusted every web app
    served on this host. A request without the header, which is every MCP
    client, passes mcp's check.
    """
    hosts = [f"{name}:*" for name in _LOOPBACK_NAMES]
    for entry in os.environ.get(_ALLOWED_HOSTS_ENV_VAR, "").split(","):
        if entry.strip():
            hosts.append(entry.strip())
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[],
    )


class HostOriginGuard:
    """ASGI middleware: refuse an HTTP request whose Host or Origin is not allowed.

    Refused with 421 (Host) or 403 (Origin), by mcp's own check. The header
    values are not logged: they are caller-controlled and unbounded.
    """

    def __init__(self, app, settings: TransportSecuritySettings):
        self.app = app
        self.check = TransportSecurityMiddleware(settings)
        # mcp's check logs each refusal as f"Invalid Host header: {host}", the
        # raw caller-supplied value. The refusal is logged above without it.
        logging.getLogger("mcp.server.transport_security").setLevel(logging.ERROR)

    async def __call__(self, scope, receive, send):
        # WebSocket too: no route takes one today, and a later one must not
        # arrive unguarded.
        if scope["type"] in ("http", "websocket"):
            # is_post=False: Content-Type is checked by the MCP transport itself.
            # Only the headers are read, so an HTTPConnection serves both types.
            refusal = await self.check.validate_request(HTTPConnection(scope), is_post=False)
            if refusal is not None:
                log.warning(
                    "http.request_refused", status=refusal.status_code, scope_type=scope["type"]
                )
                if scope["type"] == "websocket":
                    # Closing before accept makes the server answer the upgrade with 403.
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _http_middleware() -> list[Middleware]:
    return [Middleware(HostOriginGuard, settings=_transport_security_settings())]


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
    middleware = _http_middleware()
    log.info(
        "server.start",
        host=args.host,
        port=args.port,
        path=args.path,
        allowed_hosts=middleware[0].kwargs["settings"].allowed_hosts,
    )
    try:
        _serve(args, middleware)
    finally:
        # Drain the InfluxDB/NATS sinks: without this, metrics still queued when
        # the process is stopped are dropped. (The OTLP SDK flushes itself at exit.)
        telemetry.shutdown()


def _serve(args, middleware: list[Middleware]) -> None:
    mcp.run(
        transport="streamable-http",
        middleware=middleware,
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
