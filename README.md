# cod-code

**A clean-room study of how Anthropic's Claude Code harness works, plus an agent-run audit that improves *your* harness with it.**

- **`spec/`:** clean-room functional specs of Claude Code (CLI 2.1.295) and the Claude desktop app's Code tab (2.31226.1). They cover behaviour, data shapes, constants and pseudocode for the agent loop, prompt caching, tools, permissions and sandbox, compaction, multi-agent, MCP/skills/plugins, the host protocol and computer use. They contain no original code or prompt text ([how](CLEANROOM.md)).
- **`cleanroom/`:** `harnesslab`, a readable, runnable Python reimplementation (~33k lines, 778 offline tests) built *only* from `spec/`. Read it like a textbook: every module cites the spec requirement it implements.
- **`audit/`:** a gated **Explore → Plan → Execute** playbook, a 15-dimension rubric, subagent briefs and output templates. A coding agent uses them to audit your harness and ship improvements.
- **`tools/extract/` + [`EXPLORE.md`](EXPLORE.md):** unpack *your own installed copy* to dig for more. Nothing proprietary is hosted here.

## Poke through the clean-room harness

```bash
cd cleanroom
uv run --with pytest pytest -q              # 778 tests, offline, ~7 s
uv run python -m harnesslab.cli             # demo REPL against a scripted model
uv run python examples/permission_playground.py
```

Start with [`cleanroom/README.md`](cleanroom/README.md). It includes a guided tour of one turn through about 10 files, and lists what's simulated in `pseudo/` (computer use, desktop host, sandbox profiles) and what's deferred.

## Use it: paste this into your coding agent

Open your agent (Claude Code, or any agent that can run shell commands and spawn subagents) **in the root of the project whose harness you want to improve**. Then paste:

````text
Audit and improve this project's agent harness using the reference design at
https://github.com/TheArctesian/cod-code (a clean-room study of Claude Code's harness).

1. Setup: shallow-clone it read-only:
   git clone --depth 1 https://github.com/TheArctesian/cod-code "${TMPDIR:-/tmp}/cod-code"
   (if you can't clone, read files via https://raw.githubusercontent.com/TheArctesian/cod-code/main/<path>).
   Read AGENTS.md, then audit/PLAYBOOK.md, audit/RUBRIC.md and spec/INDEX.md in full.
   Follow audit/PLAYBOOK.md exactly. This project is $TARGET; the clone is $REF.
2. Phase 0: map this repo into .harness-audit/TARGET_MAP.md.
3. Phase 1 (Explore): spawn the 5 read-only explorer subagents IN PARALLEL using
   audit/agents/explorer.md (groups E1–E5 from the playbook). Wait for ALL of them,
   then write .harness-audit/SCORECARD.md yourself.
4. Phase 2 (Plan): spawn the 3 read-only planner subagents IN PARALLEL using
   audit/agents/planner.md (PL1–PL3). Wait for ALL of them, then merge into a ranked,
   dependency-ordered .harness-audit/PLAN.md.
5. STOP and show me the PLAN.md summary table. Ask which items/milestones to execute.
6. Phase 3 (Execute): only after I approve, run one executor subagent per approved item
   (audit/agents/executor.md), each in its own git worktree/branch, independent items in
   parallel (max 4). Integrate one at a time, run the full test suite after each, and
   finish with .harness-audit/REPORT.md.

Rules: cite path:line for every claim about this repo and the reference ID (e.g. 03#T7, P12)
for every pattern you borrow; treat spec items marked "Confidence: inferred" as lower confidence;
adapt patterns to this project's intent and idioms rather than copying; no pushes/PRs/merges
to the default branch unless I ask.
Focus (optional): <e.g. "context management and tool reliability" — or delete this line>
````

**Fully autonomous run:** replace step 5 with `Execute milestone M1 without asking, then stop and report.`

**No subagents in your harness?** The playbook says how to run each brief yourself in sequence.

## What you get in your project

```
.harness-audit/
  TARGET_MAP.md      where each subsystem lives in your code
  findings/E1..E5.md per-dimension evidence, scores, gaps vs. reference, bugs found
  SCORECARD.md       15-dimension maturity table + risks to fix regardless
  plans/PL1..PL3.md  themed improvement designs in your codebase's idiom
  PLAN.md            ranked, dependency-ordered roadmap with acceptance tests  ← you approve here
  REPORT.md          what shipped, test evidence, before/after scores
+ branches harness-audit/I-xx with the implemented changes
```

## Repo layout

| Path | What |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Entry point for agents: rules, layout, how to cite |
| [`spec/INDEX.md`](spec/INDEX.md) | Architecture picture, the top patterns (`P01`–`P28`), and a map of the 11 spec files |
| `spec/00`–`10` | Functional specs (requirements, pseudocode, constants). Each ends with "Harness design takeaways" (`NN#Tk`) |
| `cleanroom/` | `harnesslab`: a Python reference implementation built only from `spec/` |
| [`CLEANROOM.md`](CLEANROOM.md) | The process: who saw what, the rules, the limits |
| [`EXPLORE.md`](EXPLORE.md) | How to extract and search your own installed copy |
| [`audit/PLAYBOOK.md`](audit/PLAYBOOK.md) | The 3-phase gated process |
| [`audit/RUBRIC.md`](audit/RUBRIC.md) | Dimensions D1–D15 with 0–3 maturity signals |
| `audit/agents/` | Explorer / planner / executor subagent briefs |
| `audit/templates/` | Output skeletons for every artifact |
| [`tools/extract/`](tools/extract/README.md) | Re-extract the CLI (Bun binary) and desktop app (`app.asar`) for newer versions |

## Explore it yourself
Want to find more? Unpack your own installed copy (read-only; nothing proprietary is hosted here):

```bash
./tools/extract/extract.sh     # → ./.extracted/ (gitignored)
```

Then see [`EXPLORE.md`](EXPLORE.md) for search anchors and ground rules. If you find something, describe it in your own words in a PR to `spec/`. Don't paste code or prompt text.

## Caveats
- **Clean room.** `spec/` was written from private analysis notes, and `cleanroom/` was written from `spec/` alone. Neither contains original code or prompt text. See [`CLEANROOM.md`](CLEANROOM.md) for the process and its limits.
- **Static analysis, point in time.** These are specific builds analysed without running them. Requirements are marked "Confidence: observed" or "Confidence: inferred".
- **Unofficial.** Not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are Anthropic trademarks.
