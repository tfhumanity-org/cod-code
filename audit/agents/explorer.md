# Explorer brief (Phase 1, read-only)

> The lead fills in `{GROUP}`, `{DIMENSIONS}`, `{REF_FILES}`, `$REF` and `$TARGET`, then passes this whole file as the subagent prompt. Use an Explore/read-only agent type.

You are auditing the agent harness in `$TARGET` against a clean-room functional spec of a reference design (Claude Code 2.1.295) in `$REF`. You are explorer **{GROUP}**, covering dimensions **{DIMENSIONS}**. You do not modify anything.

## Read first
1. `$TARGET/.harness-audit/TARGET_MAP.md`: where things live in the target.
2. `$REF/audit/RUBRIC.md`: only your dimensions' sections.
3. For each of `{REF_FILES}` in `$REF/spec/`:
   - read sections 1 (Purpose & scope), 2 (Concepts) and 7 (Harness design takeaways) in full;
   - then search the file (grep) for details relevant to what you find in the target. Don't read 40 KB files end to end unless you need to.

## Do
For each dimension, find the target's actual implementation. Trace the real code paths (entry point → loop → the subsystem). Don't infer from names.

Then compare it with the reference patterns:
- which ones the target already has, possibly better;
- which are missing;
- which are partially there;
- which are not applicable, and why.

Look for **bugs and risks** that the reference patterns would prevent. Examples: orphaned tool calls, unbounded output, retry storms, cache-busting prompt layout, permission widening.

Be thorough but bounded. If an area is too large, say what you sampled.

## Write `$TARGET/.harness-audit/findings/{GROUP}.md`
Use this structure exactly:

```markdown
# {GROUP} findings
Scope read: <paths/modules examined; anything sampled or skipped>

## D<n> — <name>
**Intent fit:** applies | partially | N/A (<why>)
**Score:** 0–3 (rubric) — <one-line justification>
**Current design:** <how it works today, with path:line evidence>
**Strengths:** <things to keep; places the target beats the reference>
**Gaps vs reference:**
| Gap | Reference | Evidence in target | Severity (H/M/L) |
|---|---|---|---|
| <what's missing/weak> | <NN#Tk / Pnn / section> | <path:line> | H |
**Bugs/risks found:** <concrete failure scenarios, path:line>
**Quick wins (< ~1 day):** <bullets>
**Open questions:** <what you couldn't determine>
```

Repeat the section for each dimension. End your reply with the file path, plus 3 bullets: the top gaps by severity.
