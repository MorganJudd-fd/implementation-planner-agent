# Step 3: Explore the Codebase

**Do not plan against an imagined repository.** Explore until you can name the
real file every change lands in.

## Tools

Use **Read, Glob and Grep** only. Shell pipelines (`grep | head`, `find …`,
`cat`) are blocked by hook and are never needed. Read-only `git log/show/diff`
are allowed for history.

For a wide or unfamiliar codebase, dispatch parallel `Explore` agents, one per
subsystem. Tell each one: use Read/Glob/Grep only, no Bash pipelines, and
report every finding as `path:line — finding`.

## Establish

- **Entry points** — where the described behavior lives today. Grep for the
  domain nouns from the ticket summary and acceptance criteria.
- **Existing patterns** — how this codebase already solves this shape of
  problem. Extend the existing pattern; justify any new one in the draft.
- **Test conventions** — framework, placement, naming, how a focused subset
  runs. Match these to entries in `context.testCommands`.
- **Config and docs** — README, CLAUDE.md, ADRs, OpenAPI specs, migrations.
- **Blast radius** — call sites of anything whose signature you will change.

## Record Evidence

Every finding needs a repo-relative path, optionally `:line`
(`src/pricing/cache.ts:42`). Finalize rejects evidence paths that do not exist.
