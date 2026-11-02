# Step 2: Prepare

Run:

```
python3 scripts/planner.py prepare <KEY|brief.json> [--repo <path or folder name>]
```

The script:

0. Resolves the target repo and binds the KEY to it (see README "Target
   Repository"). Report `repo` and `resolvedVia` from its output to the user.

1. Finds the brief: an explicit path, else `<target>/.claude/jira-cycle/<KEY>.json`,
   else `<target>/.claude/plans/<KEY>.input.json`.
2. Validates it against `schemas/input-brief.schema.json` and normalizes it:
   acceptance criteria keep their ids (proposed ones are flagged), ambiguities
   are numbered `AMB1..n`.
3. Discovers facts: git HEAD, dirty state, **test commands** (from
   `package.json`, `Makefile`, Python config, GitHub workflows), **installed
   agents** (`<target>/.claude/agents`, `~/.claude/agents`), and **high-churn
   files** (lockfiles, barrels, registries).
4. Writes `<target>/.claude/plans/<KEY>.context.json` and ignores
   `.claude/plans/` via `.git/info/exclude` (no tracked file changes).

## Exit Codes

| Code | Meaning | Do |
|------|---------|-----|
| 0 | ok | Read `<KEY>.context.json`, continue to step 3 |
| 1 | brief invalid | Show the errors to the user; ask for the missing information |
| 2 | environment | Report the message (no git, path missing, not a repo) and stop |
| 3 | which repo? | The message lists candidates (or none). Ask the user which repo the ticket is for, rerun with `--repo <path or name>` |
| 3 | no brief | Ask the user for summary, description, acceptance criteria and ambiguities. Write them to `<target>/.claude/plans/<KEY>.input.json` in the input-brief shape, then rerun prepare |

Never plan against a summary line alone. Acceptance criteria are required.

## Treat the Context as Authoritative

`context.testCommands` is the complete allowlist of verification commands.
`context.agents` plus the baseline in `policy.json` is the complete list of
specialties. Do not look for, or invent, others.

If `context.repository.dirty` is true, tell the user now.
