"""Shared helpers: paths, JSON io, KEY validation, git, schema validation, glob matching."""

import functools
import json
import os
import re
import subprocess
import tempfile

AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLANNER_NAME = "implementation-planner-agent"
PLANNER_VERSION = "1.0.0"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

# Exit codes (documented in README).
EXIT_OK = 0
EXIT_INVALID = 1     # fixable validation errors; output lists them
EXIT_ENV = 2         # environment / IO problem
EXIT_ASK_USER = 3    # need input from the user (which repo?, missing brief, attempts exhausted)


class PlannerError(Exception):
    """Fatal error with an exit code."""

    def __init__(self, message, code=EXIT_ENV):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- io

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_json_or(path, default):
    try:
        return load_json(path)
    except FileNotFoundError:
        return default


def unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def duplicates(items):
    seen, dup = set(), set()
    for i in items:
        (dup if i in seen else seen).add(i)
    return sorted(dup)


def read_text(path, limit=-1):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(limit)


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        unlink_quiet(tmp)
        raise


def targets_path():
    # env override keeps tests away from the real bindings file
    return os.environ.get("PLANNER_TARGETS_FILE") or os.path.join(AGENT_ROOT, ".claude", "targets.json")


def load_targets():
    """KEY -> {"toplevel", "resolvedVia"}: the repo each ticket was bound to by `prepare` (toplevel is a realpath)."""
    data = load_json_or(targets_path(), {})
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("toplevel"), str)}


def bound_toplevel(key):
    return load_targets().get(key, {}).get("toplevel")


def bound_toplevels():
    return {t["toplevel"] for t in load_targets().values()}


def bind_target(key, toplevel, via):
    targets = load_targets()
    entry = {"toplevel": toplevel, "resolvedVia": via}
    if targets.get(key) != entry:
        targets[key] = entry
        write_json_atomic(targets_path(), targets)


def load_policy():
    return load_json(os.path.join(AGENT_ROOT, "policy.json"))


def load_schema(name):
    return load_json(os.path.join(AGENT_ROOT, "schemas", name))


# ---------------------------------------------------------------- keys & paths

def require_key(key):
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise PlannerError(f"invalid ticket key {key!r}; expected e.g. PROJ-1234", EXIT_INVALID)
    return key


def plans_dir(toplevel):
    return os.path.join(toplevel, ".claude", "plans")


def plan_file(toplevel, key, kind):
    """kind: input | context | draft | plan | attempts | decisions-cache"""
    return os.path.join(plans_dir(toplevel), f"{require_key(key)}.{kind}.json")


JIRA_CYCLE = os.path.join(".claude", "jira-cycle")


def intake_state_file(toplevel, key):
    """Where jira-intake-agent writes its state file for `key`."""
    return os.path.join(toplevel, JIRA_CYCLE, f"{key}.json")


def is_within(child, parent):
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent + os.sep)


def check_repo_path(toplevel, rel):
    """Return an error string if `rel` is not a safe repo-relative path, else None."""
    if not isinstance(rel, str) or not rel.strip():
        return "empty path"
    if os.path.isabs(rel) or rel.startswith("~"):
        return "path must be repo-relative, not absolute"
    parts = rel.replace("\\", "/").split("/")
    if ".." in parts:
        return "path must not contain '..'"
    if parts[0] == ".git":
        return "path must not be inside .git"
    if not is_within(os.path.join(toplevel, rel), toplevel):
        return "path resolves outside the repository (symlink?)"
    return None


def normalize_rel(rel):
    rel = rel.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


# ---------------------------------------------------------------- git

_GIT_HARDEN = ["-c", "core.fsmonitor=", "-c", "core.pager=cat", "-c", "core.hooksPath=/dev/null"]


def git(toplevel, *args, check=True):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    try:
        proc = subprocess.run(
            ["git", "-C", toplevel, *_GIT_HARDEN, *args],
            capture_output=True, text=True, env=env, timeout=60,
        )
    except FileNotFoundError:
        raise PlannerError("git not found on PATH")
    if check and proc.returncode != 0:
        raise PlannerError(f"git {' '.join(args)} failed in {toplevel}: {proc.stderr.strip()}")
    return proc.stdout.strip() if proc.returncode == 0 else None


