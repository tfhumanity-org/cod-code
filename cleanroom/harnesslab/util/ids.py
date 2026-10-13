"""Identifier helpers (FINAL - architect).

* ``project_slug`` implements 05 §2.6: the cwd with every non-alphanumeric char
  replaced by ``-``; over 200 chars it is truncated and suffixed with a short
  base-36 hash.
* ``short_id`` produces task / agent ids such as ``a1b2c3d4``.
* ``hex16_path`` is the checkpoint backup name stem of 05 §2.5.
"""

from __future__ import annotations

import hashlib
import re
import secrets

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
SLUG_MAX = 200


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


def project_slug(cwd: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    if len(slug) <= SLUG_MAX:
        return slug
    digest = int.from_bytes(hashlib.sha256(cwd.encode()).digest()[:6], "big")
    return f"{slug[:SLUG_MAX]}-{_base36(digest)}"


def short_id(prefix: str = "", nbytes: int = 4) -> str:
    return f"{prefix}{secrets.token_hex(nbytes)}"


def hex16_path(abs_path: str) -> str:
    """First 16 hex chars of sha256(abs_path) (05 §2.5 backup naming)."""
    return hashlib.sha256(abs_path.encode()).hexdigest()[:16]
