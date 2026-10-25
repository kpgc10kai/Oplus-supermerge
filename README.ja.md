[English](README.md) | 日本語

# Oplus-supermerge — OPPO/BBK 向け super.img マージツール

OTA の `payload.bin` に入っているパーティションと、EDL（firehose）フラッシュ用の
パーティションイメージを突き合わせて、そのまま書き込める `super.img` を組み立てるツールです。
`lpmake` / `lpdump` / `lpunpack` が無い Windows 環境でも動くよう、依存は Python 3.10+ の標準ライブラリだけです
（`REPLACE_ZSTD` を使う payload のみ `zstandard` が必要）。

```
./
├── supermerge.py          CLI
├── smlib/
│   ├── payload.py         payload.bin（update_engine）パーサ / 抽出
│   ├── lp.py              liblp メタデータの読み書き（super.img）
│   ├── superimg.py        super.img の読み出しとビルダ
│   ├── edl.py             EDL フォルダ / rawprogram*.xml のスキャン
│   ├── ofp.py             OPPO の .ofp 展開パッケージ (super_def / super_meta)
│   ├── regions.py         仕向け地テーブルとバリアント選択
│   ├── regions.json       仕向け地テーブル本体（編集可）
│   ├── sparse.py          Android sparse image の展開 / 生成
│   ├── protowire.py       最小 protobuf デコーダ（.proto 不要）
│   ├── bspatch.py         BSDIFF40 パッチ適用（差分 OTA 用）
│   └── util.py
├── tests/selftest.py      合成 payload / super による自己テスト
└── extras/                補助ツール（EROFS / UEFI FV / AVB 記述子）
```

## できること

* `payload.bin` からパーティションを展開（フル OTA: `REPLACE` / `REPLACE_BZ` /
  `REPLACE_XZ` / `REPLACE_ZSTD` / `ZERO` / `DISCARD`、差分 OTA: `SOURCE_COPY` /
  `MOVE` / `BSDIFF` / `SOURCE_BSDIFF`）
* **OPPO の `.ofp` 展開パッケージを直接読む**：`META/super_def.<nv>.json` と
  `IMAGES/super_meta.<nv>.raw` から NV 別のレイアウトを復元し、`IMAGES/` の
  イメージを詰めて 1 個の `super.img` にする（`--region IN` などで仕向け地を選択）
* EDL フォルダの自動判定（`rawprogram*.xml` の `label`、素の `<name>.img`、
  sparse 形式、`super.img` 自体）
* 既存 `super.img` をレイアウトの雛形として読み、グループ・属性・アラインメント・
  ブロックデバイスサイズ・メタデータスロット数を引き継いで再構築
* A/B のスロット処理（現行スロットのみ／両スロット／スロット無し）
* 出力の検証（liblp の `ParseGeometry` / `ParseMetadata` と同じ検査＋内容ハッシュ照合）
* sparse 出力（EDL 書き込み用）

## 使い方

```powershell
# 1) まず中身を確認する（同梱されている仕向け地も表示される）
python supermerge.py list --payload payload.bin --edl edl_dir --list-regions
python supermerge.py info --super edl_dir/super.img

# 2) マージ（インド向け。EDL の super.img を雛形にし、OTA の新しいパーティションで上書き）
python supermerge.py build `
    --payload payload.bin `
    --edl edl_dir `
    --region IN `
    --template edl_dir/super.img `
    --add-extra --sparse --verify

# 3) 検証（メタデータ＋payload / EDL との内容一致）
python supermerge.py verify `
    --super super_IN.img --payload payload.bin --edl edl_dir --region IN
```

`--edl` に `super.img` が入っていれば `--template` は省略できます。
`--payload` に OTA の `.zip` を渡すと中の `payload.bin` を自動で取り出します。

## OPPO の .ofp 展開パッケージから作る

`.ofp` を展開したフォルダ（`version_info.txt`, `META/`, `IMAGES/` があるもの）は
そのまま入力にできます。

```powershell
# どの仕向け地が入っているか見る
python supermerge.py list --edl <展開先>

# インド向け super を作る（19GiB のイメージなので sparse 推奨）
python supermerge.py build --edl <展開先> --region IN --sparse
python supermerge.py verify --super super_IN.img --edl <展開先> --region IN
```

パッケージの中身はこうなっています。

| ファイル | 用途 |
|---|---|
| `version_info.txt` | NV ごとの `nv_id` / `product_name`（例 `CPH2841IN`） |
| `META/super_def.<nv>.json` | NV ごとのレイアウト（グループ、パーティション、`path`、サイズ） |
| `IMAGES/super_meta.<nv>.raw` | NV ごとの liblp メタデータ（geometry + メタデータ） |
| `IMAGES/my_*/*.img` | 仕向け地別のイメージ（`my_preload.<id>.img` など） |

