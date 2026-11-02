"""`prepare`: load + normalize the brief, discover repo facts, write <KEY>.context.json."""

import glob
import json
import os
import re

from .common import (
    EXIT_ASK_USER, EXIT_INVALID, JIRA_CYCLE, PlannerError, bind_target, duplicates, git, glob_match,
    intake_state_file, load_json, load_policy, load_schema, plan_file, read_text, require_key, risky_git_config,
    unlink_quiet, validate, write_json_atomic,
)
from .target import resolve_for_prepare

# ---------------------------------------------------------------- brief


def is_brief_path(source):
    return source.endswith(".json") or os.sep in source


def find_brief(source, toplevel):
    """Return (path, key). `source` is a ticket key or a path to a brief JSON file."""
    if is_brief_path(source):
        path = os.path.abspath(os.path.expanduser(source))
        if not os.path.isfile(path):
            raise PlannerError(f"brief file not found: {path}")
        return path, None
    key = require_key(source)
    for candidate in (
        intake_state_file(toplevel, key),
        plan_file(toplevel, key, "input"),
    ):
        if os.path.isfile(candidate):
            return candidate, key
    raise PlannerError(
        f"no brief found for {key}. Looked in .claude/jira-cycle/{key}.json and "
        f".claude/plans/{key}.input.json. Ask the user for the ticket brief (summary, description, "
        f"acceptance criteria, ambiguities) and write it to .claude/plans/{key}.input.json.",
        EXIT_ASK_USER,
    )


def normalize_brief(raw):
    errors = validate(raw, load_schema("input-brief.schema.json"))
    if errors:
        raise PlannerError("brief is invalid:\n  " + "\n  ".join(errors), EXIT_INVALID)
    ac = [
        {"id": a["id"], "assertion": a["assertion"], "proposed": False}
        for a in raw.get("acceptanceCriteria", [])
    ] + [
        {"id": a["id"], "assertion": a["assertion"], "proposed": True}
        for a in raw.get("proposedAcceptanceCriteria", [])
    ]
    dupes = duplicates(a["id"] for a in ac)
    if dupes:
        raise PlannerError(f"brief has duplicate acceptance criterion ids: {dupes}", EXIT_INVALID)
    ambiguities = [
        {"id": f"AMB{i}", "question": a["question"], "whyItMatters": a.get("whyItMatters", "")}
        for i, a in enumerate(raw.get("ambiguities", []), 1)
    ]
    ticket = raw["ticket"]
    return {
        "ticket": {"key": ticket["key"], "summary": ticket["summary"], "url": ticket.get("url")},
        "description": raw["description"],
        "changeType": (raw.get("classification") or {}).get("changeType"),
        "acceptanceCriteria": ac,
        "ambiguities": ambiguities,
    }


# ---------------------------------------------------------------- test commands

_LOCKFILES = [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun")]
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)")
_RUN_LINE = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")


def _line_of(text, needle):
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    return 1


def _package_json_commands(top, rel, pm):
    text = read_text(os.path.join(top, rel))
    scripts = (json.loads(text).get("scripts") or {})
    prefix = os.path.dirname(rel)
    out = []
    for name in scripts:
        source = f"{rel}:{_line_of(text, json.dumps(name))}"
        if prefix:
            flag = "--prefix" if pm == "npm" else "--dir"
            out.append({"command": f"{pm} {flag} {prefix} run {name}", "source": source})
            continue
        out.append({"command": f"{pm} run {name}", "source": source})
        if name == "test":
            out.append({"command": f"{pm} test", "source": source})
    return out


def _makefile_commands(top, rel):
    text = read_text(os.path.join(top, rel))
    out, seen = [], set()
    for i, line in enumerate(text.splitlines(), 1):
        m = _MAKE_TARGET.match(line)
        if m and m.group(1) not in seen and not m.group(1).startswith("."):
            seen.add(m.group(1))
            out.append({"command": f"make {m.group(1)}", "source": f"{rel}:{i}"})
    return out


def _workflow_commands(top, rel):
    """Extract `run:` commands, including `|` / `>` block scalars, without a YAML library."""
    lines = read_text(os.path.join(top, rel)).splitlines()
    out, i = [], 0
    while i < len(lines):
        m = _RUN_LINE.match(lines[i])
        if not m:
            i += 1
            continue
        value = m.group(2).strip()
        if value[:1] in ("|", ">"):
            base = len(m.group(1))
            j = i + 1
            while j < len(lines):
                line = lines[j]
                if line.strip() and len(line) - len(line.lstrip()) <= base:
                    break
                cmd = line.strip()
                if cmd and not cmd.startswith("#"):
                    out.append({"command": cmd, "source": f"{rel}:{j + 1}"})
                j += 1
            i = j
            continue
        value = value.strip("'\"")
        if value:
            out.append({"command": value, "source": f"{rel}:{i + 1}"})
        i += 1
    return out


def _python_commands(top, files):
    out = []
    for rel in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"):
        if rel not in files:
            continue
        text = read_text(os.path.join(top, rel))
        if rel == "tox.ini":
            out.append({"command": "tox", "source": f"{rel}:1"})
        if "pytest" in text or rel == "pytest.ini":
            source = f"{rel}:{_line_of(text, 'pytest')}"
            out.append({"command": "pytest", "source": source})
            if "uv.lock" in files:
                out.append({"command": "uv run pytest", "source": source})
            if "poetry.lock" in files:
                out.append({"command": "poetry run pytest", "source": source})
    return out


