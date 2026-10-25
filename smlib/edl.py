"""Discover partition images coming from an EDL (firehose) flash folder.

Accepted inputs, freely mixed:
  * a directory containing ``rawprogram0.xml`` (and friends) plus the images
  * a directory of loose ``<name>.img`` files
  * individual image files
  * a ``super.img`` (raw or sparse) used both as layout template and as data

A package may hold several variants of the regional partitions
(``my_preload_IN`` / ``my_preload_RU`` ...); those are kept side by side and
resolved with a region table (see :mod:`smlib.regions`).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import lp, sparse
from .util import Console, Fail, human_size

SKIP_LABELS = {"", "gpt", "primarygpt", "backupgpt", "ssd", "persist"}


@dataclass
class Source:
    name: str
    path: str
    origin: str
    sparse: bool = False
    # Region tag taken from the enclosing directory (e.g. edl/IN/my_preload.img)
    tag: str | None = None

    @property
    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return -1


class SourceSet:
    def __init__(self) -> None:
        self.items: dict[str, list[Source]] = {}
        self.super_images: list[str] = []
        self.warnings: list[str] = []

    def add(self, name: str, path: str, origin: str, sparse_flag: bool = False,
            tag: str | None = None) -> None:
        if not name:
            return
        if not os.path.exists(path):
            self.warnings.append(f"{origin} references a missing file: {path}")
            return
        key = name.lower()
        bucket = self.items.setdefault(key, [])
        for existing in bucket:
            if os.path.normcase(os.path.abspath(existing.path)) == \
                    os.path.normcase(os.path.abspath(path)):
                return
        src = Source(name=name, path=path, origin=origin, sparse=sparse_flag, tag=tag)
        # XML-declared entries take precedence over loose files.
        if origin == "xml":
            bucket.insert(0, src)
        else:
            bucket.append(src)

    def candidates(self, name: str) -> list[Source]:
        return list(self.items.get(name.lower(), []))

    def candidates_for(self, base: str, table=None) -> list[Source]:
        """All images that could belong to partition *base*.

        That is the exact name plus names with a trailing tag that matches a
        known region (so ``system_ext`` is never mistaken for a ``system``
        variant).
        """
        base_low = base.lower()
        out: list[Source] = []
        seen: set[str] = set()
        for key, bucket in self.items.items():
            if key == base_low:
                wanted = True
            elif table and key.startswith(base_low) and len(key) > len(base_low) \
                    and key[len(base_low)] in "_-.":
                tag = key[len(base_low) + 1:]
                wanted = any(region.matches(tag) for region in table.values())
            else:
                wanted = False
            if not wanted:
                continue
            for src in bucket:
                path_key = os.path.normcase(os.path.abspath(src.path))
                if path_key in seen:
                    continue
                seen.add(path_key)
                out.append(src)
        return out

    def get(self, name: str) -> Source | None:
        """Return the only candidate for *name*, or None if absent/ambiguous."""
        found = self.candidates(name)
        return found[0] if len(found) == 1 else None

    def all_sources(self) -> list[Source]:
        seen: set[str] = set()
        out: list[Source] = []
        for bucket in self.items.values():
            for src in bucket:
                key = os.path.normcase(os.path.abspath(src.path))
                if key in seen:
                    continue
                seen.add(key)
                out.append(src)
        return out

    def names(self) -> list[str]:
        return sorted(src.name for src in self.all_sources())

    def __len__(self) -> int:
        return len(self.all_sources())


# ------------------------------------------------------------------ variants

def variant_tag(name: str, base: str) -> str | None:
    """Extra suffix of *name* after *base*, excluding A/B slot suffixes.

    ``my_preload_IN`` + ``my_preload`` -> ``IN``
    ``system_a``     + ``system``     -> None   (slot suffix, not a region)
    """
    low_name, low_base = name.lower(), base.lower()
    if low_name == low_base:
        return None
    if not low_name.startswith(low_base):
        return None
    rest = name[len(base):]
    if not rest or rest[0] not in "_-.":
        return None
    rest = rest[1:]
    if not rest:
        return None
    if rest.lower() in ("a", "b") and low_name == f"{low_base}_{rest.lower()}":
        return None
    return rest


def _tag_list(pairs) -> str:
    return ", ".join(t for t, _ in pairs) or "(none)"


def select_variant(base: str, pairs, region, generic: bool, log: Console | None):
    """Pick one (tag, payload) pair for *base*."""
    untagged = [obj for tag, obj in pairs if not tag]
    tagged = [(tag, obj) for tag, obj in pairs if tag]
    if generic:
        if untagged:
            return untagged[0]
        raise Fail(f"{base}: no generic image, only variants: {_tag_list(tagged)}")
    if region is not None:
        if not tagged:
            # This partition simply has no regional variants.
            if untagged:
                return untagged[0]
            raise Fail(f"{base}: no image for region {region.code}")
        hits = [(tag, obj) for tag, obj in tagged if region.matches(tag)]
        if len(hits) == 1:
            if log:
                log.debug(f"{base}: region {region.code} -> {tagged and hits[0][0]}")
            return hits[0][1]
        if len(hits) > 1:
            wanted = {region.code.lower()}
            if region.numeric:
                wanted.add(region.numeric.lower())
            exact = [obj for tag, obj in hits if tag.lower() in wanted]
            if len(exact) == 1:
                return exact[0]
            raise Fail(f"{base}: region {region.code} matches several files: "
                       f"{_tag_list(hits)}")
        raise Fail(f"{base}: no image for region {region.code}; "
                   f"available variants: {_tag_list(tagged) or '(none)'}")
    if untagged and not tagged:
        return untagged[0]
    if not untagged and len(tagged) == 1:
        return tagged[0][1]
    raise Fail(f"{base}: the EDL package contains several variants "
               f"({_tag_list(tagged)}); choose one with --region")


def pick_variant(sources: SourceSet, base: str, exact: str | None,
                 region, generic: bool = False,
                 log: Console | None = None, table=None) -> Source | None:
    pairs = []
    for src in sources.candidates_for(base, table):
        if src.tag:
            tag = src.tag
        elif exact and src.name.lower() == exact.lower():
            tag = None
        else:
            tag = variant_tag(src.name, base)
        pairs.append((tag, src))
    if not pairs:
        return None
    return select_variant(base, pairs, region, generic, log)


def pick_super(sources: SourceSet, region, generic: bool = False,
               log: Console | None = None) -> str | None:
    paths = sources.super_images
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    pairs = []
    for path in paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        pairs.append((variant_tag(stem, "super"), path))
    return select_variant("super", pairs, region, generic, log)


def guess_base(name: str, table) -> str:
    """Strip a trailing region token so ``my_carrier_IN`` maps to ``my_carrier``."""
    tokens = name.replace("-", "_").replace(".", "_").split("_")
    if len(tokens) > 1:
        for region in table.values():
            if region.matches(tokens[-1]):
                return "_".join(tokens[:-1])
    return name


def trailing_region(name: str, table) -> str | None:
    """Region code denoted by the *last* component of a file name.

    Only the last token is considered so that ``my_stock`` is not read as a
    Malaysia (``MY``) variant of ``my_``.
    """
    token = name.replace("-", "_").replace(".", "_").split("_")[-1]
    if not token:
        return None
    for region in table.values():
        if region.matches(token):
            return region.code
    return None


def region_tags(sources: SourceSet, table) -> set[str]:
    """Region codes that appear in this package's file names or folders."""
    found: set[str] = set()
    for src in sources.all_sources():
        code = trailing_region(src.tag or src.name, table)
        if code:
            found.add(code)
    for path in sources.super_images:
        stem = os.path.splitext(os.path.basename(path))[0]
        code = trailing_region(stem, table) or trailing_region(
            os.path.basename(os.path.dirname(path)), table)
        if code:
            found.add(code)
    return found


