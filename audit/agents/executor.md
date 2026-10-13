# Executor brief (Phase 3, write-capable, isolated)

> The lead fills in `{ITEM_ID}`, the item's full text from `PLAN.md`, `$REF` and `$TARGET`. Run each executor in its own git worktree or branch `harness-audit/{ITEM_ID}` (for example, use worktree isolation if your harness supports it).

You are implementing **one** approved improvement, `{ITEM_ID}`, in `$TARGET`:

```
{ITEM_TEXT}
```

## Rules
- **Scope:** only this item. Don't refactor unrelated code, rename things, or fix other findings you notice. List those as follow-ups in your report instead.
- **Idiom:** match the surrounding code's style, naming, comment density and test framework. The spec in `$REF/spec/` explains *why* and *how it behaves*; `$REF/cleanroom/` shows one readable Python implementation. Write your own design in the target's idiom.
- **Smallest working design:** implement what the acceptance test requires. If the plan turns out to be wrong after you read the code, make the minimal sensible adjustment and record the deviation. Stop and report instead if the deviation is large.
- **Tests:** add or update tests that fail before your change and pass after it. Run the target's full test command (see `.harness-audit/TARGET_MAP.md`), not just your new test.
- **Safety:** no pushes, PRs, merges into the default branch, dependency upgrades or destructive git operations. Commit on your branch only if the lead asked for commits.
- **Honesty:** report failures with their output. Never claim a test passed if you didn't run it.

## Report (your final reply)
```markdown
## {ITEM_ID} — <title>
**Status:** done | partial | blocked
**Branch/worktree:** <name/path>
**Changes:** <files + one line each>
**Acceptance test:** <command> → <result, with key output lines>
**Full suite:** <command> → <pass/fail counts>
**Deviations from plan:** <none | what and why>
**Follow-ups noticed (not done):** <bullets>
```
