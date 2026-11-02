import os
import unittest

import helpers
from plannerlib import prepare
from plannerlib.common import EXIT_ASK_USER, EXIT_INVALID, PlannerError


class TestPrepare(unittest.TestCase):
    def test_intake_state_file_accepted(self):
        with helpers.Fixture() as fx:
            ctx = fx.prepare()
            self.assertEqual(ctx["ticketKey"], helpers.KEY)
            self.assertEqual([a["id"] for a in ctx["brief"]["acceptanceCriteria"]], ["AC1", "AC2"])
            self.assertEqual(ctx["brief"]["ambiguities"][0]["id"], "AMB1")
            self.assertTrue(os.path.exists(os.path.join(fx.plans, f"{helpers.KEY}.context.json")))

    def test_test_commands(self):
        with helpers.Fixture() as fx:
            cmds = {c["command"]: c["source"] for c in fx.prepare()["testCommands"]}
            for expected in ("pnpm run test", "pnpm test", "pnpm run lint", "make build", "make test-int",
                             "pnpm install --frozen-lockfile", "pnpm run typecheck", "pnpm run e2e", "echo done"):
                self.assertIn(expected, cmds)
            self.assertTrue(cmds["pnpm run typecheck"].startswith(".github/workflows/ci.yml:"))
            self.assertNotIn("|", cmds)

    def test_agents_project_beats_user(self):
        with helpers.Fixture() as fx:
            with open(os.path.join(fx.user_agents, "pricing-dev.md"), "w") as f:
                f.write("---\nname: pricing-dev\ndescription: user copy\n---\n")
            with open(os.path.join(fx.user_agents, "other.md"), "w") as f:
                f.write("---\nname: other\ndescription: generic\n---\n")
            agents = {a["name"]: a for a in fx.prepare()["agents"]}
            self.assertEqual(agents["pricing-dev"]["scope"], "installed-project")
            self.assertEqual(agents["other"]["scope"], "installed-user")

    def test_high_churn(self):
        with helpers.Fixture() as fx:
            churn = fx.prepare()["highChurnFiles"]
            self.assertIn("pnpm-lock.yaml", churn)
            self.assertIn("src/index.ts", churn)

    def test_exclude_not_gitignore(self):
        with helpers.Fixture() as fx:
            ctx = fx.prepare()
            self.assertTrue(ctx["excludeAdded"])
            self.assertFalse(ctx["repository"]["dirty"])
            with open(os.path.join(fx.repo, ".git", "info", "exclude")) as f:
                exclude = f.read()
            self.assertIn("/.claude/plans/", exclude)
            self.assertFalse(os.path.exists(os.path.join(fx.repo, ".gitignore")))
            self.assertFalse(prepare.run(helpers.KEY, fx.repo, fx.user_agents)["excludeAdded"])  # idempotent
            status = helpers.git(fx.repo, "status", "--porcelain")
            self.assertNotIn(".claude/plans", status)

    def test_input_json_fallback(self):
        with helpers.Fixture() as fx:
            fx.write_brief(where="plans")
            self.assertEqual(prepare.run(helpers.KEY, fx.repo, fx.user_agents)["ticketKey"], helpers.KEY)

    def test_missing_brief_asks_user(self):
        with helpers.Fixture() as fx:
            with self.assertRaises(PlannerError) as cm:
                prepare.run(helpers.KEY, fx.repo, fx.user_agents)
            self.assertEqual(cm.exception.code, EXIT_ASK_USER)

    def test_invalid_brief(self):
        with helpers.Fixture() as fx:
            bad = dict(helpers.BRIEF, acceptanceCriteria=[])
            fx.write_brief(bad)
            with self.assertRaises(PlannerError) as cm:
                prepare.run(helpers.KEY, fx.repo, fx.user_agents)
            self.assertEqual(cm.exception.code, EXIT_INVALID)

    def test_proposed_ac_only_ok(self):
        with helpers.Fixture() as fx:
            b = {k: v for k, v in helpers.BRIEF.items() if k != "acceptanceCriteria"}
            b["proposedAcceptanceCriteria"] = [{"id": "PAC1", "assertion": "x"}]
            fx.write_brief(b)
            ac = prepare.run(helpers.KEY, fx.repo, fx.user_agents)["brief"]["acceptanceCriteria"]
            self.assertEqual(ac, [{"id": "PAC1", "assertion": "x", "proposed": True}])

    def test_key_mismatch(self):
        with helpers.Fixture() as fx:
            fx.write_brief(dict(helpers.BRIEF, ticket={"key": "OTHER-1", "summary": "s"}))
            with self.assertRaises(PlannerError):
                prepare.run(helpers.KEY, fx.repo, fx.user_agents)

    def test_not_a_repo(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(PlannerError):
                prepare.run(helpers.KEY, d)


if __name__ == "__main__":
    unittest.main()
