#!/usr/bin/env python3
"""supermerge - build a super.img for BBK (OPPO / OnePlus / realme) devices
by merging partitions from an OTA payload.bin with an EDL flash folder.

Typical use:

    # inspect what you have
    python supermerge.py list --payload payload.bin --edl edl_dir

    # merge: EDL super.img supplies the layout + the partitions the OTA does
    # not carry; payload.bin supplies the (newer) dynamic partitions
    python supermerge.py build --payload payload.bin \
        --edl edl_dir --template edl_dir/super.img \
        --out super_merged.img --sparse

    # verify the result end to end
    python supermerge.py verify --super super_merged.img
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from smlib import edl as edlmod
from smlib import lp
from smlib import ofp as ofpmod
from smlib import regions as regionsmod
from smlib.models import model_from_payload_properties, models_compatible
from smlib.payload import Payload, describe_version, open_payload, read_properties
from smlib.superimg import Entry, LayoutTemplate, SuperBuilder, SuperImage
from smlib.util import (Console, Fail, human_size, parse_size,
                        setup_stdout, sha256_file, split_slot)


# --------------------------------------------------------------------- helpers

REGION_PARTS = {"my_region", "my_preload", "my_manifest"}


def parse_list(text: str | None) -> list[str] | None:
    if not text:
        return None
    items = [t.strip() for t in text.replace(";", ",").split(",")]
    return [t for t in items if t]


def parse_variants(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise Fail("--variant expects NAME=PATH")
        name, path = item.split("=", 1)
        if not name.strip() or not path.strip():
            raise Fail(f"--variant is malformed: {item!r}")
        if not os.path.exists(path.strip()):
            raise Fail(f"--variant {name.strip()}: no such file {path.strip()}")
        out[name.strip()] = path.strip()
    return out


def load_region_table(args, log: Console):
    extra: list[str] = []
    if getattr(args, "regions_file", None):
        extra.append(args.regions_file)
    else:
        for raw in getattr(args, "edl", None) or []:
            if os.path.isdir(raw):
                sidecar = os.path.join(raw, "regions.json")
                if os.path.exists(sidecar):
                    extra.append(sidecar)
                    log.info(f"using region table {sidecar}")
    return regionsmod.load_table(extra)


def resolve_region(args, table, log: Console):
    """Return (Region|None, generic_flag)."""
    spec = getattr(args, "region", None)
    if not spec:
        return None, False
    if regionsmod.is_generic(spec):
        log.info("region: generic (untagged variants only)")
        return None, True
    region = regionsmod.find(table, spec)
    if region is None:
        raise Fail(
            f"unknown region '{spec}'; available codes: "
            + ", ".join(sorted(table))
            + "  (see 'list --list-regions')"
        )
    log.info(f"region: {region.label()}")
    return region, False


def load_template(args, sources: edlmod.SourceSet, log: Console,
                  region=None, generic: bool = False) -> SuperImage | None:
    path = args.template
    if path:
        if not os.path.exists(path):
            raise Fail(f"no such template: {path}")
        log.info(f"using {path} as layout template")
    elif sources.super_images:
        path = edlmod.pick_super(sources, region, generic, log)
        log.info(f"using {path} as layout template (found in EDL input)")
    if not path:
        return None
    return SuperImage(path, log=log)


def payload_summary(payload: Payload) -> str:
    lines = []
    lines.append(f"{'partition':<24}{'size':>12}  {'old':>12}  ops")
    for p in payload.partitions:
        ops = {}
        for op in p.ops:
            ops[op.name] = ops.get(op.name, 0) + 1
        optxt = ", ".join(f"{k}x{v}" for k, v in sorted(ops.items()))
        old = human_size(p.old_size) if p.old_size else "-"
        lines.append(f"{p.name:<24}{human_size(p.size):>12}  {old:>12}  {optxt}")
        if p.filesystem_type:
            lines[-1] += f"  [{p.filesystem_type}]"
    return "\n".join(lines)


def confirm_overwrite(path: str, force: bool) -> None:
    if os.path.exists(path) and not force:
        raise Fail(f"{path} already exists (use --force to overwrite)")


# ------------------------------------------------------------------- commands

def cmd_list(args) -> int:
    log = Console(args.quiet, args.verbose)
    rc = 0
    if args.payload:
        payload = open_payload(args.payload, os.path.join(args.workdir, "payload"))
        print(f"# payload: {payload.path}")
        print(f"# format v{payload.version}, block size {payload.block_size}, "
              f"{len(payload.partitions)} partition(s), "
              f"data at 0x{payload.data_offset_base:x}")
        if payload.dynamic_groups:
            print("# dynamic partitions:")
            for name, size, members in payload.dynamic_groups:
                print(f"#   {name}: max {human_size(size) if size else 'unset'}"
                      f" ({', '.join(members)})")
        print(payload_summary(payload))
    table = load_region_table(args, log)
    sources = None
    pkg = None
    if getattr(args, "ofp", None):
        pkg = ofpmod.OfpPackage(args.ofp)
        print(pkg.summary())
    elif args.edl:
        pkg = ofpmod.find_package(args.edl)
        if pkg is not None:
            print(pkg.summary())
        else:
            sources = edlmod.scan(args.edl, log, table)
            print(f"# edl sources: {len(sources)} image(s)")
            print(edlmod.describe_sources(sources, table))
            if sources.super_images:
                print("# super images found:")
                for p in sources.super_images:
                    print(f"#   {p}")
            tags = edlmod.region_tags(sources, table)
            if tags:
                print(f"# region variants in this package: {', '.join(sorted(tags))}")
    if getattr(args, "list_regions", False):
        seen = edlmod.region_tags(sources, table) if sources else set()
        print(regionsmod.table_text(table, seen))
    if args.super:
        with SuperImage(args.super, log=log, tmp_dir=args.workdir) as img:
            print(lp.describe(img.meta, args.super))
            problems = img.check()
            if problems:
                print("\n# problems:")
                for p in problems:
                    print(f"#   {p}")
                rc = 2
    ota_model = model_from_payload_properties(payload.properties) if args.payload else None
    package_model = pkg.package_model() if pkg is not None else None
    if ota_model:
        print(f"# payload model: {ota_model}")
    if package_model:
        print(f"# package model: {package_model}")
    if not models_compatible(ota_model, package_model):
        print(f"# warning: model mismatch ({ota_model} vs {package_model}); "
              "use the OTA and the EDL/package for the same device")
        rc = rc or 2
    return rc


def cmd_info(args) -> int:
    with SuperImage(args.super, log=Console(args.quiet, args.verbose),
                    tmp_dir=args.workdir) as img:
        print(lp.describe(img.meta, args.super))
        print()
        print(f"file size: {human_size(img.size)}")
        first = min((img.meta.extents_of(p)[0].target_data for p in img.meta.partitions
                     if img.meta.extents_of(p)), default=0)
        print(f"metadata copies: 0x{img.geometry.metadata_start:x}"
              f"..0x{img.geometry.metadata_end:x} "
              f"({human_size(img.geometry.metadata_end - img.geometry.metadata_start)})")
        print(f"first partition data: 0x{first * 512:x}")
        problems = img.check()
        if problems:
            print("\nproblems:")
            for p in problems:
                print(f"  - {p}")
            return 2
        print("layout: ok")
    return 0


def cmd_extract(args) -> int:
    log = Console(args.quiet, args.verbose)
    payload = open_payload(args.payload, os.path.join(args.workdir, "payload"))
    names = parse_list(args.partitions)
    files = payload.extract_all(
        args.out, names=names, old_dir=args.old_image_dir, jobs=args.jobs,
        verify_ops=not args.no_verify, verify_hash=args.verify_hash, log=log,
    )
    for name, path in sorted(files.items()):
        size = os.path.getsize(path)
        if args.sparse:
            from smlib import sparse
            enc = path
            sparse.encode(path, enc + ".tmp")
            os.replace(enc + ".tmp", enc)
        print(f"{name:<24}{human_size(size):>12}  {path}")
    if args.verify_hash:
        log.info("partition hashes match the payload manifest")
    return 0


def cmd_unpack(args) -> int:
    log = Console(args.quiet, args.verbose)
    names = parse_list(args.partitions)
    with SuperImage(args.super, log=log, tmp_dir=args.workdir) as img:
        img.extract_all(args.out, names=names, make_sparse=args.sparse, log=log)
    print(f"extracted {len(names) if names else len(img.meta.partitions)} "
          f"partition(s) to {args.out}")
    return 0


def cmd_build(args) -> int:
    log = Console(args.quiet, args.verbose)
    work = os.path.abspath(args.workdir)
    os.makedirs(work, exist_ok=True)

    # ---------------------------------------------------------- gather input
    table = load_region_table(args, log)
    pkg = ofpmod.OfpPackage(args.ofp) if getattr(args, "ofp", None) \
        else ofpmod.find_package(args.edl)
    variant = None
    if pkg is not None:
        spec = args.region
        generic_flag = bool(spec and regionsmod.is_generic(spec))
        if spec and not generic_flag:
            hit = regionsmod.find(table, spec)
            spec = hit.code if hit else spec
        variant = pkg.variant(args.nv) if args.nv else pkg.resolve(region=spec,
                                                                   generic=generic_flag)
        log.info(f"package variant: {variant.label()}")
        for warning in pkg.warnings:
            log.warn(warning)
        log.info(f"package build: {pkg.package_version()}  "
                 f"(android {pkg.android_version()})")
        region, generic = None, False
    else:
        region, generic = resolve_region(args, table, log)

    code = variant.region if variant is not None and variant.region else (
        region.code if region is not None else None)
    default_out = f"super_{code}.img" if code else "super_merged.img"
    out = os.path.abspath(args.out or default_out)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    confirm_overwrite(out, args.force)
    overrides = parse_variants(args.variant)

    if pkg is not None:
        template = LayoutTemplate(pkg.metadata(variant),
                                  sources=pkg.sources(variant),
                                  label=f"ofp:{variant.nv_id}")
        log.info(f"layout: {template.label}, "
                 f"{len(template.meta.partitions)} partition(s), "
                 f"{human_size(template.block_device.size)}")
        extra = [p for p in (args.edl or []) if os.path.abspath(p) != pkg.root]
        sources = edlmod.scan(extra, log, table) if extra else edlmod.SourceSet()
    else:
        sources = edlmod.scan(args.edl, log, table) if args.edl else edlmod.SourceSet()
        template = load_template(args, sources, log, region, generic)
    other_slot = args.other_slot or (
        "empty" if isinstance(template, LayoutTemplate) else "copy")
    payload = None
    if args.payload:
        payload = open_payload(args.payload, os.path.join(work, "payload"))
        log.info(f"payload: {len(payload.partitions)} partition(s), "
                 f"format v{payload.version}")
        if payload.properties:
            log.info(f"payload build: {describe_version(payload.properties)}")
    if payload is not None and pkg is not None and not args.keep_region_partitions:
        if any(split_slot(p.name)[0] == "my_region" for p in payload.partitions):
            log.warn("the payload also carries my_region, and the payload copy wins; "
                     "add --keep-region-partitions if that OTA is meant for a "
                     "different sales region")
    if payload is None and len(sources) == 0 and template is None:
        raise Fail("nothing to build from: pass --payload and/or --edl")
    if template is None and not args.from_scratch:
        raise Fail(
            "no super layout to work from: pass --template super.img "
            "(or point --edl at a folder containing one), "
            "or use --from-scratch with --group. See 'supermerge layout'."
        )

    ota_model = model_from_payload_properties(payload.properties) if payload else None
    package_model = pkg.package_model() if pkg is not None else None
    if ota_model:
        log.info(f"payload model: {ota_model}")
    if package_model:
        log.info(f"package model: {package_model}")
    if not models_compatible(ota_model, package_model):
        log.warn(
            f"model mismatch: the OTA targets {ota_model} but the package is "
            f"{package_model}. The merged super is unlikely to boot; use the OTA "
            "and the EDL/package built for the same device."
        )

    # -------------------------------------------------- which payload parts
    template_bases: set[str] = set()
    if template is not None:
        for part in template.meta.partitions:
            template_bases.add(split_slot(part.name)[0])
    dynamic_names: set[str] = set()
    if payload is not None:
        for _grp, _size, members in payload.dynamic_groups:
            for member in members:
                dynamic_names.add(split_slot(member)[0])
    super_parts = [p for p in (payload.partitions if payload else [])
                   if not dynamic_names or split_slot(p.name)[0] in dynamic_names]
    wanted = None
    if payload is not None:
        if template is not None and not args.add_extra:
            wanted = [p.name for p in super_parts
                      if split_slot(p.name)[0] in template_bases]
            skipped = [p.name for p in super_parts if p.name not in wanted]
            if skipped:
                log.warn("payload partition(s) not present in the layout are ignored: "
                         + ", ".join(skipped) + " (use --add-extra to include them)")
        else:
            wanted = [p.name for p in super_parts]
        others = len(payload.partitions) - len(super_parts)
        if others:
            log.debug(f"{others} non-dynamic payload partition(s) (boot, modem, ...) "
                      "are not part of super and were skipped")
    payload_files: dict[str, str] = {}
    payload_sizes: dict[str, int] = {}
    if payload is not None:
        if args.dry_run:
            payload_sizes = {p.name: p.size for p in payload.partitions}
            log.info("dry run: payload data is not extracted (sizes from the manifest)")
        else:
            payload_files = payload.extract_all(
                os.path.join(work, "payload_parts"), names=wanted,
                old_dir=args.old_image_dir, jobs=args.jobs, log=log,
            )

    # ---------------------------------------------------------- build plan
    slot_mode = "auto"
    if args.slot in ("a", "b"):
        slot_mode, slot_choice = "current", args.slot
    elif args.slot == "both":
        slot_mode, slot_choice = "both", "a"
    elif args.slot == "none":
        slot_mode, slot_choice = "none", None
    else:
        suffixed = [p for p in (template.meta.partitions if template else [])
                    if split_slot(p.name)[1]]
        if suffixed:
            slots = {split_slot(p.name)[1] for p in suffixed}
            slot_choice = "a" if "a" in slots else "b"
            slot_mode = "current"
        else:
            slot_choice = None

    if template is not None:
        builder = SuperBuilder(template.meta)
        log.info(f"layout: {len(template.meta.partitions)} partition(s), "
                 f"{len(builder.groups)} group(s), slot mode {slot_mode}"
                 + (f" ({slot_choice})" if slot_choice else ""))
    else:
        builder = SuperBuilder(None)

    ofp_files = getattr(template, "sources", None) or {}

    def resolve_source(base: str, exact: str) -> tuple[str, str | None, int] | None:
        manual = overrides.get(base) or overrides.get(exact)
        if manual:
            return "manual", manual, os.path.getsize(manual)
        if args.keep_region_partitions and base in REGION_PARTS:
            got = ofp_files.get(base) or ofp_files.get(exact)
            if got:
                return "ofp", got, os.path.getsize(got)
        order = ["payload", "ofp", "edl"]
        if args.prefer == "edl":
            order = ["edl", "ofp", "payload"]
        for kind in order:
            if kind == "payload":
                got = payload_files.get(base) or payload_files.get(exact)
                if got:
                    return "payload", got, os.path.getsize(got)
                if payload_sizes:
                    for key in (exact, base):
                        if key in payload_sizes:
                            return "payload", None, payload_sizes[key]
            elif kind == "ofp":
                got = ofp_files.get(base) or ofp_files.get(exact)
                if got:
                    return "ofp", got, os.path.getsize(got)
            else:
                src = edlmod.pick_variant(sources, base, exact, region, generic, log, table)
                if src:
                    return "edl", src.path, os.path.getsize(src.path)
        return None

    missing: list[str] = []
    placed: set[str] = set()

    if template is not None:
        for part in template.meta.partitions:
            base, part_slot = split_slot(part.name)
            size = template.meta.partition_size(part)
            attrs = part.attributes
            group = template.meta.group_for(part).name
            if size == 0 and part.num_extents == 0:
                # The layout declares this partition empty (OPPO ships slot B so).
                builder.add(Entry(part.name, group, attrs, 0, origin="empty"))
                continue
            if part_slot and slot_mode == "none":
                log.warn(f"{part.name}: slot-suffixed partition in a non-slot layout; "
                         "keeping the name as-is")
            if slot_mode == "current" and part_slot and part_slot != slot_choice:
                if other_slot == "drop":
                    log.debug(f"dropping inactive-slot partition {part.name}")
                    continue
                if other_slot == "empty":
                    builder.add(Entry(part.name, group, attrs, 0, origin="empty"))
                    continue
                resolved = resolve_source(base, base)
                if resolved is None:
                    missing.append(part.name)
                    continue
                origin, path, found = resolved
                builder.add(Entry(part.name, group, attrs, found, path=path,
                                  origin=origin + "/other-slot"))
                placed.add(base)
                continue
            resolved = resolve_source(base, part.name)
            if resolved is not None:
                origin, path, found = resolved
                if origin == "ofp" and size > 0:
                    # The package declares the partition size; the image file is
                    # usually a little smaller and gets zero padded.
                    if found > size:
                        log.warn(f"{part.name}: image is larger than declared "
                                 f"({human_size(found)} > {human_size(size)})")
                        size = found
                else:
                    size = found
                builder.add(Entry(part.name, group, attrs, size, path=path, origin=origin))
                placed.add(base)
                continue
            if args.use_template_data and template.has_data and size > 0:
                exts = template.meta.extents_of(part)
                builder.add(Entry(part.name, group, attrs, size, origin="template",
                                  template_extents=exts,
                                  template_path=template.raw_path))
                placed.add(base)
                continue
            missing.append(part.name)

    # payload partitions that the layout does not know about
    if template is None or args.add_extra:
        for name, path in payload_files.items():
            base = split_slot(name)[0]
            if base in placed or name in placed:
                continue
            group = args.group or "default"
            builder.add(Entry(name, group, lp.ATTR_READONLY,
                              os.path.getsize(path), path=path, origin="payload/new"))
            log.info(f"adding new partition {name} (group {group})")

    # EDL images that the layout does not know about
    if (template is None or args.add_extra) and args.edl:
        groups: dict[str, list] = {}
        for src in sources.all_sources():
            base = edlmod.guess_base(src.name, table)
            groups.setdefault(base.lower(), []).append(src)
        for base_key, cands in groups.items():
            if base_key in placed:
                continue
            base = edlmod.guess_base(cands[0].name, table)
            pairs = [(edlmod.variant_tag(c.name, base), c) for c in cands]
            try:
                src = edlmod.select_variant(base, pairs, region, generic, log)
            except Fail as exc:
                log.warn(f"skipping extra partition {base}: {exc}")
                continue
            if src is None:
                continue
            group = args.group or "default"
            builder.add(Entry(src.name, group, lp.ATTR_READONLY,
                              os.path.getsize(src.path), path=src.path, origin="edl/new"))
            log.info(f"adding new partition {src.name} (group {group})")
            placed.add(base_key)

    if missing:
        listing = "\n  ".join(missing)
        if args.drop_missing:
            log.warn(f"dropping {len(missing)} partition(s) with no data source:\n  {listing}")
        else:
            raise Fail(
                f"{len(missing)} partition(s) in the layout have no data source:\n  {listing}\n"
                "provide them via --payload / --edl, or pass --drop-missing"
            )

    if not builder.entries:
        raise Fail("no partitions selected; nothing to build")

    # ------------------------------------------------------------- sizing
    total_size = None
    if args.super_size and args.super_size != "auto":
        total_size = parse_size(args.super_size)
        log.info(f"target super size: {human_size(total_size)}")
    elif template is not None and template.has_data and not args.grow:
        total_size = template.block_device.size
    plan = builder.plan(total_size=total_size,
                        auto_grow_groups=not args.strict_groups,
                        alignment=None)
    for note in builder.notes:
        log.warn(note)
    template_size = template.block_device.size if template else 0
    if template_size and plan["total_size"] > template_size:
        log.warn(
            f"image grew from {human_size(template_size)} to "
            f"{human_size(plan['total_size'])}; it will not fit the device's "
            "super partition unless that partition is resized"
        )

    log.info(f"layout: data 0x{plan['data_start']:x}, "
             f"end 0x{plan['end']:x}, total {human_size(plan['total_size'])}, "
             f"{len(plan['placements'])} partition(s)")
    counts: dict[str, int] = {}
    for entry in builder.entries:
        counts[entry.origin] = counts.get(entry.origin, 0) + 1
    log.info("sources: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.dry_run:
        print(f"{'partition':<24}{'group':<24}{'size':>12}  {'offset':>12}  source")
        for entry, offset, size in plan["placements"]:
            print(f"{entry.name:<24}{entry.group:<24}{human_size(size):>12}  "
                  f"0x{offset:<10x}  {entry.origin}")
        print(f"\n(total {human_size(plan['total_size'])}, dry run - nothing written)")
        return 0

    # ------------------------------------------------------------- write
    def progress(i: int, n: int, entry) -> None:
        log.info(f"[{i}/{n}] {entry.name}")

    if args.sparse:
        log.info("writing Android sparse image")
        builder.write_sparse(out, plan, log=log, progress=progress)
    else:
        builder.write(out, plan, log=log, progress=progress)
    log.info(f"wrote {out} ({human_size(os.path.getsize(out))})")

    # ------------------------------------------------------------- verify
    if args.verify:
        log.info("verifying output")
        problems = verify_image(out, log, work)
        if problems:
            for p in problems:
                log.warn(p)
            return 2
    return 0


def verify_image(path: str, log: Console, tmp_dir: str | None = None) -> list[str]:
    with SuperImage(path, log=log, tmp_dir=tmp_dir) as img:
        problems = img.strict_check()
        if not problems:
            log.info(f"  metadata ok, {len(img.meta.partitions)} partition(s), "
                     f"{human_size(img.size)}")
        return problems


def cmd_verify(args) -> int:
    log = Console(args.quiet, args.verbose)
    problems = []
    pkg = ofpmod.OfpPackage(args.ofp) if getattr(args, "ofp", None) \
        else ofpmod.find_package(args.edl)
    ofp_sources: dict[str, str] = {}
    if pkg is not None:
        table = load_region_table(args, log)
        spec = args.region
        generic_flag = bool(spec and regionsmod.is_generic(spec))
        if spec and not generic_flag:
            hit = regionsmod.find(table, spec)
            spec = hit.code if hit else spec
        variant = pkg.variant(args.nv) if args.nv else pkg.resolve(
            region=spec, generic=generic_flag)
        log.info(f"package variant: {variant.label()}")
        ofp_sources = pkg.sources(variant)
    with SuperImage(args.super, log=log, tmp_dir=args.workdir) as img:
        problems.extend(img.strict_check())
        payload = open_payload(args.payload, os.path.join(args.workdir, "payload")) \
            if args.payload else None
        edl_sources = None
        edl_region, edl_generic, edl_table = None, False, None
        if args.edl and pkg is None:
            edl_table = load_region_table(args, log)
            edl_region, edl_generic = resolve_region(args, edl_table, log)
            edl_sources = edlmod.scan(args.edl, log, edl_table)

        for part in img.meta.partitions:
            part_size = img.meta.partition_size(part)
            if part_size == 0:
                continue
            base = split_slot(part.name)[0]
            candidates: list[tuple[str, int, str | None, str | None]] = []
            if payload is not None:
                pp = payload.find(base) or payload.find(part.name)
                if pp and pp.hash_hex:
                    candidates.append((f"payload:{pp.name}", pp.size, pp.hash_hex, None))
            path = ofp_sources.get(base) or ofp_sources.get(part.name)
            if path:
                candidates.append((os.path.basename(path), os.path.getsize(path), None, path))
            if edl_sources is not None:
                src = edlmod.pick_variant(edl_sources, base, part.name, edl_region,
                                          edl_generic, log, edl_table)
                if src:
                    candidates.append((os.path.basename(src.path),
                                       os.path.getsize(src.path), None, src.path))
            if not candidates:
                continue
            if not _partition_matches(img, part, part_size, candidates):
                labels = ", ".join(c[0] for c in candidates)
                problems.append(f"{part.name}: content matches none of: {labels}")
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 2
    print(f"{args.super}: verified ok")
    return 0


def _partition_matches(img: SuperImage, part, part_size: int,
                       candidates: list[tuple[str, int, str | None, str | None]]) -> bool:
    """True if the partition matches any of the given sources."""
    for _label, size, hexdigest, path in candidates:
        if size > part_size:
            continue
        if size == part_size:
            digest = _hash_partition(img, part.name)
        else:
            digest, got = _hash_partition_prefix(img, part.name, size)
            if got != size:
                continue
        if hexdigest is None:
            hexdigest = sha256_file(path)
        if digest == hexdigest:
            return True
    return False


def _hash_partition(img: SuperImage, name: str) -> str:
    import hashlib
    h = hashlib.sha256()
    for buf in img.iter_partition(name):
        h.update(buf)
    return h.hexdigest()


def _hash_partition_prefix(img: SuperImage, name: str, limit: int):
    """Hash the first *limit* bytes of a partition; returns (hexdigest, read)."""
    import hashlib
    h = hashlib.sha256()
    total = 0
    for buf in img.iter_partition(name):
        take = min(len(buf), limit - total)
        h.update(buf[:take])
        total += take
        if total >= limit:
            break
    return h.hexdigest(), total


# ----------------------------------------------------------------------- main

def _add_global_options(p: argparse.ArgumentParser, sub: bool = False) -> None:
    """Global flags; on sub-parsers they default to SUPPRESS so that a value
    given before the sub-command is not overwritten."""
    default: object = argparse.SUPPRESS if sub else False
    workdir_default: object = argparse.SUPPRESS if sub else "supermerge_work"
    p.add_argument("-v", "--verbose", action="store_true", default=default,
                   help="more detail on stderr")
    p.add_argument("-q", "--quiet", action="store_true", default=default,
                   help="only errors and machine output")
    p.add_argument("--workdir", default=workdir_default,
                   help="scratch directory for extracted images (default: supermerge_work)")


LAYOUT_TEXT = """\
Two different trees
===================

