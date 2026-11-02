"""`render`: human-readable plan summary for the review step. This module *is* the output format."""

import re

from .common import EXIT_INVALID, PlannerError, load_json, plan_file, require_key
from .target import bound_target

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]|\x1b\[[0-9;]*[A-Za-z]")


def _c(text):
    """Strip control chars / ANSI so ticket text cannot repaint the terminal."""
    return _CTRL.sub("", str(text))


def render(plan, plan_path):
    L = []
    L.append(f"PLAN · {plan['ticket']['key']} · {_c(plan['ticket']['summary'])}")
    L.append(f"decisions: {plan['decisionSource']}   policy {plan['policyVersion']}")
    L += ["", "Approach", f"  {_c(plan['summary'])}", "", "Grounding"]
    for f in plan["codebaseFindings"]:
        L.append(f"  {_c(f['evidence']):<40} {_c(f['finding'])}")

    L += ["", "Workstreams"]
    w_spec = max(len(w["specialty"]) for w in plan["workstreams"])
    for w in plan["workstreams"]:
        owns = ", ".join(f["path"] + ("" if f["action"] == "modify" else f" ({f['action']})") for f in w["filesOwned"])
        acs = " ".join(w["acceptanceCriteria"]) or "-"
        sec = "  [security]" if w["securitySensitive"] else ""
        L.append(f"  {w['id']:<4} {w['specialty']:<{w_spec}}  {_c(w['title'])}")
        L.append(f"       owns {_c(owns)}")
        L.append(f"       AC {acs}   risk {w['risk']['level']} ({_c(w['risk']['reason'])}){sec}")
        verify = "; ".join(f"`{_c(v)}`" for v in w["verification"]) or f"manual: {_c(w.get('manualVerification', ''))}"
        L.append(f"       verify {verify}")

    L += ["", "Execution"]
    by_id = {w["id"]: w for w in plan["workstreams"]}
    for wave in plan["waves"]:
        kind = "parallel" if wave["parallel"] else "serial"
        deps = sorted({d for wid in wave["workstreams"] for d in by_id[wid]["dependsOn"]})
        note = f"needs {', '.join(deps)}" if deps else "no dependencies"
        L.append(f"  Wave {wave['wave']} ({kind})  {' · '.join(wave['workstreams'])}   {note}")
    s = plan["speedup"]
    L.append(f"  ≈{s['waves']} wave(s) vs {s['serialSteps']} serial step(s)"
             + ("; worktree isolation required" if plan["needsWorktreeIsolation"] else ""))

    v = plan["verification"]
    L += ["", "Verification"]
    L.append("  full gate " + (" && ".join(f"`{_c(c)}`" for c in v["fullSuite"]) or "none (manual only)"))
    cov = "  ".join(f"{ac}→{','.join(ws) or '—'}" for ac, ws in v["acCoverage"].items())
    complete = all(v["acCoverage"].values())
    L.append(f"  AC coverage  {cov}   ({'complete' if complete else 'GAPS'})")
    for u in plan["uncoveredAcceptanceCriteria"]:
        L.append(f"  ! {u['id']} not covered: {_c(u['reason'])}")
    for m in v["manualChecks"]:
        L.append(f"  manual: {_c(m)}")
    L.append(f"  rollback: {_c(v['rollback'])}")
    if plan["docsToUpdate"]:
        L.append("  docs: " + ", ".join(_c(d) for d in plan["docsToUpdate"]))

    L += ["", "Assumptions"]
    L += [f"  {a['ambiguityId']} {_c(a['ambiguity'])} → {_c(a['decision'])}" for a in plan["assumptions"]] or ["  none"]
    L += ["", "Open questions"]
    L += [f"  {q.get('ambiguityId', '?')} {_c(q['question'])} — {_c(q['whyItMatters'])}" for q in plan["openQuestions"]] or ["  none"]
    L += ["", "Review flags"]
    L += [f"  [{f['source']}] {_c(f['target'])}: {_c(f['reason'])}" for f in plan["reviewFlags"]] or ["  none"]
    if plan["warnings"]:
        L += ["", "Warnings"] + [f"  ! {_c(w)}" for w in plan["warnings"]]
    L += ["", f"Plan written to {plan_path}"]
    return "\n".join(L)


def run(key, repo=None):
    top = bound_target(require_key(key), repo)
    path = plan_file(top, key, "plan")
    try:
        plan = load_json(path)
    except FileNotFoundError:
        raise PlannerError(f"no plan for {key}; run finalize first", EXIT_INVALID)
    return render(plan, path)
