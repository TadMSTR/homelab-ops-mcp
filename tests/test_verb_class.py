"""`verb_class` on run_command: the classifier, and the text that must not leak.

The command string is captured at DEBUG only, deliberately. This build adds a
bounded classification at INFO so a repo write through `run_command` is visible
without publishing what was typed. The negative assertion is the point of the
file — a test that only checks the class is present would pass happily while the
command text leaked alongside it.
"""

import json

import pytest

from homelab_ops_mcp import logging as hlog
from homelab_ops_mcp import server
from homelab_ops_mcp.verbclass import (
    GH_READ,
    GH_WRITE,
    GIT_READ,
    GIT_WRITE,
    OTHER,
    VCS_OTHER,
    VERB_CLASSES,
    classify_verb,
)

# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # writes — the class this exists to catch
        ("git push origin main", GIT_WRITE),
        ("git commit -m 'wip'", GIT_WRITE),
        ("git merge feature", GIT_WRITE),
        ("git rebase main", GIT_WRITE),
        ("git reset --hard HEAD~1", GIT_WRITE),
        ("git tag v1.0.0", GIT_WRITE),
        ("git branch -d stale", GIT_WRITE),
        ("git branch -D stale", GIT_WRITE),
        ("git remote add origin git@host:o/r.git", GIT_WRITE),
        ("git config user.email a@b.c", GIT_WRITE),
        ("git stash pop", GIT_WRITE),
        # reads
        ("git status", GIT_READ),
        ("git log --oneline -5", GIT_READ),
        ("git diff HEAD", GIT_READ),
        ("git branch", GIT_READ),
        ("git tag", GIT_READ),
        ("git remote -v", GIT_READ),
        ("git config --get user.email", GIT_READ),
        ("git stash list", GIT_READ),
        ("git remote show origin", GIT_READ),
        ("git branch --contains HEAD", GIT_READ),
        ("git tag -l 'v*'", GIT_READ),
        ("git config --get-regexp ^user", GIT_READ),
        ("git submodule status", GIT_READ),
        ("git submodule update --init", GIT_WRITE),
        ("git branch --edit-description x", GIT_WRITE),
        ("git worktree add /tmp/wt main", GIT_WRITE),
        # gh
        ("gh pr merge 12 --squash", GH_WRITE),
        ("gh pr create --title x", GH_WRITE),
        ("gh release delete v1", GH_WRITE),
        ("gh run rerun 55", GH_WRITE),
        ("gh pr list", GH_READ),
        ("gh pr view 12", GH_READ),
        ("gh run list", GH_READ),
        # gh api defaults to GET
        ("gh api /repos/o/r", GH_READ),
        ("gh api -X DELETE /repos/o/r/git/refs/heads/x", GH_WRITE),
        ("gh api --method PATCH /repos/o/r", GH_WRITE),
        ("gh api -f title=x /repos/o/r/issues", GH_WRITE),
        # not version control at all
        ("ls -la /tmp", OTHER),
        ("docker ps", OTHER),
        ("", OTHER),
        # git, but a subcommand the classifier does not know
        ("git frobnicate --wat", VCS_OTHER),
    ],
)
def test_classify(command, expected):
    assert classify_verb(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd /srv/repo && git push", GIT_WRITE),
        ("git status && git push", GIT_WRITE),
        ("git fetch; git status", GIT_WRITE),
        ("echo $(git push origin main)", GIT_WRITE),
        ("out=`git push`", GIT_WRITE),
        ("(cd /r && git push)", GIT_WRITE),
        ("echo x | xargs git push", GIT_WRITE),
        ("sudo git reset --hard", GIT_WRITE),
        ("GIT_DIR=/a git -C /b push", GIT_WRITE),
        ("/usr/bin/git push", GIT_WRITE),
        ("env GIT_SSH_COMMAND='ssh -i k' git push", GIT_WRITE),
        # a read and a write together take the write
        ("git log && gh pr list", GIT_READ),
        ("gh pr list && git push", GIT_WRITE),
    ],
)
def test_classify_finds_the_command_wherever_it_hides(command, expected):
    """A compound command takes the class of its most significant segment.

    Each of these is a way an agent can perform a repo write that a naive
    `command.startswith("git ")` check would miss entirely.
    """
    assert classify_verb(command) == expected


