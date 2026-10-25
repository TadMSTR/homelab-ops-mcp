#!/usr/bin/env python3
"""Prove the secret-scanning gate can actually fail before believing it passes.

B14 asks for a secret scanner in CI. It does not ask whether the scanner is wired up
correctly, and "no leaks found" is what a working gate reports on a clean tree and what
a misconfigured one reports on any tree at all. Those two are indistinguishable from the
green tick alone, which is how gates ship inert.

So this runs the gate TWO-SIDED:

  planted  a synthetic credential is committed into a throwaway copy — gitleaks must
           exit with LEAK_EXIT specifically, NOT merely non-zero: gitleaks uses 1 for
           an operational error, so "non-zero" would accept a scan that broke before
           reading anything. This is the side that can actually fail.
  clean    the real working tree — gitleaks must exit zero, so a gate that simply
           fails on everything is not mistaken for a working one.

THE COPY IS NOT INCIDENTAL. The planted secret is committed to git history, and history
is not undone by deleting the file — measured: after `git rm` + commit, gitleaks still
exits 1, which is the whole reason the scheduled scan uses fetch-depth: 0. Planting into
the real repo would therefore poison its history permanently. `tempfile.TemporaryDirectory`
also survives a `finally`-skipping SIGTERM better than an in-place mutate-and-restore,
which leaves the repo dirty if the runner is cancelled mid-step.

Run locally with:  python tests/check_gitleaks_gate.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# A synthetic GitHub PAT, assembled at runtime so that THIS FILE never contains a
# PAT-shaped string. The body below is not a credential and the `ghp_` prefix is only
# ever joined to it in memory, which is what keeps the clean-side scan honest: if the
# literal lived here, the real tree would scan dirty and the probe would be testing
# itself.
#
# THE BODY MUST HAVE REAL ENTROPY. This was originally 36 repeated 'A's, which read as
# obviously-not-a-credential and was wrong for a reason worth recording:
#
#   gitleaks 8.28.0 (the version loki-mcp, where this probe comes from, pinned then;
#   secret-scan.yml here pins 8.30.1) applies an ENTROPY FLOOR to the
#   github-pat rule. Measured 2026-09-20: 'ghp_' + 'A'*36 scores 0.67 and is NOT
#   reported, while the string below scores 5.22 and is. The forge host's own
#   /usr/bin/gitleaks — which reports `version is set by build process` — has no such
#   floor and DID report the all-A's value, so the probe passed locally and the gate
#   shipped inert. CI caught it on the first run, which is the entire point of running
#   the planted side at all.
#
# So: do not "simplify" this to a repeated character, and do not substitute a vendor's
# published example key either — gitleaks allowlists several of those by default and
# the probe would then pass for the wrong reason.
PLANTED = "ghp_" + "x9Kq2mVb7TzR4nJ8pLw3sYd6HgF5cQa1BeN0"

# Directories that are large, regenerable, and not part of what the gate protects.
SKIP = shutil.ignore_patterns(
    ".git",
    ".venv",
    ".venv-*",
    "__pycache__",
    "*.egg-info",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
    "dist",
    "build",
)


# A DISTINCT EXIT CODE FOR "FOUND SOMETHING", so it cannot be confused with "the
# scanner broke". gitleaks exits 1 for an operational error and, by default, ALSO 1
# for a finding — so an "any non-zero means it fired" check passes when the scan
# errored before reading anything, which is the exact false-green this probe exists
# to prevent. --exit-code moves findings off 1 and leaves 1 meaning error alone.
#
# MEASURED against 8.28.0 on 2026-09-20 (loki-mcp); planted -> 42 and clean -> 0
# re-measured against 8.30.1 for homelab-ops-mcp on 2026-10-25:
#   planted secret        -> 42
#   clean tree            -> 0
#   --source nonexistent  -> 1
#   malformed --config    -> 1
LEAK_EXIT = 42


def run_gitleaks(source: Path) -> int:
    """Exit code: 0 clean, LEAK_EXIT findings, anything else a scanner error."""
    return subprocess.run(
        [
            "gitleaks",
            "detect",
            "--source",
            str(source),
            "--redact",
            "--no-banner",
            "--exit-code",
            str(LEAK_EXIT),
        ],
        capture_output=True,
        text=True,
    ).returncode


def check_planted_fails() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(REPO, work, ignore=SKIP)
        (work / "planted_secret.py").write_text(f'GITHUB_TOKEN = "{PLANTED}"\n')

        git = ["git", "-C", str(work), "-c", "user.email=gate@test", "-c", "user.name=gate"]
        subprocess.run([*git[:3], "init", "-q", str(work)], check=True, capture_output=True)
        subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
        subprocess.run([*git, "commit", "-qm", "planted"], check=True, capture_output=True)

        code = run_gitleaks(work)
        if code == 0:
            print(
                "FAIL  planted secret was NOT detected — the gate cannot fail, so "
                "its green result on the real tree means nothing."
            )
            return False
        if code != LEAK_EXIT:
            print(
                f"FAIL  gitleaks exited {code}, not {LEAK_EXIT}. That is a scanner "
                "ERROR, not a detection — the planted secret may never have been "
                "read. Accepting any non-zero here would report a broken scan as a "
                "working gate."
            )
            return False
        print(f"ok    planted secret detected (gitleaks exit {code})")
        return True


def check_clean_passes() -> bool:
    code = run_gitleaks(REPO)
    if code == LEAK_EXIT:
        print(
            f"FAIL  the real tree scans dirty (gitleaks exit {code}) — there is a "
            "genuine finding to fix."
        )
        return False
    if code != 0:
        print(
            f"FAIL  gitleaks exited {code} on the real tree. That is a scanner error, "
            "not a clean result — do not read it as one."
        )
        return False
    print("ok    real tree scans clean")
    return True


def main() -> int:
    if shutil.which("gitleaks") is None:
        print(
            "FAIL  gitleaks is not on PATH. This probe must not be skipped: a skip "
            "here is indistinguishable from a pass, and the gate it guards is the "
            "one that keeps credentials out of a PUBLIC repo."
        )
        return 1

    # Print the version. The failure this probe was written against was a VERSION SKEW
    # between the host binary and the pinned one, and a result with no version beside it
    # cannot be compared to CI's.
    version = subprocess.run(["gitleaks", "version"], capture_output=True, text=True).stdout.strip()
    print(f"gitleaks version: {version or 'unknown'}")

    # Planted first, and RETURN before the clean check rather than collecting both
    # results. The list form evaluated BOTH, so a run where the planted secret went
    # undetected still printed `ok  real tree scans clean` underneath the failure —
    # reassurance from a gate that had just reported it cannot fail.
    if not check_planted_fails():
        return 1
    if not check_clean_passes():
        return 1
    print(
        "gitleaks gate verified two-sided: it fails on a planted secret and passes "
        "on the real tree."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
