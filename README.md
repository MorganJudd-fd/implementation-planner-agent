# Implementation Planner Agent

Turns a ticket brief into an implementation plan decomposed into
parallelizable workstreams for specialist agents — grounded in real file paths,
with exclusive file ownership, computed execution waves, full acceptance
criteria coverage and runnable verification.

Deterministic by design: the LLM explores the codebase and writes a **draft**.
`scripts/planner.py` validates it, makes the judgment calls (via the Keystone
Decision Engine or rules), computes waves and coverage, and writes the plan.
Hooks block everything else.

## Prerequisites

- [Claude Code](https://claude.ai/code)
- Git and Python 3.9+ on PATH (stdlib only, nothing to install)
- Optional: Keystone Decision Engine access (`KEYSTONE_TOKEN`)

## Quick Start

```bash
cd ~/Source/Workflow/implementation-planner-agent
claude --add-dir /path/to/target-repo
```

Then:

```
plan PROJ-1234 --repo commerce        # folder name under ~/Source, or an absolute path
plan PROJ-1234                        # repo found from the intake agent's state file
```

**Launch Claude from this repo.** Hooks and permissions load from here; the
target is reached by absolute path. `--add-dir` lets Read/Glob/Grep see it.

## Input

Any JSON matching `schemas/input-brief.schema.json`. Minimum:

```json
{
  "ticket": { "key": "PROJ-1234", "summary": "Cache pricing reads" },
  "description": "…",
  "acceptanceCriteria": [{ "id": "AC1", "assertion": "…" }],
  "ambiguities": [{ "question": "…", "whyItMatters": "…" }]
}
```

Looked up in order: an explicit path, `<target>/.claude/jira-cycle/<KEY>.json`
(a `jira-intake-agent` state file works as-is), then
`<target>/.claude/plans/<KEY>.input.json`. If none exists, the agent asks you
and writes the input file.

## Target Repository

Each ticket is planned against exactly one repo. `prepare` resolves it once and
binds `KEY → repo` in `.claude/targets.json` (this agent repo, git-ignored).
`finalize`, `render` and the hooks use that binding; writes and `git -C` are
only allowed in the bound repo.

Resolution order:

1. `--repo` from the user: an absolute path, or a **folder name** searched for
   under `policy.json` `repoSearch.roots` (default `~/Source`, 2 levels deep)
2. `repository.toplevel` inside a brief file
3. an existing binding for the KEY
4. a `jira-intake-agent` state file `<repo>/.claude/jira-cycle/<KEY>.json`
   found under the search roots (the intake agent already chose the repo)
5. otherwise exit 3: ask the user "which repo is KEY for?"

Ambiguous names (several matches) also exit 3 with the candidates listed.

## Output

`<target>/.claude/plans/` (ignored via `.git/info/exclude`; no tracked files change):

| File | Written by | Purpose |
|------|-----------|---------|
| `<KEY>.input.json` | you / LLM | Optional hand-written brief (only when no intake state file exists) |
| `<KEY>.context.json` | prepare | Normalized brief + discovered facts |
| `<KEY>.draft.json` | LLM | The proposed workstreams |
| `<KEY>.plan.json` | finalize | **The plan** — `schemas/plan.schema.json` |
| `<KEY>.attempts.json` | finalize | Fix-loop counter |
| `<KEY>.decisions-cache.json` | finalize | Engine answers, so reruns don't flip |

The input brief is never modified.

## How It Works

```
prepare   brief → normalize → discover test commands, agents, high-churn files → context.json
explore   LLM: Read/Glob/Grep (+ Explore agents) → findings with path:line
draft     LLM writes draft.json
finalize  validate (blocking) → decide → compute waves/coverage → plan.json
render    fixed-format summary for human review
```

### Enforced by finalize (blocking)

- Draft matches schema; repo HEAD unchanged since prepare
- Every path is repo-relative, inside the repo (symlinks resolved), and exists
  (or is `create` and absent); evidence paths exist
- File ownership is exclusive across workstreams
- `dependsOn` is valid and acyclic
- Every verification command is a discovered command (+ optional args)
- Every AC is mapped or explicitly uncovered with a reason
- Every ambiguity resolved exactly once (assumption or open question)
- Workstream / file-count limits

Warnings (flagged for review, not blocking): steps that name no file, high-churn
files owned outside the last wave, proposed (unconfirmed) AC.

### Enforced by hooks

| Hook | Blocks |
|------|--------|
| `guard-writes.sh` | Any write except `<repo>/.claude/plans/<KEY>.(draft\|input).json`, where `<repo>` is the one prepare bound KEY to |
| `guard-bash.sh` | Everything except `python3 scripts/planner.py …` and read-only `git rev-parse/status/log/show/diff/ls-files` (only in this repo or a bound target, so another repo's `.git/config` can't run code); shell operators, redirects and multi-line commands; git flags that write or execute (`-c`, `--output`, `-O`, `--ext-diff`) |

Hooks deny with exit 0 + `hookSpecificOutput.permissionDecision: "deny"` so the
reason reaches the model. Both apply to subagents.

## Decision Engine

Per workstream, finalize asks three typed questions:

| Question | Type | Feeds |
|----------|------|-------|
| Which agent should own it (installed agents + baseline + `undetermined`) | choice | `specialty` |
| How risky is it | score low/medium/high | `risk.level` (never below the rule floor) |
| Does it touch auth / secrets / input validation | yes_no | `securitySensitive`, risk floor |

Bands map to action per question:

| Question | act | review | escalate |
|----------|-----|--------|----------|
| specialty | applied | applied + review flag | rule answer + review flag |
| security | applied (OR rule) | applied (OR rule) | rule answer + review flag |
| risk | max(engine, floor) | max(engine, floor) | max(engine, floor) + review flag |

Every answer is recorded in `decisions[]` with its decision id.

Without `KEYSTONE_TOKEN`, rules from `policy.json` answer and one plan-level
review flag asks you to confirm them. A failed engine batch falls back to rules
for that batch's workstreams and adds a warning. `decisionSource` is `keystone`,
`fallback` or `mixed`, derived from the recorded `decisions[]`.

| Env var | Default |
|---------|---------|
| `KEYSTONE_TOKEN` | unset → rules only |
| `KEYSTONE_URL` | `https://developer-bedrock-platform.fandango.com` (https required; redirects refused) |

Check access: `python3 scripts/planner.py probe`.

**Data sent to Keystone:** ticket summary, acceptance criteria text, and per
workstream its title, objective, steps and file paths. Never file contents.
Keystone Shield scans the request; detections appear as plan warnings.
Engine probabilities vary slightly between calls; answers are cached per draft.

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | ok |
| 1 | fixable validation errors (JSON list on stdout) |
| 2 | environment / IO (no git, not a repo, missing path) |
| 3 | needs the user (no brief, fix attempts exhausted) |

## Directory Structure

```
implementation-planner-agent/
├── CLAUDE.md                 # Router
├── .claude/settings.json     # Permissions + hooks
├── policy.json               # Limits, high-churn globs, specialty rules, engine questions
├── rules/                    # 01-identity … 05-finalize-present, guardrails
├── schemas/                  # input-brief, plan-draft, plan
├── scripts/
│   ├── planner.py            # CLI: prepare | finalize | render | probe
│   └── plannerlib/           # common, prepare, validate, decide, finalize, render, hooks
├── hooks/                    # guard-writes.sh, guard-bash.sh
└── tests/                    # python3 -m unittest discover -s tests
```

## Customizing

- **Policy** — edit `policy.json`; bump `version` (recorded in every plan).
- **Engine questions** — `policy.json` `decisions.questions`; bump `decisions.version`.
- **Schemas** — the validator supports a fixed keyword subset and rejects
  unknown keywords (see `scripts/plannerlib/common.py`).

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT
