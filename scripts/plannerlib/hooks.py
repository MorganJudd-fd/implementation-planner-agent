"""PreToolUse guards. Deny by printing hookSpecificOutput JSON and exiting 0.

(Exiting 2 makes Claude Code ignore stdout JSON, which would drop the deny reason.)
"""

import json
import os
import re
import shlex
import sys

from .common import AGENT_ROOT, bound_toplevel, bound_toplevels, risky_git_config

_WRITABLE = re.compile(r"(?P<top>.+)/\.claude/plans/(?P<key>[A-Z][A-Z0-9]+-\d+)\.(draft|input)\.json")
_GIT_READ = {"rev-parse", "status", "log", "show", "diff", "ls-files"}
# prefixes, so abbreviated long options (git accepts unambiguous ones on some versions) are caught too
_GIT_BAD_FLAG = re.compile(r"-c|-O.*|--ou.*|--op.*|--ext.*|--exe.*|--textc.*")
_META = set("|&;<>`$(){}\n\r")
_SCRIPT = os.path.join(AGENT_ROOT, "scripts", "planner.py")


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"implementation-planner-agent: {reason}"}}


def check_write(tool_input):
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return _deny("write has no file path")
    real = os.path.realpath(path)
    m = _WRITABLE.fullmatch(real)
    if m and bound_toplevel(m["key"]) == m["top"]:
        return None
    if m:
        return _deny(f"{m['key']} is not bound to {m['top']}. Run `planner.py prepare {m['key']} --repo <target>` "
                     f"first; the plan files may only be written in the repo prepare bound the ticket to.")
    return _deny(
        f"writes are limited to <repo>/.claude/plans/<KEY>.draft.json and <KEY>.input.json. "
        f"{path} is not allowed. This agent plans; it never implements. Context and plan files are "
        f"written only by scripts/planner.py.")


def _enclosing_repo(path):
    path = os.path.realpath(path)
    while True:
        if os.path.lexists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _git_dir_problem(where):
    """git runs only in this agent repo or a bound target (not a nested repo), and never under risky repo config."""
    repo = _enclosing_repo(where)
    if repo not in bound_toplevels() | {os.path.realpath(AGENT_ROOT)}:
        return f"git may only run in this agent repo or a bound target repo, not {where}; run prepare first"
    risky = risky_git_config(repo)
    if risky:
        return f"{repo} git config sets {', '.join(risky)}, which can run programs; git is blocked there"
    return None


def _check_git(argv, cwd):
    i = 1
    where = cwd or AGENT_ROOT
    if len(argv) > 2 and argv[1] == "-C":
        i = 3
        where = os.path.join(where, os.path.expanduser(argv[2]))
    problem = _git_dir_problem(where)
    if problem:
        return problem
    if len(argv) <= i or argv[i] not in _GIT_READ:
        return f"git subcommand not allowed; read-only subcommands: {sorted(_GIT_READ)}"
    for a in argv[i + 1:]:
        if _GIT_BAD_FLAG.fullmatch(a):
            return f"git flag {a!r} is not allowed (can write files or run commands)"
    for a in argv[1:i]:
        if a.startswith("-") and a != "-C":
            return f"git global option {a!r} is not allowed"
    return None


def check_bash(tool_input, cwd=None):
    cmd = tool_input.get("command")
    if not isinstance(cmd, str):
        return _deny("command must be a string")
    if any(ch in _META for ch in cmd) or any(ord(ch) < 32 and ch != "\t" for ch in cmd):
        return _deny("shell operators, redirection, substitution and multi-line commands are not allowed. "
                     "Use the Read, Glob and Grep tools to inspect files.")
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return _deny(f"could not parse command: {e}")
    if not argv:
        return _deny("empty command")
    if argv[0] in ("python3", "python") and len(argv) >= 2 and \
            os.path.realpath(os.path.join(cwd or AGENT_ROOT, os.path.expanduser(argv[1]))) == _SCRIPT:
        return None
    if argv[0] == "git":
        err = _check_git(argv, cwd)
        return _deny(err) if err else None
    return _deny(f"command {argv[0]!r} is not allowed. Allowed: `python3 scripts/planner.py ...` and read-only git "
                 f"({', '.join(sorted(_GIT_READ))}). Use Read, Glob and Grep to inspect files.")


def main(kind):
    # fail closed: an uncaught exception would exit 1, which Claude Code treats as non-blocking
    try:
        data = json.load(sys.stdin)
        tool_input = data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            raise TypeError("tool_input is not an object")
        result = check_write(tool_input) if kind == "writes" else check_bash(tool_input, data.get("cwd"))
    except Exception as e:
        result = _deny(f"hook could not process its input ({type(e).__name__})")
    if result:
        print(json.dumps(result))
    return 0
