"""`finalize`: validate the LLM draft, decide judgment calls, compute waves, write <KEY>.plan.json."""

import datetime

from . import decide as decide_mod
from .common import (
    EXIT_ASK_USER, EXIT_ENV, EXIT_INVALID, PLANNER_NAME, PLANNER_VERSION, PlannerError, git, load_json,
    load_json_or, load_policy, load_schema, normalize_rel, plan_file, require_key, unlink_quiet, validate,
    write_json_atomic,
)
from .target import bound_target
from .validate import ERROR, WARNING, compute_waves, issue, validate_draft


def run(key, repo=None, use_engine=True, transport=None, env=None):
    top = bound_target(require_key(key), repo)
    try:
        context = load_json(plan_file(top, key, "context"))
    except FileNotFoundError:
        raise PlannerError(f"no context for {key} in {top}; run `planner.py prepare {key}` first", EXIT_ENV)
    policy = load_policy()
    limits = policy["limits"]

    attempts_path = plan_file(top, key, "attempts")
    used = load_json_or(attempts_path, {}).get("count", 0)
    if used >= limits["maxFinalizeAttempts"]:
        raise PlannerError(
            f"finalize failed {used} times for {key}. Stop and show the user the remaining errors; "
            f"rerun `prepare` to reset the counter after they respond.", EXIT_ASK_USER)

    draft_path = plan_file(top, key, "draft")
    try:
        draft = load_json(draft_path)
    except FileNotFoundError:
        raise PlannerError(f"draft not found: {draft_path}", EXIT_INVALID)
    except ValueError as e:
        draft = None
        issues = [issue(ERROR, "malformed_json", "draft", f"draft is not valid JSON: {e}", "Rewrite the draft as valid JSON.")]
    if draft is not None:
        issues = validate_draft(draft, context, git(top, "rev-parse", "HEAD", check=False), limits)

    errors = [i for i in issues if i["severity"] == ERROR]
    warnings = [i for i in issues if i["severity"] != ERROR]
    if errors:
        n = used + 1
        write_json_atomic(attempts_path, {"count": n})
        return {"ok": False, "attempt": n, "maxAttempts": limits["maxFinalizeAttempts"], "errors": errors, "warnings": warnings}

    wss = draft["workstreams"]
    waves = compute_waves(wss)
    wave_of = {wid: w["wave"] for w in waves for wid in w["workstreams"]}
    dec = decide_mod.decide(wss, context, policy, use_engine=use_engine,
                            cache_path=plan_file(top, key, "decisions-cache"), transport=transport, env=env)

    churn = set(context["highChurnFiles"])
    last_wave = len(waves)
    for w in (wss if last_wave > 1 else []):
        if wave_of[w["id"]] == last_wave:
            continue
        for f in w["filesOwned"]:
            rel = normalize_rel(f["path"])
            if rel in churn:
                warnings.append(issue(WARNING, "high_churn_early", w["id"], f"{rel} is high-churn but owned outside the final wave", "Consider moving shared-file edits last."))

    flags = dec["reviewFlags"] + [decide_mod.make_flag(w["path"], w["message"], "validation") for w in warnings]
    acs = context["brief"]["acceptanceCriteria"]
    amb_text = {a["id"]: a["question"] for a in context["brief"]["ambiguities"]}
    brief_ticket = context["brief"]["ticket"]
    workstreams = []
    for w in wss:
        r = dec["results"][w["id"]]
        out = {
            "id": w["id"], "title": w["title"], "specialty": r["specialty"], "specialtySource": r["specialtySource"],
            "objective": w["objective"],
            "filesOwned": [{"path": normalize_rel(f["path"]), "action": f["action"]} for f in w["filesOwned"]],
            "filesRead": [normalize_rel(p) for p in w["filesRead"]],
            "steps": w["steps"], "verification": w["verification"],
            "acceptanceCriteria": w["acceptanceCriteria"], "dependsOn": w["dependsOn"],
            "risk": r["risk"], "securitySensitive": r["securitySensitive"], "wave": wave_of[w["id"]],
        }
        for opt in ("specialtyHint", "manualVerification"):
            if w.get(opt):
                out[opt] = w[opt]
        workstreams.append(out)

    plan = {
        "schemaVersion": "1.0",
        "planner": {"name": PLANNER_NAME, "version": PLANNER_VERSION},
        "policyVersion": policy["version"],
        "decisionsVersion": policy["decisions"]["version"],
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "ticket": {"key": key, "summary": brief_ticket["summary"], "url": brief_ticket.get("url")},
        "repository": {k: context["repository"][k] for k in ("toplevel", "resolvedVia", "originUrl", "gitHead", "dirty")},
        "summary": draft["summary"],
        "codebaseFindings": draft["codebaseFindings"],
        "workstreams": workstreams,
        "waves": waves,
        "needsWorktreeIsolation": any(w["parallel"] for w in waves),
        "speedup": {"serialSteps": len(wss), "waves": len(waves)},
        "verification": {
            "fullSuite": draft["verification"]["fullSuite"],
            "acCoverage": {a["id"]: [w["id"] for w in wss if a["id"] in w["acceptanceCriteria"]] for a in acs},
            "manualChecks": draft["verification"]["manualChecks"],
            "rollback": draft["verification"]["rollback"],
        },
        "docsToUpdate": draft.get("docsToUpdate", []),
        "assumptions": [{"ambiguityId": a["ambiguityId"], "ambiguity": amb_text[a["ambiguityId"]], "decision": a["decision"]}
                        for a in draft["assumptions"]],
        "openQuestions": draft["openQuestions"],
        "uncoveredAcceptanceCriteria": draft.get("uncoveredAcceptanceCriteria", []),
        "reviewFlags": flags,
        "decisionSource": dec["decisionSource"],
        "decisions": dec["decisions"],
        "warnings": dec["warnings"] + [f"{w['code']}: {w['message']}" for w in warnings],
    }
    if context["repository"]["dirty"]:
        plan["warnings"].append("working copy was dirty at prepare time")
    self_check = validate(plan, load_schema("plan.schema.json"), "plan")
    if self_check:
        raise PlannerError("internal error: generated plan violates plan.schema.json:\n  " + "\n  ".join(self_check))
    out_path = plan_file(top, key, "plan")
    write_json_atomic(out_path, plan)
    unlink_quiet(attempts_path)
    return {"ok": True, "planPath": out_path, "warnings": warnings, "decisionSource": plan["decisionSource"]}
