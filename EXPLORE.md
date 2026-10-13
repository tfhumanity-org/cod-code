# Explore it yourself

This repo never hosts Anthropic's code. If you have Claude Code or the Claude desktop app installed, you can unpack **your own copy** and look around. The extraction is read-only: the scripts parse files and never run them. Check that this is permitted for you under the terms you agreed to, and don't redistribute what you extract.

```bash
git clone https://github.com/TheArctesian/cod-code && cd cod-code
./tools/extract/extract.sh          # finds installed builds → ./.extracted/ (gitignored)
```

You get:
- **`.extracted/cli-<ver>/`:** the CLI's bundled modules (`chunk-*.js`, entry `cli`), bundled docs (`*.md`), and native add-ons (`*.node`).
- **`.extracted/desktop-<ver>/`:** the desktop app's main-process bundles (`.vite/build/`) and renderers.

Format details are in [`tools/extract/README.md`](tools/extract/README.md).

## How to search minified bundles
The files are multi-megabyte single lines. Locate files with `grep -lF`, then cut out context with Python:

```bash
# which files mention it
grep -lF 'compact' .extracted/cli-*/chunk-*.js

# show context around each hit (offset + 600 chars before / 900 after)
python3 -I -c "import re,sys;d=open(sys.argv[1]).read();[print(m.start(),repr(d[max(0,m.start()-600):m.start()+900])) for m in list(re.finditer(re.escape(sys.argv[2]),d))[:4]]" FILE 'needle'
```

Some `grep` builds (ugrep) refuse long `.{0,N}` context regexes on these files. Python slicing always works.

## Good anchors
Identifiers are minified and change every release. **String literals don't**, so anchor on them:

| To find | Search for |
|---|---|
| Telemetry events (a map of every feature) | `tengu_` (CLI). On desktop, look for event-name string literals passed to the logging call |
| Feature flags / experiments | the long dot-separated flag lists in the desktop main chunk; `__gb__` |
| Settings & preferences | a known key such as `ccBranchPrefix`, then read the schema object around it |
| Env vars | `CLAUDE_CODE_`, `ANTHROPIC_`, `DISABLE_` |
| Tool definitions | a tool name in quotes (`"Read"`, `"Bash"`), or `isConcurrencySafe` |
| System prompt pieces | distinctive phrases you've seen the agent say about itself |
| Hook events | `PreToolUse`, `SessionStart`, `PreCompact` |
| Native add-on surface | `nm -gU file.node`, `strings -a file.node`, `otool -L file.node` |
| Desktop MCP tools | `ccd_`, `createSdkMcpServer` |

## Ground rules if you publish findings
- **Describe behaviour, don't paste code or prompt text.** That keeps you on the same side of the line as this repo. If a finding is worth sharing, consider a PR to `spec/` written in your own words.
- **Don't publish unreleased-product details** you stumble on: unannounced features, codenames, flags for things that aren't launched.
- **Note the version.** Findings go stale fast.
