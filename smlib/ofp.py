"""Extracted OPPO/BBK ``.ofp`` package support.

A package that the OPPO flash tool consumes looks like::

    version_info.txt                 JSON array: one entry per NV variant
    META/super_def.<nv>.json         per-NV super layout (partitions, groups, paths)
    META/super_map.in.csv            NV -> component build ids
    IMAGES/super_meta.<nv>.raw       per-NV liblp metadata blob (geometry + metadata)
    IMAGES/*.img                     partition images (my_* ones per region)

Each ``nv_id`` is the value the device reports from NV; the packaging tool picks
the matching variant at flash time.  Here the variant is chosen up front with
``--region`` / ``--nv``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from . import lp
from .regions import Region
from .util import Fail, human_size, split_slot

DEFAULT_META_MAX_SIZE = 65536
DEFAULT_SLOT_COUNT = 3


@dataclass
class Variant:
    nv_id: str
    product_name: str = ""
    product_model: str = ""
    market_name: str = ""
    version_name: str = ""
    region: str | None = None

    def label(self) -> str:
        bits = [self.nv_id]
        if self.region:
            bits.append(self.region)
        if self.product_name:
            bits.append(self.product_name)
        return " / ".join(bits)


def looks_like_package(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    if os.path.exists(os.path.join(path, "version_info.txt")):
        return True
    meta = os.path.join(path, "META")
    if os.path.isdir(meta):
        for name in os.listdir(meta):
            if name.startswith("super_def.") and name.endswith(".json"):
                return True
    return False


def find_package(paths: list[str] | str | None) -> "OfpPackage | None":
    if isinstance(paths, str):          # a bare path is a single candidate
        paths = [paths]
    for raw in paths or []:
        if looks_like_package(raw):
            return OfpPackage(raw)
    return None


class OfpPackage:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        if not os.path.isdir(self.root):
            raise Fail(f"not a directory: {root}")
        self.variants = self._read_variants()
        if not self.variants:
            raise Fail(f"{root}: no variants found in version_info.txt")
        self.warnings: list[str] = []

    # ------------------------------------------------------------- variants
    @staticmethod
    def _split_region(product_name: str, product_model: str) -> str | None:
        if product_name and product_model and product_name.upper().startswith(product_model.upper()):
            suffix = product_name[len(product_model):].strip("_- ")
            if suffix:
                return suffix.upper()
        return None

    def _read_variants(self) -> list[Variant]:
        path = os.path.join(self.root, "version_info.txt")
        variants: list[Variant] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as exc:
                    raise Fail(f"{path}: invalid JSON ({exc})")
            if isinstance(data, dict):
                data = [data]
            for item in data:
                nv = str(item.get("nv_id") or item.get("download_type", "")).strip()
                if nv.lower().startswith("nv"):
                    nv = nv[2:]
                if not nv:
                    continue
                model = str(item.get("product_model") or "").strip()
                product = str(item.get("product_name") or "").strip()
                variants.append(Variant(
                    nv_id=nv,
                    product_name=product,
                    product_model=model,
                    market_name=str(item.get("market_name") or "").strip(),
                    version_name=str(item.get("version_name") or "").strip(),
                    region=self._split_region(product, model),
                ))
        found = set(v.nv_id for v in variants)
        meta_dir = os.path.join(self.root, "META")
        if os.path.isdir(meta_dir):
            for name in sorted(os.listdir(meta_dir)):
                if name.startswith("super_def.") and name.endswith(".json"):
                    nv = name[len("super_def."):-len(".json")]
                    if nv not in found:
                        variants.append(Variant(nv_id=nv))
                        found.add(nv)
        # The build prop names the region for variants whose model is shared
        # (only IN and JP get their own product_name).
        for variant in variants:
            from_prop = self._region_from_prop(variant.nv_id)
            if from_prop:
                variant.region = from_prop
        return variants

    def _region_from_prop(self, nv_id: str) -> str | None:
        path = os.path.join(self.root, "MY_MANIFEST", f"build.{nv_id}.prop")
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("ro.oplus.image.my_region.type="):
                        value = line.split("=", 1)[1].strip()
                        if value.endswith("_all"):
                            value = value[:-4]
                        return value.upper() or None
        except OSError:
            return None
        return None

    def variant(self, nv_id: str) -> Variant:
        for v in self.variants:
            if v.nv_id == nv_id:
                return v
        raise Fail(f"{self.root}: no NV variant {nv_id}; have "
                   + ", ".join(v.nv_id for v in self.variants))

    def variant_table(self) -> str:
        rows = [("nv_id", "region", "product")]
        for v in sorted(self.variants, key=lambda x: x.nv_id):
            region = v.region or ("(base)" if v.nv_id == "00000000" else "-")
            rows.append((v.nv_id, region, v.product_name or "-"))
        widths = [max(len(r[i]) for r in rows) for i in range(3)]
        out = ["  ".join(rows[0][i].ljust(widths[i]) for i in range(3)).rstrip()]
        out.append("  ".join("-" * widths[i] for i in range(3)))
        for r in rows[1:]:
            out.append("  ".join(r[i].ljust(widths[i]) for i in range(3)).rstrip())
        return "\n".join("    " + line for line in out)

    def resolve(self, nv: str | None = None, region: str | None = None,
                generic: bool = False) -> Variant:
        if nv:
            nv = nv.strip()
            if nv.lower().startswith("nv"):
                nv = nv[2:]
            return self.variant(nv)
        if generic:
            for v in self.variants:
                if v.nv_id == "00000000":
                    return v
            for v in self.variants:
                if not v.region:
                    return v
            raise Fail("package has no generic variant; pass --nv")
        if region:
            code = region.strip().upper()
            direct = [v for v in self.variants if v.nv_id == region.strip()]
            if len(direct) == 1:
                return direct[0]
            hits = [v for v in self.variants if v.region and v.region.upper() == code]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise Fail(f"region {code} matches several variants:\n"
                           + self.variant_table())
            raise Fail(
                f"this package has no {code} build. Available variants:\n"
                + self.variant_table()
                + "\n\n  choose one with --region <code> or --nv <nv_id>"
            )
        if len(self.variants) == 1:
            return self.variants[0]
        raise Fail(
            f"this package contains {len(self.variants)} NV variants; "
            "pick one before building:\n"
            + self.variant_table()
            + "\n\n  --region IN        build for India\n"
              "  --nv 00011011      build for a specific NV id\n"
              "  --region generic   the common base image (nv 00000000)"
        )

    # ----------------------------------------------------------- super defs
    def super_def(self, nv_id: str) -> dict:
        path = os.path.join(self.root, "META", f"super_def.{nv_id}.json")
        if not os.path.exists(path):
            raise Fail(f"{path}: missing super definition")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def metadata(self, variant: Variant) -> lp.Metadata:
        """Layout for *variant*: the shipped metadata blob, or one built from
        super_def when the blob is absent."""
        blob_path = os.path.join(self.root, "IMAGES", f"super_meta.{variant.nv_id}.raw")
        if os.path.exists(blob_path):
            return self._metadata_from_blob(blob_path)
        self.warnings.append(
            f"super_meta.{variant.nv_id}.raw missing; synthesising metadata "
            "from super_def"
        )
        return self._metadata_from_def(self.super_def(variant.nv_id), variant)

    def _metadata_from_blob(self, path: str) -> lp.Metadata:
        with open(path, "rb") as f:
            blob = f.read()
        if len(blob) < 4096 + 128:
            raise Fail(f"{path}: too small to contain super metadata")
        geom = _geometry_from_bytes(blob[:4096], path)
        return lp.Metadata.from_bytes(geom, blob[4096:])

    def _metadata_from_def(self, defn: dict, variant: Variant) -> lp.Metadata:
        devices = defn.get("block_devices") or [{}]
        bd_def = devices[0]
        total = int(defn.get("super_device", {}).get("total_size")
                    or bd_def.get("size") or 0)
        block_size = int(bd_def.get("block_size") or 4096)
        geom = lp.Geometry(DEFAULT_META_MAX_SIZE, DEFAULT_SLOT_COUNT, block_size)
        bd = lp.BlockDevice(
            first_logical_sector=2048,
            alignment=int(bd_def.get("alignment") or (1 << 20)),
            alignment_offset=0,
            size=total,
            partition_name=str(bd_def.get("name") or "super"),
            flags=0,
        )
        groups: list[lp.Group] = []
        for grp in defn.get("groups", []):
            groups.append(lp.Group(str(grp.get("name") or "default"), 0,
                                   int(grp.get("maximum_size") or 0)))
        if not groups:
            groups.append(lp.Group("default"))
        partitions: list[lp.Partition] = []
        extents: list[lp.Extent] = []
        block = geom.logical_block_size
        for part in defn.get("partitions", []):
            gname = str(part.get("group_name") or "default")
            gi = next((i for i, g in enumerate(groups) if g.name == gname), 0)
            size = int(part.get("size") or 0)
            # Represent the declared size as a zero extent so callers can use
            # partition_size(); the builder replaces the extents anyway.
            count = 1 if size > 0 else 0
            first = len(extents)
            if size > 0:
                extents.append(lp.Extent((size + lp.SECTOR_SIZE - 1) // lp.SECTOR_SIZE,
                                         lp.EXTENT_ZERO, 0, 0))
            partitions.append(lp.Partition(str(part.get("name")), lp.ATTR_READONLY,
                                           first, count, gi))
        return lp.Metadata(geometry=geom, partitions=partitions, extents=extents,
                           groups=groups, block_devices=[bd],
                           minor_version=2, header_size=256)

    # -------------------------------------------------------------- sources
    def sources(self, variant: Variant) -> dict[str, str]:
        defn = self.super_def(variant.nv_id)
        out: dict[str, str] = {}
        for part in defn.get("partitions", []):
            rel = part.get("path")
            if not rel:
                continue
            full = os.path.join(self.root, str(rel).replace("/", os.sep))
            if not os.path.exists(full):
                self.warnings.append(f"super_def references missing image {rel}")
                continue
            name = str(part.get("name") or "")
            base = split_slot(name)[0]
            out.setdefault(base, full)
            out.setdefault(name, full)
        return out

    # --------------------------------------------------------------- report
    def package_model(self) -> str | None:
        """Device model of the package (``ro.product.model``, else variant data)."""
        props = self._root_props()
        for key in ("ro.product.model", "ro.product.name"):
            value = props.get(key, "").strip()
            if value:
                return value.upper()
        models = {v.product_model.upper() for v in self.variants if v.product_model}
        if len(models) == 1:
            return models.pop()
        return None

    def package_version(self) -> str:
        """Short version string of the package as a whole."""
        props = self._root_props()
        return (props.get("ro.oplus.version.my_manifest")
                or props.get("ro.build.version.incremental")
                or (self.variants[0].version_name if self.variants else "unknown"))

    def android_version(self) -> str:
        return self._root_props().get("ro.build.version.release", "?")

    def _root_props(self) -> dict[str, str]:
        cached = getattr(self, "_props_cache", None)
        if cached is not None:
            return cached
        props: dict[str, str] = {}
        path = os.path.join(self.root, "build.prop")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        props[key.strip()] = value.strip()
            except OSError:
                pass
        self._props_cache = props
        return props

    def summary(self) -> str:
        rows = []
        for v in sorted(self.variants, key=lambda x: x.nv_id):
            blob = os.path.join(self.root, "IMAGES", f"super_meta.{v.nv_id}.raw")
            size = "?"
            try:
                defn = self.super_def(v.nv_id)
                size = human_size(int(defn.get("super_device", {}).get("used_size") or 0))
                if not os.path.exists(blob):
                    size += " *"
            except Fail:
                size = "no super_def"
            rows.append([v.nv_id, v.region or "-", v.product_name or "-",
                         v.version_name or "-", size])
        header = ["nv_id", "region", "product", "build", "used"]
        widths = [max(len(header[i]), *(len(r[i]) for r in rows)) if rows else len(header[i])
                  for i in range(len(header))]
        lines = [f"# OPPO package: {self.root}",
                 "  ".join(header[i].ljust(widths[i]) for i in range(len(header))).rstrip()]
        for r in rows:
            lines.append("  ".join(r[i].ljust(widths[i]) for i in range(len(r))).rstrip())
        lines.append("* = super_meta.<nv>.raw missing, metadata is synthesised "
                     "from super_def")
        return "\n".join(lines)


def _geometry_from_bytes(raw: bytes, path: str) -> lp.Geometry:
    import hashlib
    import struct

    magic, struct_size = struct.unpack_from("<II", raw, 0)
    if magic != lp.GEOMETRY_MAGIC:
        raise Fail(f"{path}: no LP geometry at the start of the file")
    if struct_size < 48 or struct_size > 4096:
        raise Fail(f"{path}: unexpected geometry struct size {struct_size}")
    probe = bytearray(raw[:struct_size])
    probe[8:40] = b"\0" * 32
    if hashlib.sha256(bytes(probe)).digest() != raw[8:40]:
        raise Fail(f"{path}: geometry checksum mismatch")
    max_size, slots, block = struct.unpack_from("<III", raw, 40)
    if not (4096 <= max_size <= (1 << 22)) or not (1 <= slots <= 4) or block < 512:
        raise Fail(f"{path}: implausible geometry values")
    return lp.Geometry(max_size, slots, block)
