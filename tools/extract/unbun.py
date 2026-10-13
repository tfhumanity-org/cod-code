#!/usr/bin/env python3
"""Extract the embedded module graph from a Bun single-file executable (Mach-O).

Usage: python3 -I unbun.py <claude-binary> <out-dir>

Layout (Bun StandaloneModuleGraph, as observed in Claude Code 2.1.x):
  __BUN,__bun section = u64 payload_len + payload
  payload ends with "\n---- Bun! ----\n"; the 32 bytes before it are Offsets:
    u64 byte_count | u32 modules_off | u32 modules_len | u32 entry_id | ...
  modules table = fixed-size records starting with four (u32 off, u32 len)
  pointers: name, contents, sourcemap, bytecode. Record size is detected.
Prints a JSON summary to stdout. Never executes the target.
"""
import json
import os
import struct
import sys

TRAILER = b"\n---- Bun! ----\n"
LC_SEGMENT_64 = 0x19
CPU_ARM64 = 0x0100000C


def macho_slice(data):
    magic = struct.unpack(">I", data[:4])[0]
    if magic in (0xCAFEBABE, 0xCAFEBABF):  # fat / universal
        n = struct.unpack(">I", data[4:8])[0]
        wide = magic == 0xCAFEBABF
        size = 32 if wide else 20
        slices = []
        for i in range(n):
            e = data[8 + i * size: 8 + (i + 1) * size]
            if wide:
                cpu, _, off, sz = struct.unpack(">iiQQ", e[:24])
            else:
                cpu, _, off, sz = struct.unpack(">iiII", e[:16])
            slices.append((cpu, off, sz))
        cpu, off, sz = next((s for s in slices if s[0] == CPU_ARM64), slices[0])
        return data[off: off + sz]
    return data


def bun_section(m):
    if struct.unpack("<I", m[:4])[0] != 0xFEEDFACF:
        sys.exit("not a 64-bit Mach-O")
    ncmds = struct.unpack("<I", m[16:20])[0]
    p = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack("<II", m[p: p + 8])
        if cmd == LC_SEGMENT_64 and m[p + 8: p + 24].rstrip(b"\0") == b"__BUN":
            fileoff, filesize = struct.unpack("<QQ", m[p + 40: p + 56])
            return m[fileoff: fileoff + filesize]
        p += size
    sys.exit("no __BUN segment (not a Bun standalone binary?)")


def main(binary, out):
    sec = bun_section(macho_slice(open(binary, "rb").read()))
    n = struct.unpack("<Q", sec[:8])[0]
    p = sec[8: 8 + n]
    t = p.rfind(TRAILER)
    if t < 0:
        sys.exit("Bun trailer not found")
    _, mo, ml, entry = struct.unpack("<QIII", p[t - 32: t - 12])
    table = p[mo: mo + ml]

    def valid(rs):
        if ml % rs:
            return False
        for i in range(min(20, ml // rs)):
            o, l = struct.unpack("<II", table[i * rs: i * rs + 8])
            if not p[o: o + l].startswith(b"/$bunfs/"):
                return False
        return True

    rs = next((r for r in range(32, 129, 4) if valid(r)), None)
    if rs is None:
        sys.exit("could not detect module record size; format changed")
    os.makedirs(out, exist_ok=True)
    files = []
    for i in range(ml // rs):
        no, nl, co, cl = struct.unpack("<4I", table[i * rs: i * rs + 16])
        name = p[no: no + nl].decode(errors="replace").split("/$bunfs/root/")[-1]
        safe = name.replace("..", "_").lstrip("/")
        dest = os.path.join(out, safe)
        os.makedirs(os.path.dirname(dest) or out, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(p[co: co + cl])
        files.append({"name": safe, "bytes": cl, "entry": i == entry})
    files.sort(key=lambda f: -f["bytes"])
    print(json.dumps({"record_size": rs, "modules": len(files),
                      "entry": next((f["name"] for f in files if f["entry"]), None),
                      "largest": files[:10]}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
