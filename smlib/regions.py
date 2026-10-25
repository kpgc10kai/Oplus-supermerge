"""Sales-region table and region-variant selection for BBK firmware.

One EDL package can carry several variants of the regional partitions
(``my_preload``, ``my_stock``, ``my_carrier``, ``my_region`` ...), one per
sales region.  The device picks one at runtime from NV, so a super image has
to be built for one region up front.

The table lives in ``regions.json`` next to this module and is merged with
any ``--regions-file`` (and with a ``regions.json`` dropped into the EDL
folder).  ``numeric`` values can be filled in as they are identified.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from .util import Fail

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TABLE = os.path.join(HERE, "regions.json")

GENERIC_NAMES = {"generic", "none", "default", "-"}


@dataclass
class Region:
    code: str
    name: str = ""
    numeric: str | None = None
    aliases: list[str] = field(default_factory=list)

    def tokens(self) -> set[str]:
        out = {self.code.lower()}
        if self.numeric:
            out.add(self.numeric.lower())
        for alias in self.aliases:
            out.add(alias.lower().replace(" ", ""))
        return out

    def matches(self, tag: str) -> bool:
        """True if *tag* (a filename fragment) denotes this region."""
        if not tag:
            return False
        text = tag.lower()
        tokens = self.tokens()
        if text in tokens or text.replace(" ", "") in tokens:
            return True
        for part in re.split(r"[^a-z0-9]+", text):
            if part and part in tokens:
                return True
        return False

    def label(self) -> str:
        bits = [self.code]
        if self.numeric:
            bits.append(self.numeric)
        if self.name:
            bits.append(self.name)
        return " / ".join(bits)


def load_table(extra_paths: list[str] | None = None) -> dict[str, Region]:
    """Load the built-in table, then merge any overriding files."""
    table: dict[str, Region] = {}
    for path in [DEFAULT_TABLE, *(extra_paths or [])]:
        if not path:
            continue
        if not os.path.exists(path):
            raise Fail(f"region table not found: {path}")
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as exc:
                raise Fail(f"{path}: invalid JSON ({exc})")
        for item in data.get("regions", []):
            code = str(item.get("code", "")).strip().upper()
            if not code:
                continue
            table[code] = Region(
                code=code,
                name=item.get("name", "") or "",
                numeric=(str(item["numeric"]).strip() if item.get("numeric") else None),
                aliases=[str(a) for a in item.get("aliases", [])],
            )
    return table


def find(table: dict[str, Region], spec: str) -> Region | None:
    """Resolve 'IN', 'india', '00011011' ... to a table entry."""
    key = spec.strip()
    if not key:
        return None
    upper = key.upper()
    if upper in table:
        return table[upper]
    lowered = key.lower()
    for region in table.values():
        if region.numeric and region.numeric.lower() == lowered:
            return region
    for region in table.values():
        if region.name.lower() == lowered:
            return region
    for region in table.values():
        if lowered in [a.lower() for a in region.aliases]:
            return region
    return None


def is_generic(spec: str | None) -> bool:
    return bool(spec) and spec.strip().lower() in GENERIC_NAMES


def table_text(table: dict[str, Region], seen_codes: set[str] | None = None) -> str:
    lines = [f"{'code':<6}{'numeric':<12}{'name':<26}aliases"]
    for code in sorted(table):
        region = table[code]
        mark = " *" if seen_codes and code in seen_codes else ""
        lines.append(
            f"{region.code:<6}{(region.numeric or '-'):<12}{region.name:<26}"
            f"{', '.join(region.aliases)}{mark}"
        )
    if seen_codes:
        lines.append("")
        lines.append("* = variant present in the given EDL input")
    return "\n".join(lines)


def code_for_tag(table: dict[str, Region], tag: str) -> str | None:
    for region in table.values():
        if region.matches(tag):
            return region.code
    return None