* 仕向け地は `MY_MANIFEST/build.<nv>.prop` の `ro.oplus.image.my_region.type`
  （`IN_all` / `MY_all` …）から読み取ります。`product_name` の接尾辞（`CPH2841IN`）
  は IN / JP のように型番が分かれる場合だけ付くため、補助的にしか使いません。
  `--nv 00011011` で直接指定も可。

このパッケージ（CPH2841 16.0.6.103 EX01）の NV は次の 11 種類です。

| nv_id | region | my_region.type | my_region | my_preload |
|---|---|---|---|---|
| 00011010 | TW | `TW_all` | 3.4 MiB | 430 MiB |
| 00011011 | IN | `IN_all` | 10 MiB | 324 MiB |
| 00101100 | SG | `SG_all` | 779 MiB | 717 MiB |
| 00110011 | ID | `ID_all` | 44 MiB | 985 MiB |
| 00111000 | MY | `MY_all` | 252 MiB | 721 MiB |
| 00111001 | TH | `TH_all` | 5.6 MiB | 530 MiB |
| 00111011 | JP | `JP_all` | 5.6 MiB | 588 MiB |
| 00111100 | VN | `VN_all` | 5.3 MiB | 723 MiB |
| 01111011 | MX | `MX_all` | 3.4 MiB | 696 MiB |
| 10100100 | APC | `APC_all` | 3.4 MiB | 183 MiB |
| 00000000 | – | （共通ベース） | – | – |

**共通なのは 17 パーティション中 14 個**（system, system_ext, vendor, product,
my_product, odm, my_engineering, vendor_dlkm, system_dlkm, my_stock, my_heytap,
my_carrier, my_company, my_bigball）で、**仕向け地ごとに違うのは
`my_region` / `my_preload` / `my_manifest` の 3 つ**です。この 3 つが地域の
プリインアプリ・言語・既定設定を決めるため、super は仕向け地ごとに作り分ける
必要があります。
* `super_def` の `path` をそのまま使うので、`my_region.36865860.img` のような
  ビルド ID 付きファイル名でも取り違えません。
* パーティションサイズは `super_def` の宣言値を使い、実ファイルが短い分は
  ゼロ埋めします（この例では `system_dlkm` が 12,192 バイト短い）。
* スロット B は OPPO のメタデータどおり「サイズ 0・extent なし」で出力します
  （`--other-slot copy` を明示すれば A の内容を複製）。
* レイアウトは OPPO の `super_meta` と一致することを確認済みです
  （CPH2841 の IN で 34/34 パーティションの名前・グループ・サイズ・開始セクタが一致）。
* このパッケージで実際に `super_IN.img`（11.73 GiB の sparse）を生成し、
  `verify` で全パーティションの内容一致まで確認しています。

## OTA の payload.bin とマージする

EDL パッケージのレイアウト・仕向け地を保ったまま、新しい OTA のパーティションを
差し替えられます。

```powershell
# 計画だけ確認（payload は展開せずマニフェストのサイズで計算するので速い）
python supermerge.py build `
    --edl <パッケージ> --region IN `
    --payload <OTA の payload.bin か .zip> `
    --dry-run

# 実行
python supermerge.py build `
    --edl <パッケージ> --region IN `
    --payload <OTA の payload.bin か .zip> `
    --sparse --out super_IN_ota.img
```

* `--payload` には `payload.bin` でも OTA の `.zip` でも渡せます（zip の場合は
  中の `payload.bin` を `--workdir` に取り出します）。
* **payload が優先**されます。payload に入っていないパーティション（OPPO の OTA は
  `my_preload` や `my_company` を更新しないことが多い）はパッケージから補います。
* スロット B は OPPO の流儀どおり空のまま、`my_region` などの仕向け地パーティションは
  payload に含まれていれば payload 側が使われます。OTA が別地域のもので
  `--region` と食い違う場合は `--keep-region-partitions` を付けると
  `my_region` / `my_preload` / `my_manifest` をパッケージ側に固定できます。
* マージ開始時に両者のビルドを表示します（例：パッケージ `CPH2841_11.A.33…`
  android 16 / payload `V17.0.0 CPH2841_11.C.10…` android 17）。
* `--prefer edl` で逆（パッケージ優先）にもできます。

実際の例（CPH2841 / IN）:

