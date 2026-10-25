"""Device-model identification for OTA payloads and OPPO packages.

The tool itself is model-agnostic: the EDL package and the OTA are supplied by the
user and only need to follow the OPPO/OnePlus/realme on-disk formats.  These helpers
exist so a mismatched pair (EDL for one model, OTA for another) produces a warning
instead of a silently broken super image.
"""

from __future__ import annotations

import re

# ota_target_version looks like "CPH2841_11.C.10_1100_202610181148"
_TARGET_RE = re.compile(r"^([A-Za-z0-9]+?)(?:_|$)")


def model_from_payload_properties(props: dict[str, str]) -> str | None:
    """Best-effort model of an OTA payload from payload_properties.txt."""
    compat = props.get("compatible_ota_prefix_list", "")
    for token in compat.split(","):
        token = token.strip()
        if token:
            return token.upper()
    for key in ("ota_target_version", "post-build", "pre-build"):
        value = props.get(key, "")
        match = _TARGET_RE.match(value)
        if match:
            return match.group(1).upper()
    return None


def models_compatible(a: str | None, b: str | None) -> bool:
    """True when the two model strings plausibly describe the same device.

    Compared leniently because vendors mix forms: ``CPH2841`` / ``CPH2841IN`` /
    ``CPH2841export``; ``PYB110`` / ``PYB110PRE``.
    """
    if not a or not b:
        return True
    a, b = a.strip().upper(), b.strip().upper()
    if a == b:
        return True
    return a.startswith(b) or b.startswith(a)
