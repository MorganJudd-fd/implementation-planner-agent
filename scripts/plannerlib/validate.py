"""Deterministic draft checks and wave computation. No judgment calls live here."""

import os
import re

from .common import check_repo_path, duplicates, load_schema, normalize_rel, validate

ERROR, WARNING = "error", "warning"


def issue(severity, code, path, message, fix):
    return {"severity": severity, "code": code, "path": path, "message": message, "fix": fix}


def command_allowed(cmd, allowlist):
    cmd = " ".join(cmd.split())
    return any(cmd == a or cmd.startswith(a + " ") for a in allowlist)


def _cycle(ws_by_id):
    """Return a list of ids forming a dependency cycle, or None."""
    state = {}

    def visit(node, stack):
        state[node] = "active"
        stack.append(node)
        for dep in ws_by_id[node]["dependsOn"]:
            if dep not in ws_by_id:
                continue
            if state.get(dep) == "active":
                return stack[stack.index(dep):] + [dep]
            if dep not in state:
                found = visit(dep, stack)
                if found:
                    return found
        stack.pop()
        state[node] = "done"
        return None

    for node in ws_by_id:
        if node not in state:
            found = visit(node, [])
            if found:
                return found
    return None


def compute_waves(workstreams):
    """Kahn layering: a workstream lands in the wave after its latest dependency."""
    remaining = {w["id"]: set(w["dependsOn"]) for w in workstreams}
    order = [w["id"] for w in workstreams]
    waves, done = [], set()
    while remaining:
        ready = [i for i in order if i in remaining and remaining[i] <= done]
        if not ready:
            raise ValueError("dependency cycle")  # validate_draft rejects cycles first
        waves.append({"wave": len(waves) + 1, "parallel": len(ready) > 1, "workstreams": ready})
        done.update(ready)
        for i in ready:
            del remaining[i]
    return waves


def _path_problem(top, rel):
    """Error text if `rel` is unsafe or does not exist, else None."""
    return check_repo_path(top, rel) or (None if os.path.exists(os.path.join(top, rel)) else "file does not exist")


def _unlisted(cmds, allow):
    return [c for c in cmds if not command_allowed(c, allow)]


def _mentions_path(step, paths):
    text = step.replace("`", " ").replace("\\", "/")
    return any(p in text or os.path.basename(p) in text for p in paths)


