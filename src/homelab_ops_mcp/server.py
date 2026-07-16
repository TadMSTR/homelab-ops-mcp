"""homelab-ops-mcp — Shell and file access MCP server for the homelab ops agent.

Transport: streamable-http on 0.0.0.0:<port>/mcp

Exposes six tools: run_command, read_file, write_file, edit_file, read_directory,
and list_processes. All server logic lives here; see ARCHITECTURE.md for the layout.
"""

import contextlib
import os
import subprocess
from pathlib import Path

import psutil
from fastmcp import FastMCP

from .logging import configure_logging

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


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------
@mcp.tool
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
    """
    working_dir = cwd or str(Path.home())
    # Command text is sensitive; log it at DEBUG only.
    log.debug("run_command.start", cwd=working_dir, timeout=timeout, command=command)
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_env(),
        )
        log.info("run_command.done", cwd=working_dir, exit_code=result.returncode)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        log.warning("run_command.timeout", cwd=working_dir, timeout=timeout)
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds.",
            "exit_code": -1,
        }
    except Exception as e:
        log.error("run_command.error", cwd=working_dir, error=str(e))
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
@mcp.tool
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
        p = Path(path)
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
    except PermissionError:
        log.warning("read_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        log.error("read_file.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------
@mcp.tool
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
        p = Path(path)
        if create_dirs:
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        written = len(content.encode())
        log.info("write_file.done", path=path, bytes_written=written)
        return {"path": path, "bytes_written": written}
    except PermissionError:
        log.warning("write_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        log.error("write_file.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------
@mcp.tool
def edit_file(
    path: str,
    old_str: str,
    new_str: str,
) -> dict:
    """Find-and-replace edit: replace old_str with new_str in a file.

    old_str must match exactly once. Fails if zero or multiple matches found.

    Args:
        path: Absolute path to the file.
        old_str: Exact string to find (must match exactly once).
        new_str: Replacement string.
    """
    try:
        p = Path(path)
        if not p.exists():
            return {"error": f"File not found: {path}"}

        original = p.read_text(errors="replace")
        count = original.count(old_str)

        if count == 0:
            return {"error": "old_str not found in file. No changes made."}
        if count > 1:
            return {
                "error": f"old_str matched {count} times. Must match exactly once. No changes made."
            }

        updated = original.replace(old_str, new_str, 1)
        p.write_text(updated)
        log.info("edit_file.done", path=path, matches_replaced=1)
        return {"path": path, "status": "ok", "matches_replaced": 1}
    except PermissionError:
        log.warning("edit_file.permission_denied", path=path)
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        log.error("edit_file.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# read_directory
# ---------------------------------------------------------------------------
@mcp.tool
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
        p = Path(path)
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
    except Exception as e:
        log.error("read_directory.error", path=path, error=str(e))
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# list_processes
# ---------------------------------------------------------------------------
@mcp.tool
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
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8282, help="Bind port (default: 8282)")
    parser.add_argument("--path", default="/mcp", help="HTTP path (default: /mcp)")
    args = parser.parse_args()

    log.info("server.start", host=args.host, port=args.port, path=args.path)
    mcp.run(  # pragma: no cover
        transport="streamable-http",
        host=args.host,
        port=args.port,
        path=args.path,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
