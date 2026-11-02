"""Judgment calls: specialty, risk, security sensitivity.

Keystone Decision Engine (POST /v1/systemone) answers when KEYSTONE_TOKEN is set; deterministic
rules from policy.json answer otherwise, and per batch when the engine fails. The token is read
from the environment only and is never logged or written.
"""

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

from .common import EXIT_ENV, PlannerError, glob_match, load_json_or, normalize_rel, write_json_atomic

RETRYABLE = {500, 502, 504}


# ---------------------------------------------------------------- transport


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never forward the bearer token elsewhere
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_transport(method, url, headers, payload, timeout):
    """Return (status, headers, body). Network errors return status 0."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        return e.code, dict(e.headers or {}), body
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return 0, {}, {"error": {"code": "network_error", "message": str(e)}}


def _err_msg(status, body):
    err = (body.get("error") or {}) if isinstance(body, dict) else {}
    return f"HTTP {status} {err.get('code') or ''}".strip()


def engine_config(policy, env=None):
    """Return (url, token); raise for an unsafe URL. Token None → engine disabled."""
    env = os.environ if env is None else env
    url = env.get("KEYSTONE_URL", policy["decisions"]["defaultUrl"]).rstrip("/")
    if not url.startswith("https://"):
        raise PlannerError("KEYSTONE_URL must use https://", EXIT_ENV)
    return url, env.get("KEYSTONE_TOKEN") or None


def probe(policy, transport=http_transport, env=None):
    url, token = engine_config(policy, env)
    if not token:
        return {"enabled": False, "reason": "KEYSTONE_TOKEN is not set"}
    status, _, body = transport("GET", f"{url}/v1/models", {"Authorization": f"Bearer {token}"}, None, 15)
    if status != 200:
        return {"enabled": False, "reason": _err_msg(status, body)}
    profiles = [m.get("id") for m in body.get("data", []) if m.get("type") == "decision"]
    return {"enabled": bool(profiles), "url": url, "profiles": profiles,
            "reason": None if profiles else "Decisions API not enabled for this account"}


# ---------------------------------------------------------------- rules (fallback + floors)


def _owned(ws):
    return [normalize_rel(f["path"]) for f in ws["filesOwned"]]


def valid_specialties(context, policy):
    """name -> (scope, description). Installed agents win over baseline names."""
    out = {n: ("baseline", d) for n, d in policy["specialties"]["baseline"].items()}
    for a in context["agents"]:
        out[a["name"]] = (a["scope"], a["description"])
    return out


def rule_specialty(ws, policy):
    best, best_n = policy["specialties"]["fallback"], 0
    owned = _owned(ws)
    for rule in policy["specialties"]["globRules"]:
        n = sum(1 for p in owned if glob_match(p, rule["globs"]))
        if n > best_n:
            best, best_n = rule["specialty"], n
    return best


def rule_security(ws, policy):
    path_rx = re.compile(policy["patterns"]["security"], re.I)
    text_rx = re.compile(policy["patterns"]["securityText"], re.I)
    text = " ".join([ws["title"], ws["objective"], *ws["steps"]])
    return any(path_rx.search(p) for p in _owned(ws)) or bool(text_rx.search(text))


def risk_floor(ws, context, policy, security):
    owned = _owned(ws)
    churn = set(context["highChurnFiles"])
    reasons, level = [], 0
    if security:
        level, reasons = len(policy["decisions"]["riskLevels"]) - 1, ["security-sensitive surface"]
    mig = re.compile(policy["patterns"]["migration"], re.I)
    if any(mig.search(p) for p in owned):
        level = max(level, 1)
        reasons.append("owns a migration")
    if any(p in churn for p in owned):
        level = max(level, 1)
        reasons.append("owns a high-churn shared file")
    if any(f["action"] == "delete" for f in ws["filesOwned"]):
        level = max(level, 1)
        reasons.append("deletes files")
    return level, reasons


# ---------------------------------------------------------------- engine payload


def _state(context, workstreams, limit):
    brief = context["brief"]
    state = {
        "ticket": brief["ticket"]["summary"],
        "acceptanceCriteria": [f"{a['id']}: {a['assertion']}" for a in brief["acceptanceCriteria"]],
        "workstreams": [
            {"id": w["id"], "title": w["title"], "objective": w["objective"], "files": _owned(w),
             "reads": w["filesRead"], "steps": w["steps"]}
            for w in workstreams
        ],
    }
    text = json.dumps(state, ensure_ascii=False)
    if len(text) > limit:  # drop step detail first, then reads
        for w in state["workstreams"]:
            w["steps"] = [s[:160] for s in w["steps"][:5]]
            w.pop("reads")
        text = json.dumps(state, ensure_ascii=False)
    return text[:limit]


def _questions(ws, policy, specialties):
    cfg = policy["decisions"]
    q = cfg["questions"]
    fmt = {"id": ws["id"], "title": ws["title"]}
    options = {n: (d or None) for n, (_, d) in specialties.items()}
    options["undetermined"] = "The description does not support a clear choice"
    return {
        f"{ws['id']}__specialty": {"type": "choice", "instructions": q["specialty"].format(**fmt), "criteria": options},
        f"{ws['id']}__risk": {"type": "score", "instructions": q["risk"].format(**fmt), "criteria": cfg["riskLevels"]},
        f"{ws['id']}__security": {"type": "yes_no", "instructions": q["security"].format(**fmt), "criteria": cfg["securityCriteria"]},
    }


def _call(url, token, payload, cfg, transport, sleep):
    """Return (body, error_message, shield_header)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    retried = False
    while True:
        status, resp_headers, body = transport("POST", f"{url}/v1/systemone", headers, payload, cfg["timeoutSeconds"])
        h = {k.lower(): v for k, v in resp_headers.items()}
        shield = h.get("x-keystone-shield-detection")
        if status == 200:
            return body, None, shield
        msg = _err_msg(status, body)
        if retried:
            return None, msg, shield
        if status in RETRYABLE or status == 0:
            retried = True
            continue
        if status == 429:
            wait = h.get("retry-after", "1")
            try:
                wait = float(wait)
            except ValueError:
                wait = 1.0
            if wait <= cfg["maxRetryAfterSeconds"]:
                sleep(wait)
                retried = True
                continue
        return None, msg, shield