def test_classification_is_always_a_vocabulary_member():
    """verb_class goes on a log line and is a candidate metric label; it must be
    bounded by construction, not by convention."""
    for command in ["", "git", "gh", "git push", "!!!", "'unbalanced", "a=b", "|||", "git -C"]:
        assert classify_verb(command) in VERB_CLASSES


def test_unbalanced_quotes_still_classify():
    """shlex raises on these. Falling back to `other` would make a malformed
    `git push` indistinguishable from `ls`."""
    assert classify_verb("git push 'unbalanced") == GIT_WRITE


# ---------------------------------------------------------------------------
# The log event
# ---------------------------------------------------------------------------


def test_run_command_done_carries_verb_class(recorder):
    server.run_command("git status", cwd="/tmp")
    (done,) = recorder.of("run_command.done")
    assert done["verb_class"] == GIT_READ


def test_run_command_done_reports_a_write(recorder, tmp_path):
    # `git push` with no remote fails, but the classification is of the command,
    # not of its outcome — a failed workaround attempt is still a workaround attempt.
    server.run_command("git push nonexistent-remote", cwd=str(tmp_path))
    (done,) = recorder.of("run_command.done")
    assert done["verb_class"] == GIT_WRITE


def test_non_vcs_command_is_other(recorder):
    server.run_command("echo hello", cwd="/tmp")
    (done,) = recorder.of("run_command.done")
    assert done["verb_class"] == OTHER


def test_timeout_path_carries_verb_class(recorder):
    """A `git push` that hung is exactly the case worth seeing. The timeout path
    returns before run_command.done, so carrying the class only on `done` would
    drop it."""
    server.run_command("sleep 5 && git push", cwd="/tmp", timeout=1)
    (timeout,) = recorder.of("run_command.timeout")
    assert timeout["verb_class"] == GIT_WRITE


# ---------------------------------------------------------------------------
# The negative: the command text must not follow the class up to INFO
# ---------------------------------------------------------------------------