```text
$ python supermerge.py build --edl <パッケージ> --region IN --payload payload.bin --sparse
package build: CPH2841_11.A.33_0330_202604020155…  (android 16)
payload build: V17.0.0  CPH2841_11.C.10_1100_202610181148  android 17  patch 2026-10-01
sources: empty=17, ofp=2, payload=15

partition               group                           size        offset  source
system_a                qti_dynamic_partitions_a  775.77 MiB  0x100000      payload
my_company_a            qti_dynamic_partitions_a  328.00 KiB  0x30c800000   ofp
my_preload_a            qti_dynamic_partitions_a  309.41 MiB  0x30c900000   ofp
my_manifest_a           qti_dynamic_partitions_a  540.00 KiB  0x320000000   payload
...
system_b                qti_dynamic_partitions_b         0 B  0x0           empty
```

この組み合わせでは 15 個が OTA、`my_preload` と `my_company` の 2 個が
パッケージ（OTA が更新しないため）、スロット B の 17 個が空、という構成になりました。

## 仕向け地（リージョン）の選択

1 つの EDL パッケージに複数の仕向け地向け `my_preload` / `my_stock` /
`my_carrier` などが入っている場合、`--region` で 1 つ選びます。

```powershell
--region IN          # 国コード
--region 00011011    # NV に入っている値（分かっている場合）
--region india       # 名前・別名
--region generic     # 仕向け地なし（無印のファイル）を使う
```

* ファイル名の末尾トークン（`my_preload_IN.img`）でも、フォルダ名
  （`edl/IN/my_preload.img`）でも認識します。
* 同じパーティションに複数の候補があり `--region` が無い場合は、
  取り違えを防ぐためエラーにして候補を列挙します。
* 仕向け地を持たないパーティション（`system` など）は、そのまま 1 個の
  イメージが使われます。
* `--region` を指定して `--out` を省略すると `super_<CODE>.img` になります。
* `--variant my_preload=C:\path\my_preload_IN.img` で個別に固定できます
  （テーブルに無い仕向け地や特殊な構成用）。

### リージョンテーブル

`./smlib/regions.json` に「コード / NV 値 / 名前 / 別名」を
持っています。IN の NV 値 `00011011` は確認済みで、それ以外の `numeric` は
未確認のため空にしてあります（分かったものを埋めていけば `--region` に
NV 値をそのまま渡せるようになります）。

```json
{"code": "IN", "name": "India", "numeric": "00011011", "aliases": ["india"]}
```

* `--regions-file my_regions.json` で追加・上書きできます。
* `--edl` のフォルダに `regions.json` を置いておくと自動で読みます。
* `list --list-regions` で一覧と、そのパッケージに入っている仕向け地（`*`）を表示します。

### 主なオプション（build）

| オプション | 意味 |
|---|---|
| `--region CODE` | 仕向け地の選択（コード / NV 値 / 名前 / `generic`） |
| `--nv ID` | `.ofp` パッケージ内の NV バリアントを直接指定（例 `00011011`） |
| `--ofp DIR` | `.ofp` 展開フォルダ（`--edl` からも自動判定） |
| `--regions-file JSON` | リージョンテーブルの追加・上書き |
| `--variant NAME=PATH` | パーティション個別指定（繰り返し可） |
| `--template super.img` | レイアウトの雛形。省略時は `--edl` 内の super.img を自動使用 |
| `--slot a\|b\|both\|none\|auto` | どちらのスロットへ書くか（既定 auto: 雛形から判定） |
| `--other-slot copy\|drop` | 非アクティブスロットの扱い（既定 copy: 同じ内容を複製） |
| `--prefer payload\|edl` | 同じパーティションが両方にある場合の優先順位（既定 payload） |
| `--add-extra` | 雛形に無いパーティションを末尾に追加 |
| `--drop-missing` | データが用意できないパーティションを落とす（既定はエラー） |
| `--no-template-data` | 雛形 super.img 内のデータを流用しない |
| `--super-size 6G\|auto` | 出力サイズ。`auto` は雛形のサイズを維持 |
| `--grow` | 収まらない場合に雛形サイズを超えて拡張する |
| `--strict-groups` | グループ最大サイズを広げずエラーにする |
| `--sparse` | Android sparse 形式で出力 |
| `--dry-run` | 配置計画だけ表示して書き込まない |
| `--jobs N` | payload 抽出の並列度 |

### データの優先順位

あるパーティション（例 `system_a`）について、上から順に探します。

1. `--variant` の明示指定
2. payload.bin の同名パーティション（スロット接尾辞は外して照合）
3. `.ofp` パッケージの `super_def` が指すイメージ（`--edl` がパッケージのとき）
4. `--edl` のイメージ（`rawprogram*.xml` の `label` → ファイル名 → ステム名）。
   複数の仕向け地版がある場合は `--region` で 1 つに絞る