def describe_sources(sources: SourceSet, table) -> str:
    lines = []
    rows = []
    for src in sorted(sources.all_sources(), key=lambda s: s.name.lower()):
        tag = trailing_region(src.tag or src.name, table) or ""
        rows.append((src.name, tag, src.origin, src.size, src.path))
    width = max((len(r[0]) for r in rows), default=8)
    for name, tag, origin, size, path in rows:
        lines.append(f"{name:<{width}}  {tag:<4}{human_size(size):>12}  {origin:<5}  {path}")
    return "\n".join(lines)


def _iter_files(root: str, max_depth: int = 4):
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def _dir_region_tag(directory: str, table) -> str | None:
    """Region tag implied by a directory name (``.../IN/my_preload.img``)."""
    if not table:
        return None
    name = os.path.basename(os.path.normpath(directory))
    if not name:
        return None
    for region in table.values():
        if region.matches(name):
            return name
    return None


def _parse_rawprogram(xml_path: str, sources: SourceSet, table=None) -> int:
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        sources.warnings.append(f"{xml_path}: cannot parse ({exc})")
        return 0
    base = os.path.dirname(os.path.abspath(xml_path))
    dir_tag = _dir_region_tag(base, table)
    count = 0
    for program in tree.getroot().iter("program"):
        label = (program.get("label") or "").strip()
        filename = (program.get("filename") or "").strip()
        if not filename or filename in ("DISK", "ZERO"):
            continue
        if label.lower() in SKIP_LABELS:
            continue
        path = filename if os.path.isabs(filename) else os.path.join(base, filename)
        if not os.path.exists(path):
            sources.warnings.append(f"{xml_path}: missing image {filename}")
            continue
        is_sparse = (program.get("sparse") or "").lower() in ("true", "1", "yes")
        tag = _dir_region_tag(os.path.dirname(path), table) or dir_tag
        sources.add(label, path, "xml", is_sparse, tag)
        stem = os.path.splitext(os.path.basename(filename))[0]
        if stem != label:
            sources.add(stem, path, "xml", is_sparse, tag)
        count += 1
    return count


