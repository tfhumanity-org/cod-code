#!/usr/bin/env bash
# Locate Claude Code / Claude desktop artifacts on macOS and extract them (read-only).
# Usage: tools/extract/extract.sh [out-dir]   (default: ./.extracted)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-.extracted}"
mkdir -p "$OUT"

managed="$(ls -d "$HOME/Library/Application Support/Claude/claude-code"/*/*/claude.app/Contents/MacOS/claude 2>/dev/null | sort -V | tail -1 || true)"
standalone="$(command -v claude >/dev/null 2>&1 && readlink -f "$(command -v claude)" || true)"

for bin in "$managed" "$standalone"; do
  [ -n "$bin" ] && [ -f "$bin" ] || continue
  ver="$(echo "$bin" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || echo unknown)"
  echo "== CLI $ver: $bin"
  python3 -I "$HERE/unbun.py" "$bin" "$OUT/cli-$ver" | head -5
  if command -v zstd >/dev/null; then
    for f in "$OUT/cli-$ver"/*.zst; do [ -e "$f" ] && zstd -dqf "$f" -o "${f%.zst}"; done
  fi
done

asar=/Applications/Claude.app/Contents/Resources/app.asar
if [ -f "$asar" ]; then
  ver="$(defaults read /Applications/Claude.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null || echo unknown)"
  echo "== Desktop $ver"
  python3 -I "$HERE/unasar.py" "$asar" "$OUT/desktop-$ver"
fi
echo "Done. Output in $OUT (gitignored)."