`supermerge layout` describes the INPUTS, not the repository you cloned. The two
trees have nothing in common, and that is expected.

1) The tool (this repository — what you cloned)

  Oplus-supermerge/
  +-- supermerge.py         CLI entry point
  +-- smlib/                library (payload, liblp, ofp, edl, sparse, regions)
  +-- tests/selftest.py     self-test
  +-- extras/               EROFS / LZ4 / UEFI FV / AVB helpers
  +-- pyproject.toml        optional `pip install -e .`
  +-- README.md
  +-- LICENSE

  Running `pip install -e .` additionally creates *.egg-info/ in the repository;
  that is a build artifact and is git-ignored.

2) The inputs (firmware data — create this anywhere, e.g. D:\\work)

  work/
  +-- edl/                  EDL input, one of:
  |   +-- <extracted .ofp>  version_info.txt + META/ + IMAGES/   (layout + region)
  |   +-- <edl folder>      rawprogram*.xml + *.img, loose *.img, or super.img
  +-- ota/                  payload.bin, or the OTA .zip (inner payload.bin is used)
  +-- out/                  super.img is written here

Any OPPO / OnePlus / realme (Oplus) device works as long as the inputs follow the
same on-disk formats — nothing is hard-coded to one model. The EDL package and the
OTA must be built for the same device; a mismatch is reported as a warning.