def scan(paths: list[str], log: Console | None = None, table=None) -> SourceSet:
    sources = SourceSet()
    files: list[str] = []
    for raw in paths:
        if not os.path.exists(raw):
            raise Fail(f"no such EDL path: {raw}")
        if os.path.isdir(raw):
            files.extend(_iter_files(raw))
        else:
            files.append(os.path.abspath(raw))

    xmls = [f for f in files if os.path.basename(f).lower().startswith("rawprogram")
            and f.lower().endswith(".xml")]
    for xml in xmls:
        n = _parse_rawprogram(xml, sources, table)
        if log:
            log.debug(f"rawprogram {os.path.basename(xml)}: {n} image(s)")

    for path in files:
        base = os.path.basename(path)
        lower = base.lower()
        if lower.startswith("rawprogram") or lower.startswith("patch"):
            continue
        if lower.endswith(".ofp") or lower.endswith(".ozip") or lower.endswith(".ops"):
            sources.warnings.append(
                f"{path}: proprietary OPPO container; extract it to raw images first"
            )
            continue
        if lower.endswith(".xml") or lower.endswith(".txt") or lower.endswith(".log"):
            continue
        if not (lower.endswith(".img") or lower.endswith(".bin")):
            continue
        tag = _dir_region_tag(os.path.dirname(path), table)
        if sparse.is_sparse(path):
            try:
                if _sparse_looks_like_super(path):
                    sources.super_images.append(path)
                    continue
            except Exception:
                pass
            sources.add(os.path.splitext(base)[0], path, "file", True, tag)
            continue
        if lp.is_super_image(path):
            sources.super_images.append(path)
            continue
        stem = os.path.splitext(base)[0]
        sources.add(stem, path, "file", False, tag)

    if log:
        for warning in sources.warnings:
            log.warn(warning)
        log.debug(f"EDL sources: {len(sources)} partition image(s), "
                  f"{len(sources.super_images)} super image(s)")
    return sources


def _sparse_looks_like_super(path: str) -> bool:
    """Decode just enough of a sparse image to probe for LP geometry."""
    import struct
    with open(path, "rb") as f:
        hdr = sparse.read_header(f)
        blk = hdr["blk_sz"]
        need = 4096 + lp.GEOMETRY_SIZE
        buf = bytearray()
        f.seek(hdr["file_hdr_sz"])
        pos = 0
        while pos < need:
            raw = f.read(sparse.CHUNK_SIZE)
            if len(raw) != sparse.CHUNK_SIZE:
                break
            ctype, _r, nblocks, total_sz = struct.unpack(sparse.CHUNK_FMT, raw)
            payload = f.read(total_sz - sparse.CHUNK_SIZE)
            nbytes = nblocks * blk
            if ctype == sparse.CHUNK_RAW:
                buf += payload[:nbytes]
            elif ctype == sparse.CHUNK_FILL:
                buf += (payload[:4] * (nbytes // 4))
            else:
                buf += b"\0" * nbytes
            pos += nbytes
        if len(buf) < need:
            return False
        probe = bytes(buf[4096:4096 + 4])
        return struct.unpack("<I", probe)[0] == lp.GEOMETRY_MAGIC


def materialise(source: Source, work_dir: str) -> str:
    """Return a path to the raw (non-sparse) version of a source image."""
    if not sparse.is_sparse(source.path):
        return source.path
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, os.path.splitext(os.path.basename(source.path))[0] + ".raw")
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        sparse.decode(source.path, out)
    return out
