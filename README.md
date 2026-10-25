# Oplus-supermerge

Build a flashable `super.img` for OPPO / OnePlus / realme (BBK) devices by merging an
OTA `payload.bin` with EDL flash files or an extracted OPPO `.ofp` package.

No `lpmake`, `lpdump` or `lpunpack` required — the liblp metadata is read and written
directly, in pure Python (3.9+, standard library only).

```
$ supermerge build --edl CPH2841export_16.0.6.103 --region IN \
      --payload payload.bin --sparse --out super_IN.img
package variant: 00011011 / IN / CPH2841IN
package build: CPH2841_11.A.33_0330_202604020155…  (android 16)
payload build: V17.0.0  CPH2841_11.C.10_1100_202610181148  android 17
sources: empty=17, ofp=2, payload=15
wrote super_IN.img (12.49 GiB)
```

## What it does

* **payload.bin** (full OTA): `REPLACE`, `REPLACE_BZ`, `REPLACE_XZ`, `REPLACE_ZSTD`,
  `ZERO`, `DISCARD`; delta ops `SOURCE_COPY` / `MOVE` / `BSDIFF` / `SOURCE_BSDIFF`
  with `--old-image-dir`.
* **EDL folders**: `rawprogram*.xml` (`label` → file), loose `<name>.img`, sparse
  images, or a `super.img` used as the layout template.
* **Extracted OPPO `.ofp` packages**: `META/super_def.<nv>.json` + `IMAGES/super_meta.<nv>.raw`
  give the per-NV layout, `IMAGES/` gives the partition data.
* **Region selection**: `--region IN` (or `--nv 00011011`) picks the right per-region
  `my_region` / `my_preload` / `my_manifest` variants out of a multi-region package.
* **Merging**: payload wins by default; partitions the OTA does not carry (OPPO often
  skips `my_preload`) fall back to the package, and `--keep-region-partitions` pins the
  region-defining partitions to the package.
* **Streaming sparse output**, so a 19 GiB super does not need a 19 GiB temp file.
* **Verification**: liblp conformance checks (the same rules `liblp` applies) plus
  per-partition content hashes against every available source.

## Install

```bash
git clone https://github.com/kpgc10kai/Oplus-supermerge.git
cd Oplus-supermerge
python -m pip install -e .        # optional: gives you the `supermerge` command
```

Or just run it in place:

```bash
python supermerge.py --help
```

Only `REPLACE_ZSTD` payloads need an extra package (`pip install zstandard`).

## Quick start

```bash
# 1. see what you have
supermerge list --payload payload.bin --edl edl_dir
supermerge list --edl ofp_package --list-regions

# 2. build (EDL package = layout + region, OTA = newer partitions)
supermerge build --edl ofp_package --region IN --payload payload.bin \
    --sparse --verify --out super_IN.img

# 3. check the result
supermerge verify --super super_IN.img --edl ofp_package --region IN --payload payload.bin
```

`--edl` may be a folder with `rawprogram*.xml`, a folder of loose `.img` files, an
extracted `.ofp` package, or a single `super.img`.
`--payload` accepts a `payload.bin` or an OTA `.zip` (the inner `payload.bin` is
extracted to `--workdir`).

## Region selection

One OPPO package usually carries several sales-region variants of the regional
partitions. The region is read from `MY_MANIFEST/build.<nv>.prop`
(`ro.oplus.image.my_region.type=IN_all`) and, as a fallback, from the `product_name`
suffix (`CPH2841IN`).

```bash
supermerge build --edl package --region IN        # country code
supermerge build --edl package --region 00011011  # NV value (from regions.json)
supermerge build --edl package --nv 00111011      # NV id directly
supermerge build --edl package --region generic   # the common base variant
```

Without a region the build refuses and lists the variants:

```
error: this package contains 11 NV variants; pick one before building:
    nv_id     region  product
    --------  ------  ---------
    00000000  (base)  -
    00011010  TW      CPH2841
    00011011  IN      CPH2841IN
    ...
```

The table lives in `smlib/regions.json` (code / NV value / name / aliases) and can be
extended with `--regions-file`, or by dropping a `regions.json` into the EDL folder.

## Merge priority

For each partition of the layout:

1. `--variant NAME=PATH` (manual pin)
2. OTA payload (`--payload`)
3. `.ofp` package `super_def` path (or `--keep-region-partitions` first)
4. EDL folder images
5. template `super.img` data (`--no-template-data` to disable)

Slots: payload partitions are unsuffixed (`system`), the layout uses `system_a` /
`system_b`; the active slot gets the data and the other slot is either mirrored
(`--other-slot copy`, the default) or left empty (`--other-slot empty`, what OPPO's
own metadata does).

## Output layout

Written exactly like liblp expects (`system/core/fs_mgr/liblp`):

| offset | size | content |
|---|---|---|
| `0x0000` | 4096 | reserved |
| `0x1000` | 4096 | `LpMetadataGeometry` (52-byte struct) |
| `0x2000` | 4096 | geometry backup |
| `0x3000` | `slots × metadata_max_size` | primary metadata copies |
| … | `slots × metadata_max_size` | backup metadata copies |
| aligned | — | partition data |

* partition starts are aligned to the block device `alignment` (1 MiB on BBK devices)
* extents are 512-byte sectors; sizes rounded to the 4096-byte block size
* OPPO's slot-B partitions are emitted with zero extents, like the stock metadata

## Verification

`supermerge verify` re-parses the image and checks:

* liblp conformance (header size vs version, table bounds, attribute mask,
  `first_logical_sector` vs metadata region, all metadata copies identical)
* every partition against every source it can find (payload hash or package file)
* group maximum sizes and extent overlap

## Tests

```bash
python tests/selftest.py     # 112 assertions, synthetic payload/super/sparse fixtures
```

## Extras

`extras/` holds the small analyzers that were written alongside the tool:

| file | purpose |
|---|---|
| `erofs_extract.py` | read-only EROFS extractor (flat + LZ4 compact), dumps a text report |
| `lz4_block.py` | raw LZ4 block decompression |
| `uefi_fv.py` | UEFI firmware volume walker (FFS/sections, LZMA GUID sections) |
| `avb_descriptors.py` | list AVB vbmeta descriptors (chain/hash/hashtree) |

## Limitations

* `PUFFDIFF` / `ZUCCHINI` / `LZ4DIFF` payload operations are not implemented — use a
  full OTA for those devices.
* `.ozip` (encrypted) and `.ofp` (proprietary container) must be extracted first.
* The output keeps the template's super size; use `--super-size` or `--grow` when the
  merged data does not fit (and remember the device's `super` partition limit).
* Cross-version merges (newer OTA into an older layout) work, but the result is only
  as consistent as you make it — check `verify` and the boot result.

## License

MIT
