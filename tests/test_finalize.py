import copy
import json
import os
import subprocess
import unittest

import helpers
from plannerlib import finalize, render
from plannerlib.common import EXIT_ASK_USER, PlannerError, load_json, load_schema, validate

NO_ENGINE = {"use_engine": False}


def codes(result):
    return {e["code"] for e in result.get("errors", [])}


class TestFinalizeValid(unittest.TestCase):
    def test_plan_written(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            fx.write_draft()
            res = finalize.run(helpers.KEY, fx.repo, **NO_ENGINE)
            self.assertTrue(res["ok"], res)
            plan = load_json(res["planPath"])
            self.assertEqual(validate(plan, load_schema("plan.schema.json")), [])
            self.assertEqual(plan["waves"], [
                {"wave": 1, "parallel": True, "workstreams": ["ws1", "ws2"]},
                {"wave": 2, "parallel": False, "workstreams": ["ws3"]},
            ])
            self.assertTrue(plan["needsWorktreeIsolation"])
            self.assertEqual(plan["verification"]["acCoverage"], {"AC1": ["ws1", "ws3"], "AC2": ["ws2", "ws3"]})
            self.assertEqual(plan["decisionSource"], "fallback")
            self.assertEqual(plan["assumptions"][0]["ambiguity"], "TTL?")
            ws = {w["id"]: w for w in plan["workstreams"]}
            self.assertEqual(ws["ws3"]["specialty"], "test-writer")
            self.assertEqual(ws["ws1"]["specialty"], "general-purpose")  # .ts with no matching glob rule
            self.assertFalse(os.path.exists(os.path.join(fx.plans, f"{helpers.KEY}.attempts.json")))

    def test_specialty_hint_installed_agent(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            d = copy.deepcopy(helpers.DRAFT)
            d["workstreams"][0]["specialtyHint"] = "pricing-dev"
            fx.write_draft(d)
            plan = load_json(finalize.run(helpers.KEY, fx.repo, **NO_ENGINE)["planPath"])
            self.assertEqual(plan["workstreams"][0]["specialty"], "pricing-dev")
            self.assertEqual(plan["workstreams"][0]["specialtySource"], "installed-project")

    def test_security_floor(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            d = copy.deepcopy(helpers.DRAFT)
            d["workstreams"][1]["filesOwned"] = [{"path": "src/auth/session.ts", "action": "modify"}]
            d["workstreams"][1]["steps"] = ["Edit src/auth/session.ts"]
            fx.write_draft(d)
            plan = load_json(finalize.run(helpers.KEY, fx.repo, **NO_ENGINE)["planPath"])
            ws2 = plan["workstreams"][1]
            self.assertTrue(ws2["securitySensitive"])
            self.assertEqual(ws2["risk"]["level"], "high")
            self.assertTrue(any("security" in f["reason"] for f in plan["reviewFlags"]))

    def test_render(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            d = copy.deepcopy(helpers.DRAFT)
            d["summary"] = "Approach \x1b[31mred\x1b[0m"
            fx.write_draft(d)
            finalize.run(helpers.KEY, fx.repo, **NO_ENGINE)
            out = render.run(helpers.KEY, fx.repo)
            for section in ("Approach", "Grounding", "Workstreams", "Execution", "Verification",
                            "Assumptions", "Open questions", "Review flags", "Plan written to"):
                self.assertIn(section, out)
            self.assertNotIn("\x1b", out)
            self.assertIn("Wave 1 (parallel)  ws1 · ws2", out)
            self.assertIn("(complete)", out)


class TestFinalizeErrors(unittest.TestCase):
    def run_draft(self, fx, d=None, raw=None):
        fx.write_draft(d, raw=raw)
        return finalize.run(helpers.KEY, fx.repo, **NO_ENGINE)

    def mutate(self, fn):
        d = copy.deepcopy(helpers.DRAFT)
        fn(d)
        return d

    def test_cases(self):
        cases = {
            "malformed_json": dict(raw="{nope"),
            "schema": dict(d=self.mutate(lambda d: d["workstreams"][0].update(waves=[1]))),
            "ownership_overlap": dict(d=self.mutate(lambda d: d["workstreams"][2].update(
                filesOwned=[{"path": "src/pricing/repo.ts", "action": "modify"}]))),
            "dependency_cycle": dict(d=self.mutate(lambda d: d["workstreams"][0].update(dependsOn=["ws3"]))),
            "bad_dependency": dict(d=self.mutate(lambda d: d["workstreams"][0].update(dependsOn=["ws9"]))),
            "ac_unmapped": dict(d=self.mutate(lambda d: [w.update(acceptanceCriteria=["AC1"]) for w in d["workstreams"]])),
            "unknown_ac": dict(d=self.mutate(lambda d: d["workstreams"][0].update(acceptanceCriteria=["AC1", "AC9"]))),
            "command_not_allowlisted": dict(d=self.mutate(lambda d: d["workstreams"][0].update(verification=["npm run made-up"]))),
            "file_missing": dict(d=self.mutate(lambda d: d["workstreams"][1].update(
                filesOwned=[{"path": "src/nope.ts", "action": "modify"}]))),
            "create_exists": dict(d=self.mutate(lambda d: d["workstreams"][0].update(
                filesOwned=[{"path": "src/pricing/service.ts", "action": "create"}]))),
            "unsafe_path": dict(d=self.mutate(lambda d: d["workstreams"][0].update(
                filesOwned=[{"path": "../outside.ts", "action": "create"}]))),
            "read_missing": dict(d=self.mutate(lambda d: d["workstreams"][0].update(filesRead=["src/ghost.ts"]))),
            "evidence_missing": dict(d=self.mutate(lambda d: d.update(codebaseFindings=[{"finding": "x", "evidence": "src/ghost.ts:3"}]))),
            "ambiguity_unresolved": dict(d=self.mutate(lambda d: d.update(assumptions=[]))),
            "ambiguity_duplicated": dict(d=self.mutate(lambda d: d.update(openQuestions=[
                {"ambiguityId": "AMB1", "question": "q", "whyItMatters": "w"}]))),
            "no_verification": dict(d=self.mutate(lambda d: d["workstreams"][0].update(verification=[]))),
            "ticket_mismatch": dict(d=self.mutate(lambda d: d.update(ticketKey="OTHER-1"))),
        }
        for code, kwargs in cases.items():
            with self.subTest(code=code), helpers.Fixture() as fx:
                fx.prepare()
                res = self.run_draft(fx, **kwargs)
                self.assertFalse(res["ok"])
                self.assertIn(code, codes(res), res["errors"])
                for e in res["errors"]:
                    self.assertTrue(e["fix"])

    def test_uncovered_declared_is_ok(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            d = self.mutate(lambda d: [w.update(acceptanceCriteria=["AC1"]) for w in d["workstreams"]])
            d["uncoveredAcceptanceCriteria"] = [{"id": "AC2", "reason": "needs product decision"}]
            self.assertTrue(self.run_draft(fx, d)["ok"])

    def test_manual_verification_ok(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            d = self.mutate(lambda d: d["workstreams"][0].update(verification=[], manualVerification="eyeball it"))
            self.assertTrue(self.run_draft(fx, d)["ok"])

    def test_stale_context(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            with open(os.path.join(fx.repo, "README.md"), "a") as f:
                f.write("x\n")
            helpers.git(fx.repo, "commit", "-qam", "move head")
            self.assertIn("stale_context", codes(self.run_draft(fx)))

    def test_attempt_limit(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            for i in range(3):
                res = self.run_draft(fx, raw="{")
                self.assertEqual(res["attempt"], i + 1)
            with self.assertRaises(PlannerError) as cm:
                self.run_draft(fx, raw="{")
            self.assertEqual(cm.exception.code, EXIT_ASK_USER)
            fx.prepare()  # resets
            self.assertTrue(self.run_draft(fx)["ok"])

    def test_warnings_flagged_not_blocking(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            d = self.mutate(lambda d: d["workstreams"][1].update(steps=["Do the thing"]))
            res = self.run_draft(fx, d)
            self.assertTrue(res["ok"])
            plan = load_json(res["planPath"])
            self.assertTrue(any(f["source"] == "validation" for f in plan["reviewFlags"]))


class TestCli(unittest.TestCase):
    def test_end_to_end(self):
        cli = os.path.join(helpers.ROOT, "scripts", "planner.py")
        env = {k: v for k, v in os.environ.items() if k != "KEYSTONE_TOKEN"}
        with helpers.Fixture() as fx:
            fx.write_brief()
            run = lambda *a: subprocess.run(["python3", cli, *a, "--repo", fx.repo], capture_output=True, text=True, env=env)
            p = run("prepare", helpers.KEY)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(json.loads(p.stdout)["ambiguities"], ["AMB1"])
            fx.write_draft(raw="{")
            self.assertEqual(run("finalize", helpers.KEY).returncode, 1)
            fx.write_draft()
            f = run("finalize", helpers.KEY)
            self.assertEqual(f.returncode, 0, f.stdout)
            r = run("render", helpers.KEY)
            self.assertEqual(r.returncode, 0)
            self.assertIn("PLAN · PROJ-1234", r.stdout)
            self.assertEqual(run("finalize", "../bad").returncode, 1)


if __name__ == "__main__":
    unittest.main()