def discover_test_commands(top, files):
    pm = next((name for lock, name in _LOCKFILES if lock in files), "npm")
    found = []
    for rel in sorted(f for f in files if f == "package.json" or f.endswith("/package.json")):
        if "node_modules/" in rel:
            continue
        try:
            found += _package_json_commands(top, rel, pm)
        except (OSError, ValueError):
            continue
    for rel in ("Makefile", "makefile", "GNUmakefile"):
        if rel in files:
            found += _makefile_commands(top, rel)
    found += _python_commands(top, files)
    for rel in sorted(f for f in files if re.match(r"^\.github/workflows/[^/]+\.ya?ml$", f)):
        found += _workflow_commands(top, rel)
    unique = {}
    for entry in found:
        unique.setdefault(entry["command"], entry)
    return list(unique.values())


# ---------------------------------------------------------------- agents

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.S)


def _frontmatter(path):
    try:
        text = read_text(path, 8192)
    except OSError:
        return {}
    m = _FRONTMATTER.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def discover_agents(top, user_agents_dir=None):
    user_agents_dir = user_agents_dir or os.path.expanduser("~/.claude/agents")
    agents = {}
    for scope, directory in (("installed-user", user_agents_dir), ("installed-project", os.path.join(top, ".claude", "agents"))):
        for path in sorted(glob.glob(os.path.join(directory, "*.md"))):
            fm = _frontmatter(path)
            name = fm.get("name") or os.path.splitext(os.path.basename(path))[0]
            # project scope is processed last so it overrides a user agent of the same name
            agents[name] = {"name": name, "description": fm.get("description", ""), "scope": scope, "path": path}
    return sorted(agents.values(), key=lambda a: a["name"])


# ---------------------------------------------------------------- ignore


def ensure_excluded(top):
    """Ignore .claude/plans/ via .git/info/exclude so no tracked file changes. Returns True if added."""
    git_dir = git(top, "rev-parse", "--absolute-git-dir")
    exclude = os.path.join(git_dir, "info", "exclude")
    entry = "/.claude/plans/"
    try:
        existing = read_text(exclude)
    except FileNotFoundError:
        existing = ""
    if entry in existing.splitlines():
        return False
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"# implementation-planner-agent scratch output\n{entry}\n")
    return True


# ---------------------------------------------------------------- entry


def _load_brief(path):
    try:
        raw = load_json(path)
    except ValueError as e:
        raise PlannerError(f"brief is not valid JSON ({path}): {e}", EXIT_INVALID)
    if not isinstance(raw, dict):
        raise PlannerError(f"brief must be a JSON object ({path})", EXIT_INVALID)
    return raw


def run(source, repo=None, user_agents_dir=None):
    """`repo` is a path or folder name; when omitted the target is found (see target.resolve_for_prepare)."""
    policy = load_policy()
    raw = brief_path = key_hint = hint = None
    if is_brief_path(source):
        brief_path, _ = find_brief(source, None)
        raw = _load_brief(brief_path)
        hint = (raw.get("repository") or {}).get("toplevel")
        key = require_key((raw.get("ticket") or {}).get("key"))
    else:
        key = require_key(source)
    top, via = resolve_for_prepare(key, repo, hint, policy)
    risky = risky_git_config(top)
    if risky:
        raise PlannerError(
            f"{top}/.git/config sets {', '.join(risky)}, which can make git run programs. Show the user; "
            f"planning is refused until they remove it.", EXIT_ASK_USER)
    # bound before the brief lookup: when none exists the hook must let the agent write <KEY>.input.json
    bind_target(key, top, via)
    if raw is None:
        brief_path, key_hint = find_brief(source, top)
        raw = _load_brief(brief_path)
    brief = normalize_brief(raw)
    key = brief["ticket"]["key"]
    if key_hint and key_hint != key:
        raise PlannerError(f"brief at {brief_path} is for {key}, not {key_hint}", EXIT_INVALID)

    excluded = ensure_excluded(top)
    files = [f for f in git(top, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0") if f]
    context = {
        "ticketKey": key,
        "briefSource": brief_path,
        "brief": brief,
        "repository": {
            "toplevel": top,
            "resolvedVia": via,
            "originUrl": git(top, "remote", "get-url", "origin", check=False),
            "gitHead": git(top, "rev-parse", "HEAD", check=False),
            # scratch dirs written by planning agents do not count as local changes
            "dirty": bool(git(top, "status", "--porcelain", "--", ".", ":!.claude/plans", f":!{JIRA_CYCLE}")),
        },
        "testCommands": discover_test_commands(top, set(files)),
        "agents": discover_agents(top, user_agents_dir),
        "highChurnFiles": sorted(f for f in files if glob_match(f, policy["highChurn"])),
        "excludeAdded": excluded,
        "policyVersion": policy["version"],
    }
    write_json_atomic(plan_file(top, key, "context"), context)
    unlink_quiet(plan_file(top, key, "attempts"))
    return context


def summary(ctx):
    """Compact JSON the CLI prints after prepare."""
    top, key = ctx["repository"]["toplevel"], ctx["ticketKey"]
    return {
        "ok": True, "ticketKey": key, "repo": top, "resolvedVia": ctx["repository"]["resolvedVia"],
        "contextPath": plan_file(top, key, "context"),
        "draftPath": plan_file(top, key, "draft"),
        "acceptanceCriteria": [a["id"] for a in ctx["brief"]["acceptanceCriteria"]],
        "ambiguities": [a["id"] for a in ctx["brief"]["ambiguities"]],
        "testCommands": len(ctx["testCommands"]), "agents": [a["name"] for a in ctx["agents"]],
        "dirty": ctx["repository"]["dirty"],
        "excludeAdded": ctx["excludeAdded"],
    }