5. `--template` の super.img 内のデータ（`--no-template-data` で無効化）

どこにも無ければエラー（`--drop-missing` で除外）。

## 出力レイアウト

liblp（`system/core/fs_mgr/liblp`）の仕様どおりに書きます。

| オフセット | サイズ | 内容 |
|---|---|---|
| `0x0000` | 4096 | 予約（`LP_PARTITION_RESERVED_BYTES`） |
| `0x1000` | 4096 | `LpMetadataGeometry`（構造体は 52 バイト、残りはゼロ） |
| `0x2000` | 4096 | ジオメトリのバックアップ |
| `0x3000` | `slot_count × metadata_max_size` | メタデータ（プライマリ、全スロット同一内容） |
| 続き | `slot_count × metadata_max_size` | メタデータのバックアップ |
| 以降 | — | パーティションデータ（`alignment` に整列） |

* データ開始位置は `max(整列したメタデータ末尾, first_logical_sector × 512)`。
  雛形が `alignment=1MiB`, `first_logical_sector=2048` ならデータは 1MiB から始まります。
* 各パーティションは 1 個の `LINEAR` extent。開始位置は `alignment`（通常 1MiB）に
  整列し、サイズは 4096 バイト単位に切り上げます。OPPO の `super_meta` と同じ詰め方です。
* グループ最大サイズは雛形の値を維持し、足りない場合だけ切り上げて拡張（`--strict-groups` で禁止）。
* メタデータはプライマリ・バックアップの全コピーに同一内容を書きます。

`supermerge info` の表示は `lpdump` 相当（バージョン、スロット数、グループ、
extent 先頭オフセット、属性）です。

## OPPO / BBK 特有の扱い

* OPPO/OnePlus/realme の super には `system`, `vendor`, `product`, `odm`,
  `system_ext`, `vendor_dlkm`, `odm_dlkm`, `system_dlkm`, `my_product`,
  `my_heytap`, `my_stock`, `my_carrier`, `my_region`, `my_preload` などが入ります。
  グループ名は雛形のものをそのまま使うので、`qti_dynamic_partitions` などの
  ベンダ固有グループも維持されます。
* payload 側のパーティション名にはスロット接尾辞が付きません（`system`）。
  雛形側の `system_a` / `system_b` と接尾辞を外して対応付けます。
* `LP_PARTITION_ATTR_SLOT_SUFFIXED`（retrofit 端末の super_empty.img 形式）が
  付いている場合は、読み込み時に `_a` / `_b` の実パーティションへ展開します
  （liblp が実行時にやっていることと同じ）。
* `.ozip`（暗号化コンテナ）と `.ofp`（OPPO 独自パッケージ）は直接扱いません。
  先に展開して RAW イメージにしてください。検出した場合はその旨を表示します。

## 注意点

* 出力サイズは原則として雛形 `super.img` のブロックデバイスサイズを維持します。
  データが収まらない場合はエラーになるので、`--grow` か `--super-size` を指定してください。
  実機の `super` パーティションより大きいイメージは書き込めません。
* 差分 OTA（`SOURCE_*` / `BSDIFF`）を展開するには `--old-image-dir` に旧イメージ
  （`<partition>.img`）が必要です。`PUFFDIFF` / `ZUCCHINI` / `LZ4DIFF` は未対応で、
  その場合はフル OTA を使ってください。
* 書き込みは `fastboot flash super super_merged.img`、または EDL の
  `rawprogram` に super を追加して firehose で流します。sparse 出力は EDL 向けです。

## テスト

```powershell
python ./tests/selftest.py
```

合成した `payload.bin`（フル／差分）、`super.img`、sparse イメージ、bsdiff パッチ、
仕向け地バリアント、`.ofp` 展開パッケージ相当のフォルダを生成し、
抽出・再構築・検証まで通します。現在 112 項目すべてパスします。

## extras

`extras/` に、ツール作成時に一緒に書いた小さな解析ツールを置いています。

| ファイル | 用途 |
|---|---|
| `erofs_extract.py` | EROFS の読み出し（flat + LZ4 compact）。テキスト一括ダンプ可 |
| `lz4_block.py` | raw LZ4 ブロックの展開 |
| `uefi_fv.py` | UEFI ファームウェアボリュームの走査（FFS / セクション / LZMA） |
| `avb_descriptors.py` | vbmeta の記述子一覧（chain / hash / hashtree） |

## ライセンス

MIT
