# Extraction tools

These scripts refresh the reference for newer builds. They are read-only: they parse the files and never execute the targets. Output goes in `.extracted/`, which is gitignored; don't commit it.

```bash
./tools/extract/extract.sh            # auto-locates binaries, extracts to ./.extracted/
```

## Where the targets live (macOS)

| Target | Path |
|---|---|
| Claude Code CLI managed by the desktop app | `~/Library/Application Support/Claude/claude-code/<version>/<sha12>/claude.app/Contents/MacOS/claude` |
| Claude Code CLI installed standalone | `readlink -f "$(command -v claude)"` |
| Desktop app JS | `/Applications/Claude.app/Contents/Resources/app.asar` |
| Desktop native add-ons | `…/Resources/app.asar.unpacked/node_modules/@ant/{claude-swift,claude-native}/` |
| Desktop helper binaries | `/Applications/Claude.app/Contents/Helpers/` (e.g. `app-cu-helper`) |
| Desktop VM disk image (Cowork) | `…/Resources/smol-bin.arm64.img` |

## Formats

- **Bun single-file executable** ([`unbun.py`](unbun.py)):
  1. Find the Mach-O segment `__BUN` (universal binaries choose the arm64 slice).
  2. Its first u64 is the payload length.
  3. The payload ends with `\n---- Bun! ----\n`. The 32 bytes before the trailer hold `u64 byte_count | u32 modules_off | u32 modules_len | u32 entry_id | …`.
  4. The module table is fixed-size records (52 bytes in 2.1.236 and 2.1.295; the script detects the size). Each record starts with four `(u32 off, u32 len)` pointers: name, contents, sourcemap, bytecode.
  5. Names are `/$bunfs/root/<file>`.
  6. Bundled docs come out as `*.md.zst`; decompress them with `zstd -d`.
  7. Native add-ons come out as `*.node`. Analyse them with `otool -L`, `nm -gU`, `xcrun swift-demangle`, `strings`, or a disassembler.
- **Electron asar** ([`unasar.py`](unasar.py)):
  1. `u32 4 | u32 header_size | u32 | u32 json_len | JSON index | data`.
  2. Data offsets are relative to `8 + header_size`.
  3. Entries marked `unpacked` live in `app.asar.unpacked/`.

## Reading minified bundles
- Anchor on string literals: prompt text, tool descriptions, error messages, env var names, settings keys, and `tengu_*` telemetry event names.
- Locate the file with `grep -lF 'literal' .extracted/cli/chunk-*.js`, then slice context with Python. Some greps choke on long `.{0,N}` regexes over multi-MB single-line files.
  ```bash
  python3 -I -c "import re,sys;d=open(sys.argv[1]).read();[print(m.start(),repr(d[max(0,m.start()-600):m.start()+900])) for m in list(re.finditer(re.escape(sys.argv[2]),d))[:4]]" FILE 'needle'
  ```
- In private notes, cite evidence as `chunk-xxxx.js@<char offset>` and tag each claim observed or inferred. Never carry those citations into `spec/`.

## Refresh workflow (for maintainers)
1. Run `extract.sh` for the new version.
2. Update the private analysis notes (`.private/reference/`, never committed) by re-checking their observed claims against the new bundles.
3. Re-derive the affected `spec/` sections using the rules in [`CLEANROOM.md`](../../CLEANROOM.md): paraphrase only, no identifiers or offsets. Keep the takeaway and pattern numbering stable.
4. Changes to `cleanroom/` must be made from `spec/` alone, by someone who has not read the extracted code.
