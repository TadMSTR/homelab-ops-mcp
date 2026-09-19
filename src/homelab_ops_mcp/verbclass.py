"""Classify a shell command as a git/gh read or write, without exposing its text.

``run_command`` runs arbitrary shell, and the command string is captured at DEBUG
only — deliberately, because it can carry a token someone pasted onto a command
line. That decision stands. What was missing is any INFO-level signal that a call
*was* a repo write, which is what makes an agent shelling out around githost-mcp
visible at all.

So this returns a class, never the text. A class is bounded cardinality, is safe to
put on a log line and a metric label, and answers the only question the detector
asks: was this a git or gh write?

**Ambiguity resolves toward write.** Missing a write hides the workaround the whole
signal exists to catch; over-reporting a read costs a slightly noisy denominator.
``git branch`` alone lists and is a read; ``git branch -d x`` deletes and is a
write — where the distinction cannot be made confidently, this says write.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath

GIT_WRITE = "git_write"
GIT_READ = "git_read"
GH_WRITE = "gh_write"
GH_READ = "gh_read"
VCS_OTHER = "vcs_other"
UNCLASSIFIED = "unclassified"
OTHER = "other"

#: Above this, the command is not fully parsed. `shlex` is quadratic on one long
#: unbroken token (measured: 100k chars 0.07s, 500k 1.4s, 2M 30.5s), and classification
#: runs synchronously *before* run_command's own `timeout` can apply — so an oversized
#: command would hang the call at the classification step regardless of `timeout`.
MAX_CLASSIFY_BYTES = 64 * 1024

#: Closed vocabulary. `vcs_other` is deliberately distinct from `other`: a git
#: invocation whose subcommand this module does not know is a very different fact
#: from `ls`, and collapsing the two would bury the signal that the classifier
#: needs a new entry — the same reason the audit side counts `other` rather than
#: dropping it.
VERB_CLASSES: tuple[str, ...] = (
    GIT_WRITE,
    GH_WRITE,
    GIT_READ,
    GH_READ,
    VCS_OTHER,
    UNCLASSIFIED,
    OTHER,
)

#: Most significant first. A compound command takes the class of its most
#: significant segment: `git status && git push` is a write.
_PRECEDENCE = {c: i for i, c in enumerate(VERB_CLASSES)}

# Words that may precede the real command without changing what it is.
_PREFIXES = frozenset(
    {
        "sudo",
        "doas",
        "env",
        "command",
        "builtin",
        "exec",
        "nohup",
        "time",
        "nice",
        "ionice",
        "stdbuf",
        "setsid",
        "timeout",
        "xargs",
        "eval",
    }
)

#: Shells invoked as `sh -c "<command>"`. The payload is a nested command, and
#: `run_command` itself runs everything through `bash -c` — so `bash -c "git push"` is
#: the most ordinary wrapper there is, and was previously classified `other`.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"})

#: Wrappers that consume a fixed number of *positional* arguments before the command
#: they run. `timeout 30 git push` stops prefix-stripping at `30` without this, because
#: `30` is not an option — so the git write reads as `other`.
_WRAPPER_POSITIONAL_ARGS: dict[str, int] = {
    "timeout": 1,  # duration
    "flock": 1,  # lockfile
    "chrt": 1,  # priority
    "ssh": 1,  # host — the remainder runs on another machine, still a repo write
    "doas": 0,
    "su": 1,  # user
}

#: Options after which the remainder of the segment is itself a command.
_COMMAND_BEARING_FLAGS = frozenset({"-exec", "-execdir", "-c", "--command"})

# Shell operators that end one command and begin another.
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n", "(", ")", "{", "}", "!"})

_GIT_READ_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "show",
        "diff",
        "blame",
        "describe",
        "rev-parse",
        "rev-list",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "cat-file",
        "shortlog",
        "grep",
        "whatchanged",
        "show-ref",
        "for-each-ref",
        "check-ref-format",
        "check-ignore",
        "check-attr",
        "var",
        "help",
        "version",
        "verify-commit",
        "verify-tag",
        "count-objects",
        "name-rev",
        "merge-base",
        "cherry",
        "difftool",
        "instaweb",
        "annotate",
        "fsck",
    }
)

_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "push",
        "commit",
        "merge",
        "rebase",
        "reset",
        "revert",
        "cherry-pick",
        "am",
        "apply",
        "checkout",
        "switch",
        "restore",
        "add",
        "rm",
        "mv",
        "clean",
        "init",
        "clone",
        "fetch",
        "pull",
        "gc",
        "prune",
        "filter-branch",
        "filter-repo",
        "update-ref",
        "update-index",
        "symbolic-ref",
        "replace",
        "repack",
        "archive",
        "format-patch",
        "request-pull",
        "send-email",
        "daemon",
        "mergetool",
    }
)

# Subcommands that both read and write depending on how they are called. Two
# different shapes, so two tables.
#
# Sub-action driven: the first positional decides, and anything after it is that
# action's argument (`git remote show origin` is a read, origin notwithstanding).
_GIT_SUBACTION_READS: dict[str, frozenset[str]] = {
    "remote": frozenset({"show", "get-url"}),
    "stash": frozenset({"list", "show"}),
    "notes": frozenset({"list", "show"}),
    "worktree": frozenset({"list"}),
    "bundle": frozenset({"list-heads", "verify"}),
    # `git reflog` alone shows the log, but `expire`/`delete`/`drop` destroy history.
    "reflog": frozenset({"show", "exists"}),
    # `git bisect` alone prints usage; `start`/`reset`/`good`/`bad` move HEAD and write
    # BISECT_* state into .git.
    "bisect": frozenset({"log", "view", "visualize", "help"}),
    # `foreach` runs an arbitrary shell command in every submodule, so
    # `git submodule foreach 'git push'` is a write wearing a read's name. Removed from
    # the read set deliberately (CodeRabbit review, PR #7).
    "submodule": frozenset({"status", "summary"}),
}

#: Sub-action-driven subcommands whose *bare* form still writes. `git stash` with no
#: action is `git stash push` and changes local state.
_GIT_BARE_IS_WRITE = frozenset({"stash"})

# Flag driven: listing is requested by flags, and a bare positional means it is
# creating or changing something (`git tag v1.0` creates; `git tag -l 'v*'` lists).
_GIT_FLAG_READS: dict[str, frozenset[str]] = {
    "branch": frozenset(
        {
            "-l",
            "--list",
            "-a",
            "--all",
            "-r",
            "--remotes",
            "-v",
            "-vv",
            "--verbose",
            "--show-current",
            "--contains",
            "--no-contains",
            "--merged",
            "--no-merged",
            "--points-at",
            "--format",
            "--color",
            "--no-color",
            "--sort",
            "-i",
            "--ignore-case",
            "--column",
        }
    ),
    "tag": frozenset(
        {
            "-l",
            "--list",
            "-n",
            "--contains",
            "--no-contains",
            "--points-at",
            "--merged",
            "--no-merged",
            "--format",
            "--sort",
            "--color",
            "-i",
            "--ignore-case",
            "--column",
        }
    ),
    "config": frozenset(
        {
            "-l",
            "--list",
            "--get",
            "--get-all",
            "--get-regexp",
            "--get-urlmatch",
            "--show-origin",
            "--show-scope",
        }
    ),
}

#: Flags in the tables above that consume the following token as their value.
_READ_FLAG_VALUES = frozenset(
    {
        "--contains",
        "--no-contains",
        "--points-at",
        "--merged",
        "--no-merged",
        "--format",
        "--sort",
        "--color",
        "--column",
        "-n",
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
    }
)

_GH_READ_ACTIONS = frozenset(
    {
        "list",
        "view",
        "status",
        "diff",
        "checks",
        "download",
        "browse",
        "search",
        "clone",
        "help",
        "version",
        "completion",
        "config",
    }
)

_GH_WRITE_ACTIONS = frozenset(
    {
        "create",
        "merge",
        "close",
        "reopen",
        "edit",
        "delete",
        "comment",
        "review",
        "ready",
        "checkout",
        "rename",
        "fork",
        "sync",
        "run",
        "rerun",
        "cancel",
        "set",
        "add",
        "remove",
        "upload",
        "login",
        "logout",
        "refresh",
        "lock",
        "unlock",
        "pin",
        "unpin",
        "transfer",
        "archive",
        "unarchive",
        "restore",
        "approve",
        "disable",
        "enable",
        "watch",
        "develop",
        "revoke",
    }
)

_HTTP_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _tokenize(command: str) -> list[str]:
    """Best-effort tokenization that keeps shell operators as their own tokens.

    Command substitution markers are flattened to whitespace first, so a `git push`
    nested inside `$(...)` or backticks is still seen as its own segment rather than
    hiding inside a token like `$(git`.

    Unbalanced quotes make shlex raise. Falling back to a whitespace split keeps a
    malformed command classifiable rather than silently reporting `other` — which
    would be indistinguishable from a command that genuinely is not a repo write.
    """
    # Substitution markers become separators, not blanks: a nested `git push` has to
    # start its own segment or it is never the first token of one and is missed.
    # Newlines first. `whitespace_split=True` makes shlex treat a newline as ordinary
    # whitespace, so it is never emitted as a token and `\n` in _SEPARATORS could never
    # fire — which silently collapsed `git status\ngit push` into one segment beginning
    # with `git status`, classifying a push as a read. Multi-line commands are ordinary.
    normalized = command.replace("\r\n", " ; ").replace("\n", " ; ").replace("\r", " ; ")
    normalized = normalized.replace("$(", " ; ").replace("`", " ; ").replace(")", " ; ")
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return normalized.replace(";", " ; ").replace("|", " | ").replace("&", " & ").split()


def _segments(tokens: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _SEPARATORS:
            if current:
                out.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out


def _strip_prefixes(seg: list[str]) -> list[str]:
    """Drop leading env assignments and wrapper commands (sudo, env, time, ...)."""
    i = 0
    while i < len(seg):
        tok = seg[i]
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            i += 1
            continue
        name = PurePosixPath(tok).name
        if name in _PREFIXES or name in _WRAPPER_POSITIONAL_ARGS:
            i += 1
            # Skip that wrapper's own flags (and a value for the ones that take one).
            while i < len(seg) and seg[i].startswith("-"):
                takes_value = seg[i] in {
                    "-u",
                    "-g",
                    "-n",
                    "-S",
                    "--user",
                    "--group",
                    "-p",
                    "-i",
                    "-o",
                    "-l",
                }
                i += 1
                if takes_value and i < len(seg):
                    i += 1
            # ...then its positional arguments, so the command itself is reached.
            i += _WRAPPER_POSITIONAL_ARGS.get(name, 0)
            continue
        break
    return seg[i:]


#: git's own global flags that consume the following token as a value.
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)


def _first_non_flag(
    args: list[str], value_flags: frozenset[str] = frozenset()
) -> tuple[str | None, list[str]]:
    """Return the first positional token and everything after it."""
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("-"):
            if tok in value_flags:
                i += 2
            else:
                i += 1
            continue
        return tok, args[i + 1 :]
    return None, []


def _is_listing(rest: list[str], read_flags: frozenset[str]) -> bool:
    """True if every argument is a recognised listing flag (or its value).

    A bare positional — `git tag v1.0`, `git config user.email a@b.c` — means the
    command is creating or setting something, so it is not a listing. An unrecognised
    flag resolves toward write for the same reason: an unknown flag is more likely to
    be one that mutates than one this table simply forgot.

    The exception is a positional that follows an explicit listing flag: in
    `git tag -l 'v*'` the `v*` is the pattern being listed, not a tag being created.
    """
    i = 0
    saw_read_flag = False
    while i < len(rest):
        tok = rest[i]
        bare = tok.split("=", 1)[0]
        if not tok.startswith("-"):
            if saw_read_flag:
                i += 1  # a pattern argument to the listing flag
                continue
            return False
        if bare not in read_flags:
            return False
        saw_read_flag = True
        if bare in _READ_FLAG_VALUES and "=" not in tok:
            i += 2
        else:
            i += 1
    return True


def _classify_git(args: list[str]) -> str:
    sub, rest = _first_non_flag(args, _GIT_GLOBAL_VALUE_FLAGS)
    if sub is None:
        return GIT_READ  # bare `git` prints usage
    if sub in _GIT_WRITE_SUBCOMMANDS:
        return GIT_WRITE
    if sub in _GIT_READ_SUBCOMMANDS:
        return GIT_READ
    if sub in _GIT_SUBACTION_READS:
        action, _ = _first_non_flag(rest)
        if action is None:
            # bare `git remote` / `git reflog` list; but bare `git stash` is `stash push`
            return GIT_WRITE if sub in _GIT_BARE_IS_WRITE else GIT_READ
        return GIT_READ if action in _GIT_SUBACTION_READS[sub] else GIT_WRITE
    if sub in _GIT_FLAG_READS:
        return GIT_READ if _is_listing(rest, _GIT_FLAG_READS[sub]) else GIT_WRITE
    return VCS_OTHER


def _classify_gh(args: list[str]) -> str:
    sub, rest = _first_non_flag(args)
    if sub is None:
        return GH_READ
    if sub == "api":
        # `gh api` is GET unless told otherwise; -f/-F/--input imply a POST body.
        for i, tok in enumerate(rest):
            if tok in {"-X", "--method"} and i + 1 < len(rest):
                return GH_WRITE if rest[i + 1].upper() in _HTTP_WRITE_METHODS else GH_READ
            if tok.startswith("--method="):
                return GH_WRITE if tok.split("=", 1)[1].upper() in _HTTP_WRITE_METHODS else GH_READ
            if tok in {"-f", "-F", "--field", "--raw-field", "--input"}:
                return GH_WRITE
        return GH_READ
    action, _ = _first_non_flag(rest)
    if action in _GH_WRITE_ACTIONS:
        return GH_WRITE
    if action in _GH_READ_ACTIONS:
        return GH_READ
    if action is None:
        # e.g. `gh pr` alone, or a top-level verb like `gh auth`
        return GH_WRITE if sub in _GH_WRITE_ACTIONS else GH_READ
    return VCS_OTHER


def _classify_segment(seg: list[str], depth: int) -> str:
    """Classify one already-separated command, recursing into nested payloads."""
    seg = _strip_prefixes(seg)
    if not seg:
        return OTHER

    name = PurePosixPath(seg[0]).name
    rest = seg[1:]

    # `bash -c "git push"` — the payload is a whole command in one token. Recurse.
    # Without this the most ordinary wrapper on the system reads as `other`, and any
    # block built on this classification would be bypassed by the same three characters.
    if name in _SHELLS and depth < _MAX_DEPTH:
        for i, tok in enumerate(rest):
            # -c, and combined forms like -lc / -ec that end in c
            if tok == "-c" or (
                tok.startswith("-") and not tok.startswith("--") and tok.endswith("c")
            ):
                if i + 1 < len(rest):
                    return classify_verb(rest[i + 1], _depth=depth + 1)
                break
        return OTHER

    # `find . -exec git push {} ;` — the command follows the flag, not quoted.
    if depth < _MAX_DEPTH:
        for i, tok in enumerate(rest):
            if tok in _COMMAND_BEARING_FLAGS and i + 1 < len(rest):
                tail = [t for t in rest[i + 1 :] if t not in {"{}", ";", "\\;", "+"}]
                if tail:
                    return _classify_segment(tail, depth + 1)

    if name == "git":
        return _classify_git(rest)
    if name == "gh":
        return _classify_gh(rest)
    return OTHER


#: Guards the shell/-exec recursion. `bash -c "bash -c '...'"` is legitimate but nesting
#: deeper than this is not something to spend an unbounded parse on.
_MAX_DEPTH = 4


def classify_verb(command: str, _depth: int = 0) -> str:
    """Return the most significant VERB_CLASSES member the command performs.

    Returns `unclassified` — never `other` — when the command is too long to parse.
    `other` is a positive claim that this was not a repo operation, and a parse that
    was declined cannot support it. Anything gating on this must be able to tell "we
    looked and it was fine" from "we did not look".
    """
    truncated = len(command) > MAX_CLASSIFY_BYTES
    if truncated:
        command = command[:MAX_CLASSIFY_BYTES]

    best = OTHER
    for seg in _segments(_tokenize(command)):
        found = _classify_segment(seg, _depth)
        if _PRECEDENCE[found] < _PRECEDENCE[best]:
            best = found

    if truncated and best == OTHER:
        return UNCLASSIFIED
    return best
