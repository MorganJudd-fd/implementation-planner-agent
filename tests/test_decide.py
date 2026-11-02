import copy
import os
import tempfile
import unittest

import helpers
from plannerlib import decide
from plannerlib.common import EXIT_ENV, PlannerError, load_policy

POLICY = load_policy()
ENV = {"KEYSTONE_TOKEN": "test-token"}


def context(agents=()):
    return {
        "brief": {"ticket": {"summary": "s"}, "acceptanceCriteria": [{"id": "AC1", "assertion": "a"}]},
        "agents": [{"name": n, "description": "d", "scope": "installed-project"} for n in agents],
        "highChurnFiles": ["src/index.ts"],
    }


WSS = copy.deepcopy(helpers.DRAFT["workstreams"])


def answers(spec="backend-architect", spec_band="act", risk=0.2, risk_band="act", p_yes=0.1, sec_band="act", ids=("ws1", "ws2", "ws3")):
    a, b = {}, {}
    for i in ids:
        a[f"{i}__specialty"] = {"type": "choice", "choice": spec, "confidence": 0.9}
        a[f"{i}__risk"] = {"type": "score", "score": risk, "confidence": 0.9}
        a[f"{i}__security"] = {"type": "yes_no", "p_yes": p_yes}
        b.update({f"{i}__specialty": spec_band, f"{i}__risk": risk_band, f"{i}__security": sec_band})
    return {"id": "dec_1", "answers": a, "suggested_bands": b}


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, payload, timeout):
        self.calls.append((method, url, headers, payload))
        return self.responses.pop(0)


