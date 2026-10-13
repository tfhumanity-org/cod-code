# Planner brief (Phase 2, read-only)

> The lead fills in `{THEME}`, `{DIMENSIONS}`, `$REF` and `$TARGET`, then passes this file as the subagent prompt. Use a Plan/read-only agent type.

You are planner **{THEME}**, designing improvements to the agent harness in `$TARGET` for dimensions **{DIMENSIONS}**. You use the reverse-engineered Claude Code reference in `$REF` as inspiration, not as a spec. You do not modify anything.

## Read first
- `$TARGET/.harness-audit/SCORECARD.md`
- `$TARGET/.harness-audit/TARGET_MAP.md`
- The findings files covering your dimensions in `$TARGET/.harness-audit/findings/`
- The reference sections those findings cite. Read the cited takeaway or section in full: you need the *mechanics* (thresholds, data shapes, ordering), not just the slogan.
- The target code you would change. Read enough to be confident that your file list and approach are right.

## Do
1. Turn the gaps, bugs and risks into candidate **improvement items**. Merge gaps that share one fix. Drop items whose cost outweighs the benefit for the target's stated intent. Say what you dropped and why.
2. For each item, design the change *in the target's idiom*: its language, architecture, naming and test style. Prefer the smallest design that captures most of the benefit. Note where the reference's full design would be overkill.
3. Make every item independently verifiable. Its acceptance test must run locally (a unit test, a scripted transcript replay, or a measurable metric).
4. Note dependencies between items, including dependencies on items another planner is likely to propose (for example, a typed decision reason before a classifier).

## Write `$TARGET/.harness-audit/plans/{THEME}.md`

```markdown
# {THEME} plan
## Dropped / deferred
- <gap> — <why>

## Items
### <short-title>
- **Dimension / gap:** D<n> — <gap>, findings ref <file#section>
- **Reference:** <NN#Tk / Pnn> — <one line on the mechanism being borrowed>
- **Problem today:** <failure scenario or cost, path:line>
- **Design:** <concrete change: types, functions, data flow, constants; in target idiom>
- **Files:** <paths to create/modify>
- **Steps:** 1… 2… 3…
- **Acceptance test:** <exact test(s) to add and command to run; what passes>
- **Impact:** H/M/L — <why> · **Effort:** S/M/L · **Risk:** H/M/L — <what could break>
- **Depends on:** <other items or none> · **Rollback:** <how to revert / flag>
```

End your reply with the file path and your top 3 items by impact ÷ effort.
