![cod-code](cod_code.jpg)

# cod-code

**Get more out of Claude Code by understanding how it works inside.** Practical advice, a clean-room spec of the harness, and a runnable Python reimplementation.

## Start here: [the guide](guide/README.md)

Seven chapters of practical advice, every tip linked to the spec requirement behind it:
[instructions & memory](guide/01-instructions-and-memory.md) ·
[permissions & settings](guide/02-permissions-and-settings.md) ·
[hooks recipes](guide/03-hooks-recipes.md) ·
[context, cost & long sessions](guide/04-context-cost-and-long-sessions.md) ·
[tools & long-running work](guide/05-tools-and-long-running-work.md) ·
[agents, skills & parallel work](guide/06-agents-skills-and-parallel-work.md) ·
[agent-friendly repos](guide/07-agent-friendly-repos.md)

A taste, from [the top 20](guide/README.md#the-top-20):
- Monorepo? Put rules in `.claude/rules/*.md` with `paths:` frontmatter. They stay out of context until Claude touches matching files.
- Hooks fail *open* by default. Exit 2 blocks; set `onFailure: "block"` for guards that matter.
- Every Bash call is a fresh shell: the working directory carries over, `export` doesn't.
- Avoid long idle gaps mid-task. Once the prompt cache expires you pay full price to re-send the whole conversation.
- Never ask Claude to `sleep`-poll. Long commands move to the background and report back.

## Also in this repo

- **[`spec/`](spec/INDEX.md):** clean-room functional specs of Claude Code (CLI 2.1.295) and the desktop app's Code tab. They cover behaviour, constants and pseudocode for the agent loop, caching, tools, permissions, compaction, multi-agent, MCP/skills, host protocol and computer use, with no original code or prompt text ([how](CLEANROOM.md)).
- **[`cleanroom/`](cleanroom/README.md):** `harnesslab`, a readable, runnable Python reimplementation (~33k lines, 787 offline tests) built *only* from `spec/`.
- **[`audit/`](audit/PLAYBOOK.md):** a copy-paste prompt (below) that has your coding agent audit *your own* agent harness against the spec and ship improvements.
- **[`tools/extract/`](tools/extract/README.md) + [`EXPLORE.md`](EXPLORE.md):** unpack *your own installed copy* to dig further. Nothing proprietary is hosted here.

## How the harness evolved

[`spec/CHANGES-2.1.236-to-2.1.295.md`](spec/CHANGES-2.1.236-to-2.1.295.md) compares two builds 59 releases apart and describes 56 behaviour changes, each with the design lesson behind it. Across those builds the public surface barely moved. The work went into reliability (sleep-aware watchdogs, a declarative policy for streams that fail part-way), cache economics (compaction timed to cache expiry, change-only context updates), depth of safety checks and multi-agent hygiene.

## Poke through the clean-room harness

```bash
cd cleanroom
uv run --with pytest pytest -q              # 787 tests, offline, ~7 s
uv run python -m harnesslab.cli             # demo REPL against a scripted model
uv run python examples/permission_playground.py
```

Start with [`cleanroom/README.md`](cleanroom/README.md). It includes a guided tour of one turn through about 10 files, and lists what's simulated in `pseudo/` (computer use, desktop host, sandbox profiles) and what's deferred.

## Building your own harness? Paste this into your coding agent

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