# ---------------------------------------------------------------- main entry


def decide(workstreams, context, policy, use_engine=True, cache_path=None, transport=None,
           sleep=time.sleep, env=None):
    cfg = policy["decisions"]
    transport = transport or http_transport
    specialties = valid_specialties(context, policy)
    warnings, flags, decisions = [], [], []
    results, engine_answers = {}, {}

    url, token = engine_config(policy, env) if use_engine else (None, None)
    if use_engine and not token:
        warnings.append("Keystone disabled: KEYSTONE_TOKEN not set; rule-based decisions used.")

    if token:
        cache = load_json_or(cache_path, {}) if cache_path else {}
        used = {}  # only digests for the current state stay cached; older ones can never hit again
        state = _state(context, workstreams, cfg["maxStateChars"])
        per_call = max(1, cfg["maxQuestionsPerCall"] // 3)
        for start in range(0, len(workstreams), per_call):
            batch = workstreams[start:start + per_call]
            questions = {}
            for w in batch:
                questions.update(_questions(w, policy, specialties))
            payload = {"model": cfg["profile"], "state": state, "questions": questions}
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            body = cache.get(digest)
            if body is not None:
                used[digest] = body
            else:
                body, err, shield = _call(url, token, payload, cfg, transport, sleep)
                if shield:
                    warnings.append(f"Keystone Shield detection: {shield}")
                if err:
                    warnings.append(f"Keystone batch {[w['id'] for w in batch]} failed ({err}); rule-based decisions used.")
                    continue
                used[digest] = body
            for qid, ans in body.get("answers", {}).items():
                engine_answers[qid] = (ans, body.get("suggested_bands", {}).get(qid), body.get("id"))
        if cache_path and used != cache:
            write_json_atomic(cache_path, used)

    levels = cfg["riskLevels"]
    for w in workstreams:
        wid = w["id"]
        hint = w.get("specialtyHint")
        rule_spec = hint if hint in specialties else rule_specialty(w, policy)
        rule_sec = rule_security(w, policy)

        # specialty
        got = engine_answers.get(f"{wid}__specialty")
        if got and got[0].get("choice") in specialties:
            ans, band, did = got
            choice = ans["choice"] if band != "escalate" else rule_spec
            decisions.append(_dec(f"{wid}__specialty", "keystone", did, ans["choice"], ans.get("confidence"), band))
            if band != "act":
                flags.append(make_flag(wid, f"specialty '{choice}' ({band}); confirm the owning agent", "keystone"))
        else:
            choice = rule_spec
            if got:  # answered 'undetermined'
                decisions.append(_dec(f"{wid}__specialty", "keystone", got[2], got[0].get("choice"), got[0].get("confidence"), got[1]))
                flags.append(make_flag(wid, f"engine could not choose a specialty; using '{choice}'", "keystone"))
            else:
                decisions.append(_dec(f"{wid}__specialty", "fallback", None, choice, None, "review"))

        # security
        got = engine_answers.get(f"{wid}__security")
        if got:
            ans, band, did = got
            p_yes = ans.get("p_yes", 0.0)
            engine_sec = band in ("act", "review") and p_yes > 0.5
            decisions.append(_dec(f"{wid}__security", "keystone", did, round(p_yes, 3), None, band))
            if band == "escalate":
                flags.append(make_flag(wid, "unclear whether this workstream is security-sensitive", "keystone"))
            security = engine_sec or rule_sec
        else:
            security = rule_sec
            decisions.append(_dec(f"{wid}__security", "fallback", None, security, None, "review"))

        # risk = max(engine, deterministic floor)
        floor, reasons = risk_floor(w, context, policy, security)
        got = engine_answers.get(f"{wid}__risk")
        level = floor
        if got:
            ans, band, did = got
            eng = min(len(levels) - 1, max(0, int(round(float(ans.get("score", 0))))))
            decisions.append(_dec(f"{wid}__risk", "keystone", did, levels[eng], ans.get("confidence"), band))
            if eng > floor:
                reasons.insert(0, f"engine assessed {levels[eng]}")
            level = max(floor, eng)
            if band == "escalate":
                flags.append(make_flag(wid, "engine was unsure about risk; confirm", "keystone"))
        else:
            decisions.append(_dec(f"{wid}__risk", "fallback", None, levels[floor], None, "review"))
        note = w.get("riskNote", "").strip()
        reason = "; ".join(reasons + ([note] if note else [])) or "no risk factors detected"

        scope = specialties[choice][0] if choice in specialties else "baseline"
        results[wid] = {"specialty": choice, "specialtySource": scope, "securitySensitive": security,
                        "risk": {"level": levels[level], "reason": reason}}

    sources = {d["source"] for d in decisions}
    engine_used = "keystone" in sources
    if any(r["securitySensitive"] for r in results.values()) and \
            not any("security" in r["specialty"] for r in results.values()):
        sec_ids = [i for i, r in results.items() if r["securitySensitive"]]
        flags.append(make_flag(",".join(sec_ids), "security-sensitive work with no security specialist; consider a security review workstream", "keystone" if engine_used else "fallback"))
    if not engine_used:
        flags.append(make_flag("plan", "specialty, risk and security came from rules, not the decision engine; confirm them", "fallback"))
    source = "mixed" if len(sources) > 1 else ("keystone" if engine_used else "fallback")
    return {"results": results, "decisions": decisions, "reviewFlags": flags, "decisionSource": source, "warnings": warnings}


def _dec(qid, source, did, answer, confidence, band):
    return {"questionId": qid, "source": source, "decisionId": did, "answer": answer,
            "confidence": confidence, "band": band}


def make_flag(target, reason, source):
    return {"target": target, "reason": reason, "source": source}
