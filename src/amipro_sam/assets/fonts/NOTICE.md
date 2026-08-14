# Bundled preservation fonts

These fonts are bundled so PDF output has stable, cross-platform Unicode
coverage. They are project resources, not resources recovered from a source
document. The converter never loads a font path named by a document.

The PDF renderer dynamically uses two separately installed libraries:
`python-bidi` 0.6.11 for Unicode bidirectional ordering and `uharfbuzz` 0.55
for OpenType shaping. `python-bidi` is replaceable under the normal Python
import mechanism and is licensed under LGPL-3.0-or-later; its authors, GPL,
LGPL, and third-party notices are included here. `uharfbuzz` is licensed under
Apache-2.0 and its license is included here. Neither library is copied into
this source tree or statically linked into the toolkit.

- <https://github.com/MeirKriheli/python-bidi/tree/v0.6.11>
- <https://github.com/harfbuzz/uharfbuzz/tree/v0.55.0>

## DejaVu Sans 2.37

`DejaVuSans.ttf`, `DejaVuSans-Bold.ttf`, `DejaVuSans-Oblique.ttf`, and
`DejaVuSans-BoldOblique.ttf` retain the glyphs and font tables from the official
DejaVu Fonts 2.37 release. Only their family, style, unique, full, PostScript,
typographic-family, and typographic-style name records are deterministically
changed to `AmiPro Preservation Sans`. This avoids ReportLab global-registry
collisions with a host DejaVu installation. Copyright, version, embedded
license, license URL, and original source timestamps remain intact. Their
license is in `LICENSE-DejaVu.txt`; its renaming requirements are satisfied.

- Release: <https://github.com/dejavu-fonts/dejavu-fonts/releases/tag/version_2_37>
- Source archive: `dejavu-fonts-ttf-2.37.zip`
- Archive SHA-256:
  `7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a`
- Original upstream font SHA-256 values:
  - `DejaVuSans.ttf`:
    `7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954`
  - `DejaVuSans-Bold.ttf`:
    `e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724`
  - `DejaVuSans-Oblique.ttf`:
    `4af75fa16ee6d3ad43e1ecec41862c24954af26a55c6bb1ebb27bd486a50f5f4`
  - `DejaVuSans-BoldOblique.ttf`:
    `eb436dca0c2594b73d8b603b892e374fdfd8d885d25ffb4f18df4c4c0b49e50f`
- Bundled name-only derived SHA-256 values:
  - `DejaVuSans.ttf`:
    `8a301f4fc28b4cadd8668f41c61217e200ffd3e069d2912966b5a2903ab09434`
  - `DejaVuSans-Bold.ttf`:
    `6b4f83ef68e461c05a8d8b218177936226a32f746044cfc10e4b9351c4a9415d`
  - `DejaVuSans-Oblique.ttf`:
    `6c4bf004bd06ad8b16ac3be38627e6cfd7f7da01b6563ddf6d385f227a8f28ac`
  - `DejaVuSans-BoldOblique.ttf`:
    `6d26ecff69d04ad88af75bb046370d6f52d8908a97632cee8cc8682638dc9758`

Reproduce the name-only derivation with fontTools 4.63.0:

```sh
python tools/rename_amipro_dejavu.py regular DejaVuSans.ttf OUT/DejaVuSans.ttf
python tools/rename_amipro_dejavu.py bold DejaVuSans-Bold.ttf OUT/DejaVuSans-Bold.ttf
python tools/rename_amipro_dejavu.py oblique DejaVuSans-Oblique.ttf OUT/DejaVuSans-Oblique.ttf
python tools/rename_amipro_dejavu.py bold-oblique DejaVuSans-BoldOblique.ttf OUT/DejaVuSans-BoldOblique.ttf
```

## AmiPro Preservation CJK Regular

`AmiProPreservationCJK-Regular.ttf` is a static, renamed, bounded BMP subset
derived from Noto Sans CJK SC version 2.004. Noto Sans CJK is licensed under
the SIL Open Font License 1.1; see `LICENSE-Noto-CJK.txt`. The derived font is
renamed to avoid using the upstream Reserved Font Name `Source` as its family
identity. Its copyright and version name records are retained.

