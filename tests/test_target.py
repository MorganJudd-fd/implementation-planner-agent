import json
import os
import unittest

import helpers
from plannerlib import finalize, prepare, render
from plannerlib.common import EXIT_ASK_USER, EXIT_INVALID, PlannerError, load_targets
from plannerlib.target import find_intake_states, find_repos_named, resolve_for_prepare, resolve_repo

POLICY = {"repoSearch": {"roots": [], "maxDepth": 2}}


class TestResolve(unittest.TestCase):
    def test_by_folder_name(self):
        with helpers.Fixture() as fx:
            self.assertEqual(find_repos_named("repo", [fx.tmp], 2), [fx.repo])
            self.assertEqual(resolve_repo("repo", [fx.tmp], 2), (fx.repo, "name"))

    def test_unknown_or_ambiguous_name_asks_user(self):
        with helpers.Fixture() as fx:
            os.makedirs(os.path.join(fx.tmp, "group", "repo", ".git"))
            for name in ("repo", "nope"):
                with self.assertRaises(PlannerError) as cm:
                    resolve_repo(name, [fx.tmp], 2)
                self.assertEqual(cm.exception.code, EXIT_ASK_USER, name)

    def test_intake_state_found(self):
        with helpers.Fixture() as fx:
            fx.write_brief()
            self.assertEqual(find_intake_states(helpers.KEY, [fx.tmp], 2), [fx.repo])
            policy = {"repoSearch": {"roots": [fx.tmp], "maxDepth": 2}}
            self.assertEqual(resolve_for_prepare(helpers.KEY, None, policy=policy), (fx.repo, "intake-state"))

    def test_nothing_to_go_on_asks_user(self):
        with helpers.Fixture():
            with self.assertRaises(PlannerError) as cm:
                resolve_for_prepare(helpers.KEY, None, policy=POLICY)
            self.assertEqual(cm.exception.code, EXIT_ASK_USER)
            self.assertIn("which repository", str(cm.exception))

    def test_brief_file_repository_hint(self):
        with helpers.Fixture() as fx:
            path = os.path.join(fx.tmp, "brief.json")
            with open(path, "w") as f:
                json.dump(dict(helpers.BRIEF, repository={"toplevel": fx.repo}), f)
            ctx = prepare.run(path, None, fx.user_agents)
            self.assertEqual((ctx["repository"]["toplevel"], ctx["repository"]["resolvedVia"]), (fx.repo, "brief"))


class TestBinding(unittest.TestCase):
    def test_prepare_binds_and_later_commands_use_it(self):
        with helpers.Fixture() as fx:
            fx.prepare()
            self.assertEqual(load_targets()[helpers.KEY], {"toplevel": fx.repo, "resolvedVia": "path"})
            fx.write_draft()
            self.assertTrue(finalize.run(helpers.KEY, use_engine=False)["ok"])
            self.assertIn("PLAN · PROJ-1234", render.run(helpers.KEY))

    def test_missing_brief_still_binds(self):
        with helpers.Fixture() as fx:
            with self.assertRaises(PlannerError):
                prepare.run(helpers.KEY, fx.repo, fx.user_agents)
            self.assertEqual(load_targets()[helpers.KEY]["toplevel"], fx.repo)

    def test_unbound_or_mismatched(self):
        with helpers.Fixture() as fx, helpers.Fixture() as other:
            with self.assertRaises(PlannerError) as cm:
                finalize.run("ZZ-1", use_engine=False)
            self.assertEqual(cm.exception.code, EXIT_INVALID)
            fx.bind(helpers.KEY, fx.repo)
            with self.assertRaises(PlannerError) as cm:
                render.run(helpers.KEY, other.repo)
            self.assertIn("bound to", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
