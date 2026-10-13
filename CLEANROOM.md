# Clean-room process

This repo publishes **behavioural specifications** and an **independent reimplementation**. It does not publish Anthropic's code or prompt text. This file records what each participant saw.

```
Shipped binaries ──(local extraction, never committed)──► analysis notes (private, not in this repo)
                                                               │
                                       DIRTY SIDE: spec writers read the notes
                                                               ▼
                                                  spec/  (paraphrased behaviour, data shapes,
                                                          constants, own pseudocode; no quotes,
                                                          no identifiers, no offsets)
                                                               │
                                       ═══════ wall: only spec/ crosses ═══════
                                                               ▼
                                       CLEAN SIDE: implementers given ONLY spec/
                                                               ▼
                                                  cleanroom/  (original Python + pseudocode)
```

## Participants and what they saw

| Stage | Participant | Saw | Did not see | Produced |
|---|---|---|---|---|
| Extraction & analysis | analysis agents (Oct 2026) | locally extracted CLI 2.1.295 / desktop 2.31226.1 bundles | n/a | private analysis notes |
| Spec | 4 spec-writer agents | private analysis notes | extracted bundles | `spec/` |
| Implementation | fresh implementer agents in a separate workspace that contained only a copy of `spec/` | `spec/` | analysis notes, extracted bundles, original binaries | `cleanroom/` |
| Integration | repo maintainer | everything | n/a | copied `cleanroom/` in unmodified except for packaging |

## Rules the spec obeys
- No verbatim prompt, tool-description, error-message or code text from the original.
- Prompts are described by purpose and required content, never restated.
- No minified identifiers, internal function or class names, file offsets, telemetry event names, feature-flag IDs, internal codenames, internal URLs, or unreleased-product details.
- Interface-level facts *are* kept because they're needed for compatibility: public tool names and fields, hook events, settings keys, env vars, file formats, and numeric thresholds.
- Algorithms are written as the spec writers' own pseudocode.

## Honest limits
- **Procedural, not physical.** "Clean" here means the implementers ran in a fresh context with access only to a workspace containing `spec/`, plus instructions not to look elsewhere. It wasn't a separate legal team on separate machines.
- **Copyright only.** A clean room is a defence against copyright claims on the implementation. It doesn't override terms of service, and it doesn't address trade secrets.
- **Not legal advice.** If you plan to rely on this commercially, get your own advice.
- **Unofficial.** Not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are Anthropic trademarks, used here only to identify what was studied.
