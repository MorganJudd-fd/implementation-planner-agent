# Step 4: Write the Draft

Write `<target>/.claude/plans/<KEY>.draft.json` matching
`schemas/plan-draft.schema.json`. This is the **only** plan file you author.

## You Write

| Field | Rule |
|-------|------|
| `ticketKey` | The key from the context |
| `summary` | One paragraph: the approach |
| `codebaseFindings[]` | `{finding, evidence}` — evidence is a real `path[:line]` |
| `workstreams[]` | See below |
| `verification.fullSuite[]` | Commands from `context.testCommands` that gate the whole change |
| `verification.manualChecks[]` | Anything a human must eyeball |
| `verification.rollback` | How to undo |
| `docsToUpdate[]` | Docs the change makes stale |
| `assumptions[]` | `{ambiguityId, decision}` |
| `openQuestions[]` | `{ambiguityId?, question, whyItMatters}` |
| `uncoveredAcceptanceCriteria[]` | `{id, reason}` — only when an AC genuinely cannot be planned |

## You Do NOT Write

`waves`, `acCoverage`, final `specialty`, `risk`, `decisions`, `reviewFlags`.
Finalize computes them. Adding them fails schema validation.

## Workstreams

A workstream is a unit one agent completes independently, with its own file
ownership and verification.

| Field | Rule |
|-------|------|
| `id` | `ws1`, `ws2`, … unique |
| `title` | Imperative summary |
| `specialtyHint` | Optional. Name from `context.agents` or the `policy.json` baseline. The engine/rules decide the final value |
| `objective` | What "done" means, one sentence |
| `filesOwned[]` | `{path, action}`, action ∈ `create` / `modify` / `delete`. **Exclusive across all workstreams.** `create` → must not exist; `modify`/`delete` → must exist |
| `filesRead[]` | Existing files it reads but must not modify |
| `steps[]` | Ordered, concrete; each step names the file it touches |
| `verification[]` | Commands from `context.testCommands`, optional trailing args (`npm test -- pricing/cache`) |
| `manualVerification` | Only when no discovered command can prove it |
| `acceptanceCriteria[]` | AC ids from the context |
| `dependsOn[]` | Workstream ids that must land first. No cycles |
| `riskNote` | Optional context for the risk decision |

### Ownership and Parallelism

- One owner per file. If two workstreams need the same file, merge them or
  serialize with `dependsOn`. Prefer serializing over splitting a file.
- If ws2 needs ws1's interface, either depend on ws1, or carve the interface
  into its own small workstream so the rest can run in parallel.
- `context.highChurnFiles` (lockfiles, barrels, registries) get exactly one
  owner, ideally in a workstream that runs last.
- Inherently serial work is a legitimate outcome. Do not force parallelism.

### Sizing

Max workstreams and files per workstream are in `policy.json` `limits`. A
workstream per one-line edit is overhead; twelve files is not independently
verifiable.

## Ambiguities

Every `AMBn` in `context.brief.ambiguities` appears **exactly once**: as an
assumption (preferred, where a reasonable default exists) or an open question
(only when it genuinely blocks planning).

## Acceptance Criteria

Every AC id maps to at least one workstream, or is listed in
`uncoveredAcceptanceCriteria` with a reason. Never paper over a gap.
