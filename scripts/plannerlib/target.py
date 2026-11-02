"""Which repository a ticket is planned against.

`prepare` resolves the target once and binds KEY -> toplevel in the agent's targets file. Every later
command, and the write/git hooks, use that binding, so the agent can only touch the confirmed repo.
"""

import glob
import os
import pathlib

from .common import (
    EXIT_ASK_USER, EXIT_ENV, PlannerError, bound_toplevel, intake_state_file, load_policy, load_targets,
    resolve_toplevel,
)


def search_roots(policy):
    cfg = policy["repoSearch"]
    return [os.path.expanduser(r) for r in cfg["roots"]], cfg["maxDepth"]


def _glob_levels(roots, max_depth, *tail):
    for root in roots:
        for depth in range(1, max_depth + 1):
            yield from glob.glob(os.path.join(glob.escape(root), *["*"] * (depth - 1), *tail))


def find_repos_named(name, roots, max_depth):
    """Git repos whose folder name is `name`, up to `max_depth` levels below each root."""
    return sorted({os.path.realpath(d) for d in _glob_levels(roots, max_depth, glob.escape(name))
                   if os.path.exists(os.path.join(d, ".git"))})


def find_intake_states(key, roots, max_depth):
    """Repos holding a jira-intake-agent state file for `key` (the intake agent already resolved them)."""
    return sorted({os.path.realpath(pathlib.Path(f).parents[2])
                   for f in _glob_levels(roots, max_depth, intake_state_file("*", glob.escape(key)))})


def _choices(found):
    return "Candidates:\n  " + "\n  ".join(found) if found else "No candidates found."


def resolve_repo(arg, roots, max_depth):
    """`arg` is a path, or a bare folder name searched for under the configured roots. Returns (toplevel, via)."""
    if os.sep in arg or arg.startswith("~") or arg in (".", ".."):
        return resolve_toplevel(arg), "path"
    found = find_repos_named(arg, roots, max_depth)
    if len(found) == 1:
        return resolve_toplevel(found[0]), "name"
    raise PlannerError(
        f"repo name {arg!r} matched {len(found)} git repos under {roots}. {_choices(found)}\n"
        f"Ask the user which repo to use and rerun with --repo <absolute path>.", EXIT_ASK_USER)


def resolve_for_prepare(key, repo, hint, policy):
    """Order: --repo, the brief's repository.toplevel / an existing binding (must agree), an intake state file, else ask."""
    roots, depth = search_roots(policy)
    if repo:
        return resolve_repo(repo, roots, depth)
    bound = load_targets().get(key)
    hinted = resolve_toplevel(hint) if hint else None
    if bound and hinted and hinted != bound["toplevel"]:
        raise PlannerError(
            f"{key} is bound to {bound['toplevel']} but the brief names {hinted}. Ask the user which repo is "
            f"right and rerun with --repo <path or name>.", EXIT_ASK_USER)
    if hinted:
        return hinted, "brief"
    if bound:
        return resolve_toplevel(bound["toplevel"]), bound["resolvedVia"]
    found = find_intake_states(key, roots, depth)
    if len(found) == 1:
        return resolve_toplevel(found[0]), "intake-state"
    raise PlannerError(
        f"which repository is {key} for? {_choices(found)}\n"
        f"Ask the user (\"{key} is for repo …\") and rerun with --repo <name or absolute path>.", EXIT_ASK_USER)


def bound_target(key, repo=None):
    """Toplevel bound to `key` by prepare. A --repo given here must agree with the binding."""
    top = bound_toplevel(key)
    if not top:
        raise PlannerError(f"{key} is not bound to a repository; run `planner.py prepare {key}` first", EXIT_ENV)
    if repo:
        given = resolve_repo(repo, *search_roots(load_policy()))[0]
        if given != top:
            raise PlannerError(f"{key} is bound to {top}, not {given}. Ask the user which is right; "
                               f"rerun prepare with --repo to rebind.", EXIT_ASK_USER)
    return top