_SECTION = re.compile(r'^\s*\[\s*([^\]\s"]+)(?:\s+"([^"]*)")?\s*\]')
_KEY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s*(?:=|$)")


def _git_config_files(toplevel):
    dot = os.path.join(toplevel, ".git")
    if os.path.isfile(dot):  # worktree / submodule: ".git" is a "gitdir: <path>" pointer
        gitdir = read_text(dot).partition("gitdir:")[2].strip()
        gitdir = os.path.normpath(os.path.join(toplevel, gitdir))
        common = os.path.join(gitdir, "commondir")
        dirs = [gitdir] + ([os.path.normpath(os.path.join(gitdir, read_text(common).strip()))]
                           if os.path.isfile(common) else [])
    else:
        dirs = [dot]
    return [os.path.join(d, n) for d in dirs for n in ("config", "config.worktree")]


def risky_git_config(toplevel):
    """Repo-local git settings that make read-only git commands run programs. Returns 'section.key' strings.

    Agent git calls cannot be hardened by a hook, so a repo whose own config sets any of these is refused.
    """
    found = set()
    for path in _git_config_files(toplevel):
        try:
            text = read_text(path)
        except OSError:
            continue
        section = sub = ""
        for line in text.splitlines():
            m = _SECTION.match(line)
            if m:
                head = m.group(1).lower()
                section, _, dotted = head.partition(".")
                sub = m.group(2) if m.group(2) is not None else dotted
                if section in ("include", "includeif", "filter", "pager"):
                    found.add(section)
                continue
            k = _KEY.match(line)
            if not k:
                continue
            key = k.group(1).lower()
            if (section, key) in (("core", "fsmonitor"), ("core", "pager"), ("diff", "external"), ("gpg", "program")) \
                    or (section == "diff" and sub and key in ("textconv", "command")) \
                    or (section == "gpg" and sub and key == "program"):
                found.add(f"{section}.{key}")
    return sorted(found)


def resolve_toplevel(path):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise PlannerError(f"repository path does not exist: {path}")
    top = git(path, "rev-parse", "--show-toplevel", check=False)
    if not top:
        raise PlannerError(f"not a git working copy: {path}")
    return os.path.realpath(top)


# ---------------------------------------------------------------- glob

@functools.lru_cache(maxsize=None)
def glob_to_regex(pattern):
    """Translate a gitignore-ish glob ('**' spans directories, '*' does not) to a compiled regex."""
    i, out = 0, []
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path, patterns):
    return any(glob_to_regex(p).match(path) for p in patterns)


# ---------------------------------------------------------------- schema validation

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "number": (int, float), "integer": int, "null": type(None),
}
_KEYWORDS = {
    "$schema", "title", "description", "type", "required", "properties", "items", "enum",
    "const", "pattern", "additionalProperties", "minItems", "anyOf",
}


def _type_ok(value, t):
    if t in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[t])


def validate(value, schema, path="$"):
    """Validate against the supported JSON Schema subset. Returns a list of error strings.

    Unknown keywords raise, so a schema can never silently exceed what is enforced.
    """
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise ValueError(f"schema at {path} uses unsupported keywords: {sorted(unknown)}")
    errors = []
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, t) for t in types):
            return [f"{path}: expected {'|'.join(types)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errors.append(f"{path}: does not match pattern {schema['pattern']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required field '{req}'")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errors.extend(validate(v, props[k], f"{path}.{k}"))
            elif extra is False:
                errors.append(f"{path}: unexpected field '{k}'")
            elif isinstance(extra, dict):
                errors.extend(validate(v, extra, f"{path}.{k}"))
    if "anyOf" in schema and not any(not validate(value, s, path) for s in schema["anyOf"]):
        errors.append(f"{path}: does not satisfy any allowed alternative ({len(schema['anyOf'])} options)")
    return errors
