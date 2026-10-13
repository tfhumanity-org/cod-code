#!/usr/bin/env python3
"""Extract an Electron app.asar archive (files stored in the archive only).

Usage: python3 -I unasar.py <app.asar> <out-dir>

Layout: u32 4 | u32 header_size | u32 ... | u32 json_len | JSON index | data.
Entries flagged "unpacked" live beside the archive in app.asar.unpacked/.
"""
import json
import os
import struct
import sys


def main(src, dst):
    f = open(src, "rb")
    f.read(4)
    header_size = struct.unpack("<I", f.read(4))[0]
    f.read(4)
    json_len = struct.unpack("<I", f.read(4))[0]
    index = json.loads(f.read(json_len))
    base = 8 + header_size
    count = 0

    def walk(node, path):
        nonlocal count
        for name, entry in node["files"].items():
            target = os.path.join(path, name.replace("..", "_"))
            if "files" in entry:
                os.makedirs(target, exist_ok=True)
                walk(entry, target)
            elif "link" in entry or entry.get("unpacked"):
                continue
            else:
                f.seek(base + int(entry["offset"]))
                with open(target, "wb") as out:
                    out.write(f.read(entry["size"]))
                count += 1

    os.makedirs(dst, exist_ok=True)
    walk(index, dst)
    print(f"extracted {count} files to {dst}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
