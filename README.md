# Oplus-supermerge

Build a flashable `super.img` for OPPO / OnePlus / realme (BBK) devices by merging an
OTA `payload.bin` with EDL flash files or an extracted OPPO `.ofp` package.

No `lpmake`, `lpdump` or `lpunpack` required — the liblp metadata is read and written
directly, in pure Python (3.10+, standard library only).

```
$ python supermerge.py build --edl CPH2841export_16.0.6.103 --region IN \
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
* **Model check**: the device model is read from the OTA (`payload_properties.txt`) and
  from the package (`ro.product.model`); a mismatch between the two is reported.

## Choosing the inputs

The tool is model-agnostic. Supply the EDL package and the OTA built for **your**
device; only the on-disk formats matter, and OPPO / OnePlus / realme share them.

There are two unrelated trees:

```
1) The tool — this repository (what you cloned)

Oplus-supermerge/
├── supermerge.py         CLI entry point
├── smlib/                library (payload, liblp, ofp, edl, sparse, regions)
├── tests/selftest.py     self-test
├── extras/               EROFS / LZ4 / UEFI FV / AVB helpers
├── pyproject.toml        optional `pip install -e .`
├── README.md
└── LICENSE

2) The inputs — firmware data, created anywhere (e.g. D:\work)

work/
├── edl/                  EDL input, one of:
│   ├── <extracted .ofp>  version_info.txt + META/ + IMAGES/   (layout + region)
│   └── <edl folder>      rawprogram*.xml + *.img, loose *.img, or super.img
├── ota/                  payload.bin, or the OTA .zip (the inner payload.bin is used)
└── out/                  super.img is written here
```

`python supermerge.py layout` prints both trees. The `work/` folders are not part of
the repository; create them wherever you keep firmware. Point `--edl` and `--payload`
at those paths; nothing needs to be renamed.

Use the package and the OTA for the same device. If they disagree you get a warning
instead of a silently broken image:

```
warning: model mismatch: the OTA targets PYB110 but the package is CPH2841. The merged
super is unlikely to boot; use the OTA and the EDL/package built for the same device.
```

The model comes from `compatible_ota_prefix_list` / `ota_target_version` in
`payload_properties.txt`, and from `ro.product.model` in the package's `build.prop`.
When one side cannot be determined the check is skipped (no false alarms).

## Install

No installation is required. Clone and run the script in place:

```bash
git clone https://github.com/kpgc10kai/Oplus-supermerge.git
cd Oplus-supermerge
python supermerge.py --help
```

All examples below use `python supermerge.py …` because that always works, with or
without installation.

Optionally install it to get a `supermerge` command on your PATH:

```bash
python -m pip install -e .
python supermerge.py --help
```

If PowerShell says `The term 'supermerge' is not recognized`, or your shell says
`supermerge: command not found`, the package is simply not installed yet — use
`python supermerge.py` instead.

Only `REPLACE_ZSTD` payloads need an extra package (`pip install zstandard`).

## Quick start

```bash
# 0. where to put the inputs (prints both trees)
python supermerge.py layout

# 1. see what you have (also prints both device models)
python supermerge.py list --edl work/edl --payload work/ota/payload.bin
python supermerge.py list --edl work/edl --list-regions

# 2. build (EDL package = layout + region, OTA = newer partitions)
python supermerge.py build --edl work/edl --region IN --payload work/ota/payload.bin \
    --sparse --verify --out work/out/super_IN.img

# 3. check the result
python supermerge.py verify --super work/out/super_IN.img --edl work/edl \
    --region IN --payload work/ota/payload.bin
```

`--edl` may be a folder with `rawprogram*.xml`, a folder of loose `.img` files, an
extracted `.ofp` package, or a single `super.img`.
`--payload` accepts a `payload.bin` or an OTA `.zip` (the inner `payload.bin` is
extracted to `--workdir`).

### Wizard

If you would rather just point at the two packages instead of arranging a folder tree:

```bash
python supermerge.py wizard
```

```
EDL package or folder: D:\ofp\CPH2841export_16.0.6.103
  package: CPH2841, android 16, 11 variant(s): APC, ID, IN, JP, ...
OTA payload.bin or .zip (blank to skip): D:\ota\payload.bin
  payload: V17.0.0  CPH2841_11.C.10_1100_202610181148  android 17
Region (APC/ID/IN/JP/...): IN
Output file [super_IN.img]:
Write a sparse image? (y/n) [y]:
```

The wizard only asks for the two paths (plus region, output, sparse), verifies that
they exist, and then runs the same `build`. The `work/` layout above is just a
suggestion for people who prefer a fixed tree.

## Region selection

One OPPO package usually carries several sales-region variants of the regional
partitions. The region is read from `MY_MANIFEST/build.<nv>.prop`
(`ro.oplus.image.my_region.type=IN_all`) and, as a fallback, from the `product_name`
suffix (`CPH2841IN`).

```bash
python supermerge.py build --edl package --region IN        # country code
python supermerge.py build --edl package --region 00011011  # NV value (from regions.json)
python supermerge.py build --edl package --nv 00111011      # NV id directly
python supermerge.py build --edl package --region generic   # the common base variant
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

`python supermerge.py verify` re-parses the image and checks:

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