@pytest.fixture()
def logfile(monkeypatch, tmp_path):
    path = tmp_path / "server.log"
    hlog._configured = False
    monkeypatch.setenv("LOG_FILE", str(path))
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    hlog.configure_logging()
    monkeypatch.setattr(server, "log", hlog.configure_logging())
    yield path
    hlog._configured = False
    monkeypatch.delenv("LOG_FILE", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    hlog.configure_logging()


MARKER = "s3cret-marker-do-not-log"


def test_command_text_does_not_appear_at_info(logfile):
    server.run_command(f"git push origin {MARKER}", cwd="/tmp")
    body = logfile.read_text()

    records = [json.loads(ln) for ln in body.splitlines() if ln.strip()]
    done = [r for r in records if r["event"] == "run_command.done"]
    assert done, "expected a run_command.done record — otherwise this test proves nothing"
    assert done[0]["verb_class"] == GIT_WRITE

    assert MARKER not in body
    assert not any("command" in r for r in records)


def test_the_marker_would_have_been_visible_at_debug(monkeypatch, tmp_path):
    """Proves the test above is not vacuous.

    If the command text were never written anywhere, `MARKER not in body` would
    pass no matter what the code did. At DEBUG the same command *does* appear, so
    the INFO-level absence is a real filter and not an artefact of the fixture.
    """
    path = tmp_path / "debug.log"
    hlog._configured = False
    monkeypatch.setenv("LOG_FILE", str(path))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    hlog.configure_logging()
    monkeypatch.setattr(server, "log", hlog.configure_logging())
    try:
        server.run_command(f"git push origin {MARKER}", cwd="/tmp")
        assert MARKER in path.read_text()
    finally:
        hlog._configured = False
        monkeypatch.delenv("LOG_FILE", raising=False)
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        hlog.configure_logging()


# ---------------------------------------------------------------------------
# Review round 2 — commands carried inside another command's arguments
#
# All of these classified as `other` or `git_read` on the first pass. They share one
# root cause: the classifier only looked at the head of each segment, so anything
# nested in a quoted payload, behind a positional wrapper argument, or on a second
# line escaped it entirely. That matters more than a missed log line — the programme's
# later part plans to *block* on this signal, and `bash -c` is three characters.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # `run_command` itself executes via `bash -c`, so this is the most ordinary
        # wrapper on the system, not an exotic evasion.
        ('bash -c "git push"', GIT_WRITE),
        ("sh -c 'git push origin main'", GIT_WRITE),
        ('bash -lc "git commit -m x"', GIT_WRITE),
        ('bash -c "ls -la"', OTHER),
        ('bash -c "git status"', GIT_READ),
        # newline-separated commands: `\n` was in _SEPARATORS but shlex's
        # whitespace_split ate it, so the clause could never fire
        ("git status\ngit push", GIT_WRITE),
        ("cd /r\ngit push", GIT_WRITE),
        ("git status\ngit log", GIT_READ),
        ("git status\r\ngit push", GIT_WRITE),
        # positional wrapper arguments
        ("timeout 30 git push", GIT_WRITE),
        ("timeout 30 ls", OTHER),
        ("flock /tmp/lock git push", GIT_WRITE),
        ("ssh host git push", GIT_WRITE),
        # a command behind a command-bearing flag
        ("find . -exec git push {} ;", GIT_WRITE),
        ("find . -name '*.py'", OTHER),
    ],
)
def test_a_command_nested_in_another_command_is_still_found(command, expected):
    assert classify_verb(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # `git submodule foreach` runs an arbitrary shell command in every submodule —
        # a write wearing a read's name (CodeRabbit, PR #7)
        ('git submodule foreach "git push"', GIT_WRITE),
        ("git submodule status", GIT_READ),
        ("git submodule update --init", GIT_WRITE),
        # bare `git stash` IS `git stash push` and changes local state
        ("git stash", GIT_WRITE),
        ("git stash list", GIT_READ),
        ("git stash show", GIT_READ),
        ("git stash pop", GIT_WRITE),
        # reflog/bisect were flat reads; several of their actions destroy state
        ("git reflog", GIT_READ),
        ("git reflog show", GIT_READ),
        ("git reflog expire --expire=now --all", GIT_WRITE),
        ("git reflog delete HEAD@{0}", GIT_WRITE),
        ("git bisect log", GIT_READ),
        ("git bisect start", GIT_WRITE),
        ("git bisect reset", GIT_WRITE),
        # bare `git remote` still lists — the bare-is-write rule must not over-apply
        ("git remote", GIT_READ),
        ("git notes", GIT_READ),
    ],
)
def test_subcommands_that_write_despite_a_read_shaped_name(command, expected):
    assert classify_verb(command) == expected


def test_oversized_command_is_unclassified_not_other(monkeypatch):
    """`other` is a positive claim that this was not a repo operation. A parse that was
    declined cannot support it, and anything gating on this must be able to tell
    "we looked and it was fine" from "we did not look"."""
    from homelab_ops_mcp.verbclass import MAX_CLASSIFY_BYTES, UNCLASSIFIED

    assert classify_verb("echo " + "a" * (MAX_CLASSIFY_BYTES + 10)) == UNCLASSIFIED
    # a write in the parsed head is still a positive finding
    assert classify_verb("git push " + "a" * (MAX_CLASSIFY_BYTES + 10)) == GIT_WRITE
    # and a normal-length non-git command is still a confident `other`
    assert classify_verb("echo hello") == OTHER


def test_classification_is_bounded_in_time():
    """The tokenizer is quadratic on one long unbroken token, and classification runs
    before run_command's own timeout can apply — so an oversized command would hang the
    call at the classification step regardless of `timeout`. Measured pre-fix:
    2M chars took 30.5s."""
    import time

    from homelab_ops_mcp.verbclass import MAX_CLASSIFY_BYTES

    start = time.perf_counter()
    classify_verb("echo " + "a" * (MAX_CLASSIFY_BYTES * 40))
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"classification took {elapsed:.1f}s — the cap is not holding"


def test_recursion_is_depth_bounded():
    nested = 'bash -c "' * 12 + "git push" + '"' * 12
    assert classify_verb(nested) in VERB_CLASSES  # terminates, whatever it decides
