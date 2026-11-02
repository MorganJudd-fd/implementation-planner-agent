import json
import os
import subprocess
import unittest

import helpers
from plannerlib.hooks import check_bash, check_write

ROOT = helpers.ROOT


def denied(result):
    return result is not None and result["hookSpecificOutput"]["permissionDecision"] == "deny"


class Bound(unittest.TestCase):
    """PROJ-1 and AB2-77 bound to /r (as prepare would) via a throwaway targets file."""

    def setUp(self):
        self.fx = helpers.Fixture().__enter__()
        self.addCleanup(self.fx.__exit__)
        self.fx.bind("PROJ-1", "/r")
        self.fx.bind("AB2-77", "/r")


class TestWrites(Bound):
    def test_allowed(self):
        for p in ("/r/.claude/plans/PROJ-1.draft.json", "/r/.claude/plans/AB2-77.input.json"):
            self.assertIsNone(check_write({"file_path": p}), p)

    def test_unbound_repo_or_key_denied(self):
        for p in ("/tmp/.claude/plans/PROJ-1.draft.json", "/other/.claude/plans/PROJ-1.draft.json",
                  "/r/.claude/plans/ZZ-9.draft.json"):
            self.assertTrue(denied(check_write({"file_path": p})), p)

    def test_denied(self):
        for p in ("/r/src/x.ts", "/r/.claude/plans/PROJ-1.plan.json", "/r/.claude/plans/PROJ-1.context.json",
                  "/r/.claude/plans/PROJ-1.attempts.json", "/r/.claude/plans/../../src/PROJ-1.draft.json",
                  "/r/.claude/plans/proj-1.draft.json", "/r/.claude/plans/x/PROJ-1.draft.json", "",
                  "/r/.claude/plans/PROJ-1.draft.json\n"):
            self.assertTrue(denied(check_write({"file_path": p})), p)

    def test_symlinked_plans_dir(self):
        with helpers.Fixture() as fx:
            os.makedirs(os.path.join(fx.repo, ".claude"), exist_ok=True)
            os.symlink(os.path.join(fx.repo, "src"), fx.plans)
            fx.bind("PROJ-1", fx.repo)
            self.assertTrue(denied(check_write({"file_path": os.path.join(fx.plans, "PROJ-1.draft.json")})))


class TestBash(Bound):
    def test_allowed(self):
        for c in ("python3 scripts/planner.py prepare PROJ-1 --repo /r",
                  f"python3 {ROOT}/scripts/planner.py finalize PROJ-1 --repo /r",
                  "git status", "git -C /r log --oneline -5", "git -C /r diff --stat", "git ls-files",
                  "git show HEAD:README.md", "git rev-parse --show-toplevel"):
            self.assertIsNone(check_bash({"command": c}, ROOT), c)

    def test_denied(self):
        for c in ("rm -rf /", "cat a > b", "ls | head", "ls\nrm x", "git status; rm x", "git status && rm x",
                  "echo $(id)", "echo `id`", "git diff --output=x", "git log --output x",
                  "git -c core.pager=sh log", "git grep -Ocat foo", "git push", "git commit -m x",
                  "git remote add x y", "git -C /r checkout .", "find . -fprint x", "ls",
                  "python3 other.py", "python3 -c 'import os'", "git diff --ext-diff", "curl https://x",
                  "python3 scripts/planner.py prepare X-1 &", "git status 2>/dev/null",
                  "git -C /etc status", "git -C /tmp/evil diff", "git -C /r/../tmp log",
                  "git diff --outp=x", "git diff --ext-dif", "git show --textcon"):
            self.assertTrue(denied(check_bash({"command": c}, ROOT)), c)

    def test_reason_present(self):
        r = check_bash({"command": "ls | head"}, ROOT)
        self.assertIn("Read, Glob and Grep", r["hookSpecificOutput"]["permissionDecisionReason"])


class TestWrappers(unittest.TestCase):
    """The .sh wrappers must exit 0 and emit deny JSON on stdout (exit 2 would drop the reason)."""

    def run_hook(self, name, payload):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=ROOT)
        return subprocess.run(["bash", os.path.join(ROOT, "hooks", name)], input=payload if isinstance(payload, str) else json.dumps(payload),
                              capture_output=True, text=True, env=env)

    def test_bash_deny(self):
        p = self.run_hook("guard-bash.sh", {"tool_name": "Bash", "cwd": ROOT, "tool_input": {"command": "rm -rf x"}})
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_bash_allow(self):
        p = self.run_hook("guard-bash.sh", {"tool_name": "Bash", "cwd": ROOT,
                                            "tool_input": {"command": "python3 scripts/planner.py probe"}})
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_write_deny(self):
        p = self.run_hook("guard-writes.sh", {"tool_name": "Write", "tool_input": {"file_path": "/r/src/a.ts"}})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_bad_input_denies(self):
        for raw in ("not json", "[]", '{"tool_input": "x"}', '{"tool_input": {"command": 5}}'):
            p = self.run_hook("guard-bash.sh", raw)
            self.assertEqual(p.returncode, 0, raw)
            self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny", raw)
        p = self.run_hook("guard-writes.sh", "[]")
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