Example
-------

  python supermerge.py build --edl work/edl --region IN \\
      --payload work/ota/payload.bin --sparse --out work/out/super_IN.img

  python supermerge.py wizard      # asks for the paths instead of a folder layout

Check both sides first with:

  python supermerge.py list --edl work/edl --payload work/ota/payload.bin
"""


def cmd_layout(args) -> int:
    print(LAYOUT_TEXT, end="")
    return 0


def _prompt(label: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{hint}: ").strip()
    except EOFError:
        raise Fail("input closed") from None
    return value or default


def _peek_ota(path: str) -> dict[str, str]:
    """payload_properties.txt of a payload.bin or an OTA .zip, without extracting it."""
    if path.lower().endswith(".zip"):
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                data = zf.read("payload_properties.txt")
        except (KeyError, OSError):
            return {}
        props: dict[str, str] = {}
        for line in data.decode("utf-8", "replace").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                props[key.strip()] = value.strip()
        return props
    return read_properties(os.path.join(os.path.dirname(path), "payload_properties.txt"))


def cmd_wizard(args) -> int:
    if not sys.stdin.isatty():
        raise Fail("'wizard' needs an interactive terminal; pass --edl and --payload "
                   "to 'build' instead")
    print("supermerge wizard — press Enter to accept the default\n")

    edl = ""
    while not edl:
        edl = _prompt("EDL package, folder or .zip")
        if not edl:
            print("  required: an extracted .ofp package, a folder with "
                  "rawprogram*.xml + images, or a .zip of either")
        elif not os.path.exists(edl):
            print(f"  not found: {edl}")
            edl = ""

    pkg = ofpmod.find_package(edl)
    regions: list[str] = []
    if pkg is not None:
        try:
            codes = sorted({v.region for v in pkg.variants if v.region})
            regions = codes
            print(f"  package: {pkg.package_model() or '?'}, "
                  f"android {pkg.android_version()}, {len(pkg.variants)} variant(s)"
                  + (f": {', '.join(codes)}" if codes else ""))
        except Exception as exc:  # packaging oddities should not stop the wizard
            print(f"  package: could not read the variant table ({exc})")
    else:
        print("  plain EDL folder (no .ofp package): the model cannot be checked")

    ota = ""
    while True:
        ota = _prompt("OTA payload.bin or .zip (blank to skip)")
        if not ota:
            break
        if not os.path.exists(ota):
            print(f"  not found: {ota}")
            continue
        props = _peek_ota(ota)
        print(f"  payload: {describe_version(props) if props else 'version unknown'}")
        break

    region = ""
    if len(regions) > 1:
        while True:
            region = _prompt(f"Region ({'/'.join(regions)})").upper()
            if not region or region in regions:
                break
            print(f"  pick one of: {', '.join(regions)}")
    elif regions:
        region = regions[0]
        print(f"  region: {region}")

    out = _prompt("Output file",
                  f"super_{region}.img" if region else "super_merged.img")
    sparse = _prompt("Write a sparse image? (y/n)", "y").lower().startswith("y")

    argv = ["build", "--edl", edl, "--out", out]
    if ota:
        argv += ["--payload", ota]
    if region:
        argv += ["--region", region]
    if sparse:
        argv += ["--sparse"]
    if getattr(args, "workdir", None):
        argv += ["--workdir", args.workdir]
    if args.verbose:
        argv.append("-v")
    print()
    return main(argv)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="supermerge",
        description="Merge OTA payload.bin partitions with EDL flash files into a super.img",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    _add_global_options(p)

    sub = p.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--payload", help="payload.bin or OTA .zip containing it")
    common.add_argument("--edl", nargs="+", metavar="PATH",
                        help="EDL folder(s)/image(s): rawprogram*.xml plus images, or loose .img")
    common.add_argument("--region", metavar="CODE",
                        help="sales region used to pick regional variants: table code (IN), "
                             "NV value (00011011), name/alias, or 'generic'")
    common.add_argument("--nv", metavar="ID",
                        help="NV variant id inside an extracted OPPO package (e.g. 00011011)")
    common.add_argument("--ofp", metavar="DIR",
                        help="extracted OPPO .ofp package (auto-detected from --edl)")
    common.add_argument("--regions-file", metavar="JSON",
                        help="region table to merge over the built-in regions.json")

    def sub_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, **kwargs)
        _add_global_options(sp, sub=True)
        return sp

    # layout
    sp = sub_parser("layout", help="print the recommended input folder layout")
    sp.set_defaults(func=cmd_layout)

    # wizard
    sp = sub_parser("wizard", help="ask for the EDL and OTA paths, then build")
    sp.add_argument("--sparse", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_wizard)

    # list
    sp = sub_parser("list", parents=[common], help="show payload / EDL / super contents")
    sp.add_argument("--super", help="super.img to describe")
    sp.add_argument("--list-regions", action="store_true",
                    help="print the region table (marks codes found in --edl)")
    sp.set_defaults(func=cmd_list)

    # info
    sp = sub_parser("info", help="describe a super.img layout")
    sp.add_argument("--super", required=True)
    sp.set_defaults(func=cmd_info)

    # extract
    sp = sub_parser("extract", help="extract partitions from payload.bin")
    sp.add_argument("--payload", required=True)
    sp.add_argument("-o", "--out", required=True, help="output directory")
    sp.add_argument("--partitions", help="comma separated subset")
    sp.add_argument("--old-image-dir", help="previous images for delta payloads")
    sp.add_argument("--jobs", type=int, default=1)
    sp.add_argument("--sparse", action="store_true", help="write Android sparse images")
    sp.add_argument("--verify-hash", action="store_true",
                    help="check each partition against the manifest hash")
    sp.add_argument("--no-verify", action="store_true", help="skip per-operation hash checks")
    sp.set_defaults(func=cmd_extract)

    # unpack
    sp = sub_parser("unpack", help="extract partitions from a super.img")
    sp.add_argument("--super", required=True)
    sp.add_argument("-o", "--out", required=True)
    sp.add_argument("--partitions", help="comma separated subset")
    sp.add_argument("--sparse", action="store_true")
    sp.set_defaults(func=cmd_unpack)

    # build
    sp = sub_parser("build", parents=[common], help="merge sources into a new super.img")
    sp.add_argument("--template", help="super.img to take the layout from "
                                      "(auto-detected in --edl if omitted)")
    sp.add_argument("-o", "--out", "--output", dest="out", default=None,
                    help="output image (default: super_<REGION>.img, or super_merged.img)")
    sp.add_argument("--slot", choices=["a", "b", "both", "none", "auto"], default="auto",
                    help="which slot to populate (default: auto-detect from the template)")
    sp.add_argument("--other-slot", choices=["copy", "drop", "empty"], default=None,
                    help="what to do with the other slot's partitions "
                         "(default: copy, or empty for OPPO packages)")
    sp.add_argument("--prefer", choices=["payload", "edl"], default="payload",
                    help="which source wins when both provide a partition")
    sp.add_argument("--add-extra", action="store_true",
                    help="append source partitions that the layout does not contain")
    sp.add_argument("--drop-missing", action="store_true",
                    help="omit layout partitions that have no data source")
    sp.add_argument("--no-template-data", dest="use_template_data", action="store_false",
                    help="do not reuse partition data from the template super.img")
    sp.add_argument("--strict-groups", action="store_true",
                    help="fail instead of growing group maximum sizes")
    sp.add_argument("--from-scratch", action="store_true",
                    help="build metadata without a template (needs --group)")
    sp.add_argument("--group", default="default", help="group for new partitions")
    sp.add_argument("--variant", action="append", metavar="NAME=PATH",
                    help="force one image for a partition, overriding region "
                         "selection (repeatable)")
    sp.add_argument("--keep-region-partitions", action="store_true",
                    help="take my_region/my_preload/my_manifest from the package "
                         "even when the payload also carries them")
    sp.add_argument("--super-size", default="auto",
                    help="total image size, e.g. 6G; 'auto' keeps the template size")
    sp.add_argument("--grow", action="store_true",
                    help="allow the image to grow past the template size when needed")
    sp.add_argument("--sparse", action="store_true", help="write an Android sparse image")
    sp.add_argument("--force", action="store_true", help="overwrite an existing output file")
    sp.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    sp.add_argument("--jobs", type=int, default=1, help="parallel payload extraction")
    sp.add_argument("--old-image-dir", help="previous images for delta payloads")
    sp.add_argument("--verify", action="store_true", help="check the output after writing")
    sp.set_defaults(func=cmd_build)

    # verify
    sp = sub_parser("verify", parents=[common], help="check a super.img")
    sp.add_argument("--super", required=True)
    sp.set_defaults(func=cmd_verify)
    return p


def main(argv: list[str] | None = None) -> int:
    setup_stdout()
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "edl", None):
            args.edl = edlmod.materialize_inputs(
                args.edl, args.workdir, Console(args.quiet, args.verbose))
        return args.func(args) or 0
    except Fail as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort reporting
        if os.environ.get("SUPERMERGE_DEBUG"):
            traceback.print_exc()
        print(f"error: unexpected failure: {exc}", file=sys.stderr)
        print("set SUPERMERGE_DEBUG=1 for a traceback", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
