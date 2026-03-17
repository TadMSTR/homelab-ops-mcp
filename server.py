"""
homelab-ops-mcp — Shell and file access MCP server for LibreChat homelab ops agent.
Transport: streamable-http on 0.0.0.0:<port>/mcp
"""

import os
import subprocess
import psutil
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

mcp = FastMCP(
    name="homelab-ops",
    instructions=(
        "Homelab operations server. Provides shell command execution, file system "
        "read/write, directory listing, and process inspection on claudebox."
    ),
)


# ---------------------------------------------------------------------------
# run_command
# ---------------------------------------------------------------------------
@mcp.tool
def run_command(
    command: str,
    cwd: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Execute a shell command and return stdout, stderr, and exit code.

    Args:
        command: Shell command to run (executed via bash -c).
        cwd: Working directory for the command. Defaults to /home/ted.
        timeout: Max seconds to wait before killing the process (default 30).
    """
    working_dir = cwd or str(Path.home())
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds.",
            "exit_code": -1,
        }
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------
@mcp.tool
def read_file(
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
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
            return {
                "path": path,
                "total_lines": total,
                "returned_lines": f"{sl + 1}-{el}",
                "content": "".join(selected),
            }

        return {"path": path, "total_lines": total, "content": "".join(lines)}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
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
        return {"path": path, "bytes_written": len(content.encode())}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
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
            return {"error": f"old_str matched {count} times. Must match exactly once. No changes made."}

        updated = original.replace(old_str, new_str, 1)
        p.write_text(updated)
        return {"path": path, "status": "ok", "matches_replaced": 1}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
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
                        try:
                            entry["size_bytes"] = item.stat().st_size
                        except Exception:
                            pass
                    if recursive and item.is_dir() and depth < max_depth:
                        entry["children"] = _list(item, depth + 1)
                    entries.append(entry)
            except PermissionError:
                entries.append({"name": str(directory), "error": "permission denied"})
            return entries

        entries = _list(p, 1)
        return {"path": path, "entries": entries, "count": len(entries)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# list_processes
# ---------------------------------------------------------------------------
@mcp.tool
def list_processes(
    sort_by: str = "cpu",
    limit: int = 30,
    name_filter: Optional[str] = None,
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
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"],
                    "cpu_percent": round(info["cpu_percent"] or 0.0, 2),
                    "mem_percent": round(info["memory_percent"] or 0.0, 3),
                    "status": info["status"],
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        sort_key = {"cpu": "cpu_percent", "mem": "mem_percent", "pid": "pid"}.get(sort_by, "cpu_percent")
        procs.sort(key=lambda x: x[sort_key], reverse=(sort_by != "pid"))
        return {"count": len(procs[:limit]), "sort_by": sort_by, "processes": procs[:limit]}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="homelab-ops MCP server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8282, help="Bind port (default: 8282)")
    parser.add_argument("--path", default="/mcp", help="HTTP path (default: /mcp)")
    args = parser.parse_args()

    mcp.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        path=args.path,
    )
