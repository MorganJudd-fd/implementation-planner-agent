"""Test helpers: build a throwaway git repo fixture and a valid draft."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

KEY = "PROJ-1234"

FILES = {
    "package.json": json.dumps({"name": "fx", "scripts": {"test": "vitest run", "lint": "eslint ."}}, indent=2),
    "pnpm-lock.yaml": "lockfileVersion: 9\n",
    "Makefile": "build:\n\techo build\n\ntest-int:\n\techo int\n",
    ".github/workflows/ci.yml": (
        "jobs:\n  t:\n    steps:\n"
        "      - run: pnpm install --frozen-lockfile\n"
        "      - name: checks\n        run: |\n          pnpm run typecheck\n          pnpm run e2e\n"
        "      - run: echo done\n"
    ),
    "src/pricing/service.ts": "export function price() { return 1 }\n",
    "src/pricing/repo.ts": "export function save() {}\n",
    "src/auth/session.ts": "export const s = 1\n",
    "src/index.ts": "export * from './pricing/service'\n",
    "test/pricing/service.spec.ts": "test('x', () => {})\n",
    "README.md": "# fx\n",
    ".claude/agents/pricing-dev.md": "---\nname: pricing-dev\ndescription: Pricing service specialist for fx\n---\nbody\n",
}

BRIEF = {
    "schemaVersion": "1.0",
    "phase": "intake_complete",
    "ticket": {"key": KEY, "summary": "Cache pricing reads", "url": "https://x/browse/PROJ-1234"},
    "description": "Add a read-through cache in front of pricing.",
    "acceptanceCriteria": [
        {"id": "AC1", "assertion": "Repeated reads hit the cache", "source": "field"},
        {"id": "AC2", "assertion": "Writes invalidate the cache", "source": "field"},
    ],
    "ambiguities": [{"question": "TTL?", "whyItMatters": "Staleness"}],
    "classification": {"changeType": "feat", "scope": "pricing", "breaking": False},
    "repository": {"toplevel": "/ignored"},
}

DRAFT = {
    "ticketKey": KEY,
    "summary": "Read-through cache in front of the pricing service.",
    "codebaseFindings": [{"finding": "uncached read path", "evidence": "src/pricing/service.ts:1"}],
    "workstreams": [
        {
            "id": "ws1", "title": "Add cache wrapper", "objective": "Cache module exists",
            "filesOwned": [{"path": "src/pricing/cache.ts", "action": "create"}],
            "filesRead": ["src/pricing/service.ts"],
            "steps": ["Create src/pricing/cache.ts with get/set"],
            "verification": ["pnpm test -- pricing"],
            "acceptanceCriteria": ["AC1"], "dependsOn": [],
        },
        {
            "id": "ws2", "title": "Invalidate on write", "objective": "Writes clear cache",
            "filesOwned": [{"path": "src/pricing/repo.ts", "action": "modify"}],
            "filesRead": [],
            "steps": ["Edit repo.ts save() to clear the cache"],
            "verification": ["pnpm run test"],
            "acceptanceCriteria": ["AC2"], "dependsOn": [],
        },
        {
            "id": "ws3", "title": "Cache tests", "objective": "Tests cover cache",
            "filesOwned": [{"path": "test/pricing/cache.spec.ts", "action": "create"}],
            "filesRead": ["test/pricing/service.spec.ts"],
            "steps": ["Write test/pricing/cache.spec.ts"],
            "verification": ["pnpm test"],
            "acceptanceCriteria": ["AC1", "AC2"], "dependsOn": ["ws1"],
        },
    ],
    "verification": {"fullSuite": ["pnpm test", "pnpm run lint"], "manualChecks": [], "rollback": "revert branch"},
    "docsToUpdate": ["README.md"],
    "assumptions": [{"ambiguityId": "AMB1", "decision": "60s, configurable"}],
    "openQuestions": [],
}


def git(cwd, *args):
    # ignore the developer's global config (e.g. commit.gpgsign) so fixtures are fast and hermetic
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_NOSYSTEM="1",
                                   GIT_CONFIG_GLOBAL=os.devnull)).stdout


class Fixture:
    """Temp git repo + empty user agents dir. Use as a context manager."""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="planner-test-")
        self._prev_targets = os.environ.get("PLANNER_TARGETS_FILE")
        os.environ["PLANNER_TARGETS_FILE"] = os.path.join(self.tmp, "targets.json")
        self.repo = os.path.join(self.tmp, "repo")
        self.user_agents = os.path.join(self.tmp, "user-agents")
        os.makedirs(self.user_agents)
        for rel, text in FILES.items():
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.repo = os.path.realpath(self.repo)
        self.plans = os.path.join(self.repo, ".claude", "plans")
        return self

    def __exit__(self, *exc):
        if self._prev_targets is None:
            os.environ.pop("PLANNER_TARGETS_FILE", None)
        else:
            os.environ["PLANNER_TARGETS_FILE"] = self._prev_targets
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_brief(self, brief=None, where="jira-cycle"):
        brief = BRIEF if brief is None else brief
        d = os.path.join(self.repo, ".claude", where)
        os.makedirs(d, exist_ok=True)
        name = f"{KEY}.json" if where == "jira-cycle" else f"{KEY}.input.json"
        path = os.path.join(d, name)
        with open(path, "w") as f:
            json.dump(brief, f)
        return path

    def write_draft(self, draft=None, raw=None):
        os.makedirs(self.plans, exist_ok=True)
        with open(os.path.join(self.plans, f"{KEY}.draft.json"), "w") as f:
            f.write(raw if raw is not None else json.dumps(draft or DRAFT))

    def prepare(self):
        from plannerlib import prepare
        self.write_brief()
        return prepare.run(KEY, self.repo, user_agents_dir=self.user_agents)

    def bind(self, key, toplevel):
        from plannerlib.common import bind_target
        bind_target(key, toplevel, "path")