- Upstream repository: <https://github.com/notofonts/noto-cjk>
- Pinned commit: `165c01b46ea533872e002e0785ff17e44f6d97d8`
- Immutable source:
  <https://raw.githubusercontent.com/notofonts/noto-cjk/165c01b46ea533872e002e0785ff17e44f6d97d8/Sans/Variable/TTF/NotoSansCJKsc-VF.ttf>
- Source SHA-256:
  `990c807e79c25662a5a9ecf7f971baeb2bf2eab9a559e5ecf15cdfdb8561d21f`
- Derived font SHA-256:
  `267a6ba550900fec48fd45d8a4fd5f8941f6cff5db9a0f8b313d3b31966da2c0`
- Build tool: fontTools 4.63.0

The subset requests these Unicode ranges exactly:

```text
U+0020-007E,U+00A0-00FF,U+2000-206F,U+2E80-2FFF,U+3000-303F,
U+3040-30FF,U+3100-312F,U+31A0-31BF,U+31F0-31FF,U+3400-4DBF,
U+4E00-9FFF,U+AC00-D7A3,U+F900-FAFF,U+FE00-FE0F,U+FF00-FFEF
```

Only code points present in the upstream SC font are included. The result has
40,227 mapped code points and 39,825 glyphs. It fully covers Basic Latin,
Latin-1, CJK Symbols and Punctuation, Katakana, Katakana Phonetic Extensions,
and Hangul Syllables within the requested ranges. It covers 6,582 of 6,592
requested CJK Unified Ideographs Extension A code points, 20,976 of 20,992 CJK
Unified Ideographs, 93 of 96 Hiragana code points, 43 of 48 Bopomofo code
points, 28 of 32 Bopomofo Extended code points, 366 of 512 CJK Compatibility
Ideographs, and 225 of 240 Halfwidth and Fullwidth Forms. General Punctuation
and CJK Radicals Supplement/Ideographic Description Characters are included
where the source maps them. The source has no requested variation-selector
mappings, and the derived font does not claim non-BMP Han coverage.

### Reproducible build

Run these commands from the repository root with fontTools 4.63.0.
`tools/rename_amipro_cjk.py` performs only the deterministic name-table and
timestamp changes described below.

```sh
fonttools varLib.instancer NotoSansCJKsc-VF.ttf wght=400 --static \
  --update-name-table --no-recalc-timestamp \
  --output NotoSansCJKsc-Regular-stage.ttf

pyftsubset NotoSansCJKsc-Regular-stage.ttf \
  --output-file=AmiProPreservationCJK-Regular-pre.ttf \
  --unicodes='U+0020-007E,U+00A0-00FF,U+2000-206F,U+2E80-2FFF,U+3000-303F,U+3040-30FF,U+3100-312F,U+31A0-31BF,U+31F0-31FF,U+3400-4DBF,U+4E00-9FFF,U+AC00-D7A3,U+F900-FAFF,U+FE00-FE0F,U+FF00-FFEF' \
  --layout-features='' \
  --drop-tables+=BASE,GDEF,GPOS,GSUB,STAT,vhea,vmtx \
  --name-IDs+=13,14 \
  --legacy-cmap --notdef-glyph --notdef-outline --recommended-glyphs \
  --no-recalc-timestamp --canonical-order

python tools/rename_amipro_cjk.py AmiProPreservationCJK-Regular-pre.ttf \
  AmiProPreservationCJK-Regular.ttf
```

The last step removes name IDs 1, 2, 3, 4, 6, 16, 17, 18, 21, 22, and 25,
then writes Windows Unicode English records for:

```text
1  AmiPro Preservation CJK
2  Regular
3  AmiPro Preservation CJK 2.004
4  AmiPro Preservation CJK Regular
6  AmiProPreservationCJK-Regular
16 AmiPro Preservation CJK
17 Regular
```

It sets `head.created` to `3702527940` and `head.modified` to `3702528280`
(the fixed timestamps in the immutable source), saves with
`recalcTimestamp=False`, and uses canonical table ordering. Repeating the
build produces the derived SHA-256 above. Upstream name IDs 13 and 14 retain
the OFL description and license URL in the derived font itself.
