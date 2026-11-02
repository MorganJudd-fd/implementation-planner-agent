# Step 5: Finalize and Present

## Finalize

```
python3 scripts/planner.py finalize <KEY>
```

Add `--no-engine` only if the user asks to skip the decision engine.

The script validates the draft, then:

- **Decides** specialty, risk and security sensitivity per workstream. With
  `KEYSTONE_TOKEN` set it asks the Keystone Decision Engine; otherwise (or per
  failed batch, which adds a warning) it applies the rules in `policy.json`.
  Uncertain engine answers become review flags (see README "Decision Engine"
  for the per-question band table).
- **Computes** waves from `dependsOn`, AC coverage, worktree isolation, speedup.
- **Writes** `<target>/.claude/plans/<KEY>.plan.json` (validated against
  `schemas/plan.schema.json`).

## Fix Loop

| Exit | Meaning | Do |
|------|---------|-----|
| 0 | plan written | Go to Present |
| 1 | `errors[]` returned | Fix **every** error in the draft (each has `code`, `path`, `message`, `fix`), then rerun finalize |
| 2 | environment | Report and stop |
| 3 | attempts exhausted | Stop. Show the user the last errors and ask how to proceed |

Attempts are counted by the script (`policy.json` `limits.maxFinalizeAttempts`).
`stale_context` means the repo changed: rerun prepare, re-check findings, then
finalize.

Never edit `<KEY>.plan.json`, `<KEY>.context.json` or any other generated file.
Hooks block it.

## Present

```
python3 scripts/planner.py render <KEY>
```

Print the output **verbatim**. Do not summarize, reorder or embellish it — it is
the review artifact. After it, add at most two sentences calling out the most
important review flag or open question, if any.