def validate_draft(draft, context, current_head, limits):
    """Return a list of issues. Any severity=error blocks finalize."""
    issues = []
    for msg in validate(draft, load_schema("plan-draft.schema.json"), "draft"):
        issues.append(issue(ERROR, "schema", msg.split(":")[0], msg, "Edit the draft to match schemas/plan-draft.schema.json."))
    if issues:
        return issues  # structural problems make the semantic checks unreliable

    top = context["repository"]["toplevel"]
    key = context["ticketKey"]
    allow = [c["command"] for c in context["testCommands"]]
    acs = {a["id"]: a for a in context["brief"]["acceptanceCriteria"]}
    ambs = {a["id"] for a in context["brief"]["ambiguities"]}
    wss = draft["workstreams"]

    if draft["ticketKey"] != key:
        issues.append(issue(ERROR, "ticket_mismatch", "draft.ticketKey", f"draft is for {draft['ticketKey']}, context is {key}", f"Set ticketKey to {key}."))
    if context["repository"]["gitHead"] != current_head:
        issues.append(issue(ERROR, "stale_context", "context.gitHead", "repository HEAD moved since prepare", "Rerun `planner.py prepare`, re-check findings, then finalize."))
    if len(wss) > limits["maxWorkstreams"]:
        issues.append(issue(ERROR, "too_many_workstreams", "draft.workstreams", f"{len(wss)} workstreams exceeds {limits['maxWorkstreams']}", "Merge small workstreams; a workstream per one-line edit is overhead."))

    for i, f in enumerate(draft["codebaseFindings"]):
        err = _path_problem(top, normalize_rel(re.split(r":\d", f["evidence"], maxsplit=1)[0]))
        if err:
            issues.append(issue(ERROR, "evidence_missing", f"draft.codebaseFindings[{i}].evidence", f"{f['evidence']!r}: {err}", "Cite a real repo-relative path, optionally with :line."))

    for dup in duplicates(w["id"] for w in wss):
        issues.append(issue(ERROR, "duplicate_id", "draft.workstreams", f"workstream id {dup} used more than once", "Give every workstream a unique wsN id."))
    by_id = {w["id"]: w for w in wss}

    owners = {}
    for w in wss:
        p = f"draft.workstreams[{w['id']}]"
        if len(w["filesOwned"]) > limits["maxFilesOwnedPerWorkstream"]:
            issues.append(issue(ERROR, "too_many_files", f"{p}.filesOwned", f"{len(w['filesOwned'])} files exceeds {limits['maxFilesOwnedPerWorkstream']}", "Split into independently verifiable workstreams."))
        for fo in w["filesOwned"]:
            rel = normalize_rel(fo["path"])
            err = check_repo_path(top, rel)
            exists = not err and os.path.lexists(os.path.join(top, rel))
            if err:
                issues.append(issue(ERROR, "unsafe_path", f"{p}.filesOwned", f"{fo['path']!r}: {err}", "Use a repo-relative path inside the repository."))
            elif fo["action"] == "create" and exists:
                issues.append(issue(ERROR, "create_exists", f"{p}.filesOwned", f"{rel} already exists", "Use action 'modify' for existing files."))
            elif fo["action"] != "create" and not exists:
                issues.append(issue(ERROR, "file_missing", f"{p}.filesOwned", f"{rel} does not exist", "Fix the path, or use action 'create' for a new file."))
            owners.setdefault(rel, []).append(w["id"])
        for rel in w["filesRead"]:
            err = _path_problem(top, normalize_rel(rel))
            if err:
                issues.append(issue(ERROR, "read_missing", f"{p}.filesRead", f"{rel!r}: {err}", "List only existing repo files."))
        for dep in w["dependsOn"]:
            if dep == w["id"] or dep not in by_id:
                issues.append(issue(ERROR, "bad_dependency", f"{p}.dependsOn", f"{dep!r} is not another workstream", "Reference existing workstream ids only."))
        if not w["verification"] and not w.get("manualVerification", "").strip():
            issues.append(issue(ERROR, "no_verification", f"{p}.verification", "workstream has no verification", "Add an allowlisted command, or set manualVerification when none can prove it."))
        for cmd in _unlisted(w["verification"], allow):
                issues.append(issue(ERROR, "command_not_allowlisted", f"{p}.verification", f"{cmd!r} is not a discovered command", "Use a command from context.testCommands (extra trailing args allowed), or move the check to manualVerification."))
        for ac in w["acceptanceCriteria"]:
            if ac not in acs:
                issues.append(issue(ERROR, "unknown_ac", f"{p}.acceptanceCriteria", f"{ac!r} is not in the brief", f"Use ids from the brief: {sorted(acs)}."))
            elif acs[ac]["proposed"]:
                issues.append(issue(WARNING, "proposed_ac", f"{p}.acceptanceCriteria", f"{ac} is a proposed criterion awaiting confirmation", "Confirm at review."))
        paths = [normalize_rel(f["path"]) for f in w["filesOwned"]] + [normalize_rel(r) for r in w["filesRead"]]
        for si, step in enumerate(w["steps"]):
            if not _mentions_path(step, paths):
                issues.append(issue(WARNING, "step_without_path", f"{p}.steps[{si}]", "step names none of this workstream's files", "Name the file each step lands in."))

    for rel, who in sorted(owners.items()):
        if len(who) > 1:
            issues.append(issue(ERROR, "ownership_overlap", "draft.workstreams", f"{rel} is owned by {', '.join(who)}", "Give the file exactly one owner; serialize or merge the workstreams."))

    cyc = _cycle(by_id) if len(by_id) == len(wss) else None
    if cyc:
        issues.append(issue(ERROR, "dependency_cycle", "draft.workstreams", " -> ".join(cyc), "Break the cycle; land a shared interface as its own workstream."))

    v = draft["verification"]
    for cmd in _unlisted(v["fullSuite"], allow):
            issues.append(issue(ERROR, "command_not_allowlisted", "draft.verification.fullSuite", f"{cmd!r} is not a discovered command", "Use commands from context.testCommands."))
    if not v["fullSuite"] and not v["manualChecks"]:
        issues.append(issue(ERROR, "no_full_suite", "draft.verification", "no full-suite gate and no manual checks", "Add a discovered command to fullSuite, or explain the manual gate in manualChecks."))

    mapped = {ac for w in wss for ac in w["acceptanceCriteria"]}
    uncovered = {u["id"] for u in draft.get("uncoveredAcceptanceCriteria", [])}
    for ac in acs:
        if ac not in mapped and ac not in uncovered:
            issues.append(issue(ERROR, "ac_unmapped", "draft.workstreams", f"{ac} is not covered by any workstream", "Map it to a workstream, or list it in uncoveredAcceptanceCriteria with a reason."))
    for ac in uncovered - set(acs):
        issues.append(issue(ERROR, "unknown_ac", "draft.uncoveredAcceptanceCriteria", f"{ac!r} is not in the brief", "Remove it."))

    handled = [a["ambiguityId"] for a in draft["assumptions"]] + [q["ambiguityId"] for q in draft["openQuestions"] if "ambiguityId" in q]
    for amb in sorted(ambs):
        n = handled.count(amb)
        if n != 1:
            issues.append(issue(ERROR, "ambiguity_unresolved" if n == 0 else "ambiguity_duplicated", "draft.assumptions", f"{amb} handled {n} times", "Resolve each ambiguity exactly once: an assumption or an open question."))
    for amb in sorted(set(handled) - ambs):
        issues.append(issue(ERROR, "unknown_ambiguity", "draft.assumptions", f"{amb} is not in the brief", f"Use ids from context.brief.ambiguities: {sorted(ambs)}."))
    return issues