class TestDecide(unittest.TestCase):
    def run_decide(self, responses, wss=WSS, ctx=None, **kw):
        t = FakeTransport(responses)
        res = decide.decide(wss, ctx or context(), POLICY, transport=t, sleep=lambda s: None, env=ENV, **kw)
        return res, t

    def test_act(self):
        res, t = self.run_decide([(200, {}, answers())])
        self.assertEqual(res["decisionSource"], "keystone")
        self.assertEqual(res["results"]["ws1"]["specialty"], "backend-architect")
        self.assertEqual(res["results"]["ws1"]["risk"]["level"], "low")
        self.assertFalse(any(f["target"] == "ws1" for f in res["reviewFlags"]))
        payload = t.calls[0][3]
        self.assertEqual(payload["model"], "keystone-decide-fast")
        self.assertIn("ws1", payload["questions"]["ws1__risk"]["instructions"])
        self.assertIn("undetermined", payload["questions"]["ws1__specialty"]["criteria"])
        self.assertEqual(t.calls[0][2]["Authorization"], "Bearer test-token")

    def test_review_flags(self):
        res, _ = self.run_decide([(200, {}, answers(spec_band="review"))])
        self.assertTrue(any(f["target"] == "ws1" and "specialty" in f["reason"] for f in res["reviewFlags"]))

    def test_escalate_uses_rules(self):
        res, _ = self.run_decide([(200, {}, answers(spec_band="escalate"))])
        self.assertEqual(res["results"]["ws3"]["specialty"], "test-writer")

    def test_undetermined(self):
        res, _ = self.run_decide([(200, {}, answers(spec="undetermined"))])
        self.assertEqual(res["results"]["ws3"]["specialty"], "test-writer")
        self.assertTrue(any("could not choose" in f["reason"] for f in res["reviewFlags"]))

    def test_risk_rounding_and_floor(self):
        res, _ = self.run_decide([(200, {}, answers(risk=1.6))])
        self.assertEqual(res["results"]["ws1"]["risk"]["level"], "high")
        wss = copy.deepcopy(WSS)
        wss[0]["filesOwned"] = [{"path": "src/index.ts", "action": "modify"}]
        res, _ = self.run_decide([(200, {}, answers(risk=0.0))], wss=wss)
        self.assertEqual(res["results"]["ws1"]["risk"]["level"], "medium")

    def test_security_yes(self):
        res, _ = self.run_decide([(200, {}, answers(p_yes=0.95))])
        self.assertTrue(res["results"]["ws1"]["securitySensitive"])
        self.assertEqual(res["results"]["ws1"]["risk"]["level"], "high")

    def test_installed_agent_option(self):
        res, t = self.run_decide([(200, {}, answers(spec="pricing-dev"))], ctx=context(["pricing-dev"]))
        self.assertIn("pricing-dev", t.calls[0][3]["questions"]["ws1__specialty"]["criteria"])
        self.assertEqual(res["results"]["ws1"]["specialtySource"], "installed-project")

    def test_retry_then_success(self):
        res, t = self.run_decide([(502, {}, {}), (200, {}, answers())])
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(res["decisionSource"], "keystone")

    def test_429_retry_after(self):
        slept = []
        t = FakeTransport([(429, {"Retry-After": "2"}, {}), (200, {}, answers())])
        decide.decide(WSS, context(), POLICY, transport=t, sleep=slept.append, env=ENV)
        self.assertEqual(slept, [2.0])

    def test_404_falls_back(self):
        res, _ = self.run_decide([(404, {}, {"error": {"code": "not_found"}})])
        self.assertEqual(res["decisionSource"], "fallback")
        self.assertTrue(any("not_found" in w for w in res["warnings"]))

    def test_shield_header_warns(self):
        res, _ = self.run_decide([(200, {"x-keystone-shield-detection": "pii"}, answers())])
        self.assertTrue(any("Shield" in w for w in res["warnings"]))

    def test_mixed_batches(self):
        wss = []
        for i in range(1, 13):  # 12 ws × 3 questions → 2 batches of ≤32
            w = copy.deepcopy(WSS[1])
            w["id"] = f"ws{i}"
            wss.append(w)
        ids1 = [f"ws{i}" for i in range(1, 11)]
        res, t = self.run_decide([(200, {}, answers(ids=ids1)), (500, {}, {}), (500, {}, {})], wss=wss)
        self.assertEqual(len(t.calls[0][3]["questions"]), 30)
        self.assertEqual(res["decisionSource"], "mixed")

    def test_cache(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "c.json")
            self.run_decide([(200, {}, answers())], cache_path=cache)
            res, t = self.run_decide([], cache_path=cache)
            self.assertEqual(t.calls, [])
            self.assertEqual(res["decisionSource"], "keystone")

    def test_no_token(self):
        res = decide.decide(WSS, context(), POLICY, transport=FakeTransport([]), env={})
        self.assertEqual(res["decisionSource"], "fallback")
        self.assertTrue(any("KEYSTONE_TOKEN" in w for w in res["warnings"]))

    def test_https_required(self):
        with self.assertRaises(PlannerError) as cm:
            decide.engine_config(POLICY, {"KEYSTONE_URL": "http://evil", "KEYSTONE_TOKEN": "t"})
        self.assertEqual(cm.exception.code, EXIT_ENV)

    def test_source_counts_risk_and_security_answers(self):
        body = answers()
        for i in ("ws1", "ws2", "ws3"):
            del body["answers"][f"{i}__specialty"]
        res, _ = self.run_decide([(200, {}, body)])
        self.assertEqual(res["decisionSource"], "mixed")
        self.assertFalse(any(f["target"] == "plan" for f in res["reviewFlags"]))

    def test_cache_pruned_to_current_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cache.json")
            with open(path, "w") as f:
                f.write('{"stale-digest": {}}')
            self.run_decide([(200, {}, answers())], cache_path=path)
            with open(path) as f:
                self.assertNotIn("stale-digest", f.read())

    def test_probe(self):
        t = FakeTransport([(200, {}, {"data": [{"id": "keystone-decide-fast", "type": "decision"}]})])
        self.assertTrue(decide.probe(POLICY, t, ENV)["enabled"])
        t = FakeTransport([(200, {}, {"data": []})])
        self.assertFalse(decide.probe(POLICY, t, ENV)["enabled"])
        self.assertFalse(decide.probe(POLICY, FakeTransport([]), {})["enabled"])

    def test_token_not_in_output(self):
        res, _ = self.run_decide([(401, {}, {})])
        self.assertNotIn("test-token", repr(res))


if __name__ == "__main__":
    unittest.main()
