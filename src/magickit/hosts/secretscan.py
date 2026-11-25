"""A last check on probe output. Not the boundary.

The boundary is that probes rebuild their output from validated values
(:mod:`.fields`) and never return log bodies (ruling 2, msg-1770). This
module looks at every string that survived that and drops the ones shaped
like a credential, as defence in depth against a validator that is wider
than it should be. Einstein msg-1758 Obj-3 is why it cannot be more than
that: a short password in prose matches no rule here.

Rules: the known key prefixes and the Bearer rule of Lexora's ingress
redactor (``src/lexora/ingress/redact.py`` at ``1c5e29a``, ADR-2026-05-31-14
§7.3), its entropy rule (32+ characters, >= 4.0 bits/char, hex and UUIDs
excluded), and the three msg-1757 B-2 adds (URL userinfo, Authorization /
Cookie headers, ``token=``-style assignments). Copied rather than imported:
Lexora is a different repository and service.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_KEY_RES = [
    re.compile(r"(?<![A-Za-z0-9_])" + p)
    for p in (
        r"sk-ant-[A-Za-z0-9_\-]{20,}",
        r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}",
        r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}",
        r"gh[pousr]_[A-Za-z0-9]{36,}",
        r"github_pat_[A-Za-z0-9_]{22,}",
        r"glpat-[A-Za-z0-9_\-]{20,}",
        r"(?:AKIA|ASIA)[0-9A-Z]{16}",
        r"AIza[0-9A-Za-z_\-]{35}",
        r"xox[abposr]-[A-Za-z0-9\-]{10,}",
        r"npm_[A-Za-z0-9]{36}",
    )
]
_OTHER_RES = {
    "bearer": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{8,}=*"),
    "url_userinfo": re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,15}://[^\s/:@]+:[^\s/@]+@"),
    "auth_header": re.compile(r"(?i)\b(?:proxy-)?authorization\s*:|\b(?:set-)?cookie\s*:"),
    "keyword_kv": re.compile(r"(?i)(?:token|secret|password|passwd|api_?key)\s*[=:]\s*\S+"),
    "pem": re.compile(r"-----BEGIN [A-Z0-9 ]+-----"),
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
ENTROPY_THRESHOLD = 4.0


def _entropy(text: str) -> float:
    counts = Counter(text)
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def secret_like(text: str) -> str | None:
    """The rule ``text`` trips, or ``None``. Never returns the match."""
    for rx in _KEY_RES:
        if rx.search(text):
            return "key"
    for name, rx in _OTHER_RES.items():
        if rx.search(text):
            return name
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        if _HEX_RE.match(token) or _UUID_RE.match(token):
            continue
        if _entropy(token) >= ENTROPY_THRESHOLD:
            return "entropy"
    return None


def scrub(value: Any, counts: Counter[str]) -> Any:
    """Return ``value`` with every secret-shaped string replaced by ``None``.

    Walks dicts (keys and values), lists and tuples. A dict key that trips a
    rule drops the whole entry. ``counts`` gets one increment per dropped
    string under ``secret_like:<rule>``.
    """
    if isinstance(value, str):
        rule = secret_like(value)
        if rule is None:
            return value
        counts[f"secret_like:{rule}"] += 1
        return None
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str):
                rule = secret_like(k)
                if rule is not None:
                    counts[f"secret_like:{rule}"] += 1
                    continue
            out[k] = scrub(v, counts)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub(v, counts) for v in value]
    return value
