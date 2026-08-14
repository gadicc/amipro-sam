# Ami Pro 3.1 executable-format interoperability research

Date: 2026-08-14

## Result and boundary

The highest-value static targets are not the packed primary executable.  They are
`W4W33F.DLL` and `W4W33T.DLL`, the bundled Word For Word SAM reader and writer.
Their three NE segments have direct file mappings, their exports identify the
conversion direction, and relocation-aware call paths reach the table, frame,
paragraph, and style serializers described below.

This pass confirms how the bundled W4W filter subsystem reads or writes table
dimensions, zero-based row/column addresses, and four-field `<:I...>` records.  It
also finds strong interoperability evidence for those structures in the corpus and
for row/column metrics, merge control, four packed cell components, frame-edge
arithmetic, and several inline-command shapes.  It does **not** promote any native
converter semantic change to confirmed status: corpus save provenance is not
attested, and no controlled Ami Pro oracle observation exists.  Border/shading/
formula/protection labels, most flag names, the first/rest ordering in `<:I...>`,
fixed or repeating frame placement, and the remaining inline semantics stay open.

One broad existing claim is contradicted: the first field of `<:#first,width>` is
not a general horizontal region origin or left margin in the private corpus.  A
first-line-offset interpretation is strongly disfavored but not falsified by that
arithmetic alone.  The field instead correlates with font/line height and text
volume; any vertical interpretation remains tentative because no executable
consumer or controlled oracle observation was found.

No vendor code was executed.  No executable, DLL, filter, resource, font, help file,
document content, extracted segment, or durable decoded image was copied into the
repository.  The committed excerpts total only a few bounded instruction windows
needed to make individual claims reviewable.

## Sequence and repository state

The investigation began from clean commit `386c778`.  The reviewed plan was committed
alone as `0d40f04` before this tooling and ledger.  During the investigation, a
separate concurrent task committed `c49d102`, which changes table parser/model/
renderer semantics.  This research did not author, stage, amend, revert, or treat
those changes as evidence.  They need an evidence review before being retained as
the implementation of any result below.

Another concurrent task committed its own plan and isolated emulator-oracle scaffold
as `8455a8a` and `7ab96d8`.  This investigation did not author or use that scaffold,
and no vendor program was launched through it.

The converter, its current tests, and `docs/format-notes.md` are baseline context,
not corroboration.  The research commit contains only the files identified in its
staged safety review.

## Evidence and confidence rules

The confidence terms are those fixed in the committed plan:

- **confirmed**: two genuinely independent sources with a dependency analysis, or a
  controlled Ami Pro observation that isolates the field;
- **strong**: complete reproducible static parser/serializer-to-use data flow, but no
  independent behavioral confirmation;
- **tentative**: a partial static path or discriminating corpus correlation with
  plausible alternatives;
- **contradicted**: a reproducible observation falsifies the claim as stated;
- **open**: the available path or sample does not discriminate among meanings.

`W4W33F.DLL` and `W4W33T.DLL` are treated as one static conversion subsystem, not
two independent sources.  ndisasm, Capstone, and objdump are three views of the same
bytes, not independent evidence.  Aggregate documents are observations separate
from the static trace, but some could have passed through the same conversion
technology; without provenance they cannot complete the confirmation gate.  The
KOffice notes and KOffice parser are one public reverse-engineering source, not two.

## Input and module inventory

Every analysis entry point verifies the exact bytes it opens.  Stage one creates the
manifest; stage two requires the recorded size and SHA-256 before token search or
disassembly.  Both stages use one `O_NOFOLLOW` descriptor, compare identity and
metadata before and after reading, and parse the bytes that were hashed.

The primary trust anchor matched exactly:

| Module | Bytes | SHA-256 |
|---|---:|---|
| `AMIPRO.EXE` | 888,224 | `555506d1558d61579d5c6fee8bf5fa9d960aa05a20a5d171240ac2e0ea73cbbd` |

The deterministic flat inventory examined 662 payload-root entries: 659 regular
files, three skipped directories, and 19,870,333 regular-file bytes.  It found 134
root-level NE containers.  A separate recursive reconnaissance found nine additional
NE files below `DIALOGED/` and `SPELL/`, for 143 in the extracted tree.  The plan
deliberately made the committed manifest nonrecursive; the nested dialog/spelling
modules did not contribute to any claim.

Relevant module identities are:

| Module | Bytes | SHA-256 | Structural role and disposition |
|---|---:|---|---|
| `W4W33F.DLL` | 108,176 | `a2f34efd5191ee1bd71e7410a689266f861932e1c6f734ff7a3c47f3dcd95759` | `FILTERFROM`/`WFWFROM`; direct SAM reader; primary target |
| `W4W33T.DLL` | 110,960 | `0a1fd9f33120f16f2b96a5eb7716ee35d52ad2829fc1d610eb17302bd9c8eba1` | `FILTERTO`/`WFWTO`; direct SAM writer; primary target |
| `AMIFM.EXE` | 62,208 | `6a77d5f0d737319fbe7a886f08911eef24eaa116659dc278d604e76c61caea62` | direct five-segment file manager; native section table corroborates traversal only |
| `AMIPROUI.DLL` | 479,488 | `4378e439aff955fb8a7dbd3a22ba041ff4cbbfce23b20fbe1a069b9374bc7a35` | UI exports; 141/142 mappings unsupported because iterated/self-loaded |
| `AMIPRO.EXE` | 888,224 | trust anchor above | primary document application; 208/209 mappings unsupported |
| `AMIPRINT.EXE` | 34,224 | `8b60c908b6670098d120a953615cd5c317c394677e67eb4ed4fdab3d2a5e067f` | direct print-pipeline candidate; no necessary SAM path found |
| `AMIENV.DLL` | 11,208 | `d3e1f6e28d585153e9e0093778f0c0b0a1a6cfe3161a05f924898346c88287a6` | paper/environment support; excluded from field tracing |
| `AMIFONT.DLL` | 34,074 | `880bf10fb876be9d255fb427d3ec92188071e49917d911889e9042af67bb6bd5` | metrics/kerning candidate; no required path found |
| `AMILOTUS.DLL` | 80,320 | `2173b922daf45d87365f7ab632bfbfbfb4136890d2d7259073aa65251b073c09` | Lotus integration candidate; no native parser anchor found |

The complete machine-readable list, segment counts, relocation counts, resource
metadata counts, hashes, and tool probes are in `module-manifest.json`.  Resource
names and bodies are absent.  The manifest's own size and SHA-256 are recorded in
the final reproduction section so evidence packets can bind to one exact inventory.

## Tool assessment

| Tool | Observed version/status | NE result and limitation |
|---|---|---|
| Wine `winedump` | Wine suite 11.14; dumper has no version switch | Parses NE headers, segments, exports, resources, and relocation records; no disassembly or xrefs |
| GNU objdump | Binutils 2.46.1 | Rejects NE; usable only on an already validated raw x86-16 window |
| LLVM objdump/readobj | LLVM 22.1.8 | Reject NE |
| ndisasm | 3.02 | Decodes raw x86-16; knows neither NE mapping nor fixups |
| cstool/Capstone | 5.0.9 | Decodes raw x86-16; no NE loader or fixups |
| 7-Zip | 26.02 | Does not provide a usable NE/resource model for these filters |
| pefile | 2024.8.26 | Rejects the non-PE signature |
| Ghidra | not installed | Current built-in loader source does not expand these iterated/self-loaded mappings |
| radare2/rabin2 | not installed | Current NE plugin source maps stored file ranges directly |
| Rizin/rz-bin | not installed | Current NE plugin source likewise lacks the required reconstruction |
| wrestool and other resource extractors | not installed | No extraction was needed or performed |

The source assessment is pinned rather than inferred from product reputation:

- Ghidra release `Ghidra_12.1.2_build`, commit
  `c0f584bf229fffba61b36431f3ce30c0c3e4e682`; `NeLoader.java` lines 244-276
  create a block from the stored file offset/length and zero-fill allocation slack,
  without an iterated/self-loader expansion
  ([source](https://github.com/NationalSecurityAgency/ghidra/blob/Ghidra_12.1.2_build/Ghidra/Features/Base/src/main/java/ghidra/app/util/opinion/NeLoader.java#L244-L276),
  [release](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.2_build)).
- radare2 6.2.0, commit `e1fc278734ad62f933fc6f91edb29e4ba732f402`;
  `libr/bin/format/ne/ne.c` lines 113-131 report the stored segment length and
  aligned file offset directly
  ([source](https://github.com/radareorg/radare2/blob/6.2.0/libr/bin/format/ne/ne.c#L113-L131),
  [release](https://github.com/radareorg/radare2/releases/tag/6.2.0)).
- Rizin v0.9.1, commit `c3a90e9226d977f58f4e9c75f78fa6b07afe13c7`;
  `librz/bin/format/ne/ne.c` lines 127-152 use stored segment bytes and a direct
  physical address
  ([source](https://github.com/rizinorg/rizin/blob/v0.9.1/librz/bin/format/ne/ne.c#L127-L152),
  [release](https://github.com/rizinorg/rizin/releases/tag/v0.9.1)).
- Wine 11.14's 16-bit NE loader treats segment 1 of a self-loading module through
  the standard iterated-record path and delegates later segments to the module's
  self-loader.  The local indexer mirrors only that structural boundary and marks
  later loaded mappings unsupported
  ([source](https://github.com/wine-mirror/wine/blob/wine-11.14/dlls/krnl386.exe16/ne_segment.c)).

Those loaders may still be useful on ordinary direct NE files.  They are not a valid
memory-map authority for this `AMIPRO.EXE`: all 209 segments are `SELFLOAD`, 208 are
`ITERATED`, and 206 are discardable.  The research indexer therefore reports 208
loaded mappings as unsupported instead of guessing.  Installing a modern decompiler
would not repair that missing representation.

## Address and relocation convention

Addresses below are `segment:offset`; file offsets are hexadecimal.  The two filter
layouts are:

| Module | Segment 1 | Segment 2 | Segment 3 |
|---|---|---|---|
| `W4W33F.DLL` | file `0x1200`, length `0xfed8` | `0x11220`, `0x7d5a` | `0x18f90`, `0x165d` |
| `W4W33T.DLL` | file `0x1200`, length `0xfab4` | `0x10df0`, `0x88b6` | `0x196c0`, `0x1a0b` |

All six mappings are validated direct mappings.  Relocation counts are 39/2/1 for
`W4W33F` and 39/2/2 for `W4W33T`.  A separately implemented, hash-gated
`winedump` cross-check agrees on every stored offset, stored length, raw flag,
allocation size, and relocation count for these filters and all 209 primary-
executable segments.  This is a structural parser cross-check, not independent
semantic evidence and not validation of the primary executable's loaded image.

For a non-additive internal selector fixup, the word stored at the selector site is
the next relocation-chain offset, not the runtime selector.  It terminates at
`0xffff`.  In a far call the selector site is `call_start + 3`; the preceding word
is the target offset and the relocation record supplies the target segment.  Raw
decoder output such as `call 0xa1c1:0x013a` is therefore annotated as a call to
`2:013a`, not trusted literally.  Imported, additive, loader-created, and indirect
callback targets were not invented.

Every representative code window below is reproducible with
`tools/research/evidence.py packet` against the exact manifest.  The packet command
records module size/hash, segment and mapped file offset, up to 64 raw bytes, all
overlapping relocation annotations, and the manifested identities of ndisasm,
Capstone, and objdump.  Those decoder views are formatting cross-checks only.  Call
paths and inferred behavior in the ledger are analyst review fields, not output of a
decompiler.

### Converter baseline (not evidence)

At the initial `386c778` baseline, table dimensions were inferred from maximum cell
coordinates, `[h]`/`[w]` and most cell fields were opaque, and an inferred first row
could become a header.  Concurrent commit `c49d102` now materializes many table
fields, flags, merge relationships, and metric labels; none of that code or its tests
corroborates the claims below.  It is the implementation under review.

The current paragraph parser preserves `<:I...>` only when it has four nonnegative
fields and f3 is zero, and does not apply the values.  It stores `<:#...>` as
`region_x_twips`/`region_width_twips`; a full-width region is rendered with f0 as a
first-line offset.  Existing frame, inline, and style behavior derives from the
project's earlier public/corpus research.  It remains baseline context, never
executable evidence.  Each packet below says whether the new evidence confirms,
contradicts, or leaves that behavior open.

## Evidence ledger

### RE-TBL-01 — table dimensions and definition shape

**Claim.** `[tbl]` field 0 is the declared row count and field 1 is the declared
column count.  The legacy writer emits seven fields; corpus fields 7 and 8 are not
explained by this filter.

**Static evidence.** Modules and hashes are the `W4W33F/T` identities above.
Reader dispatch follows `F 1:9830 -> 1:8498`.  `F 1:854c` passes data token
`F 3:06c0` (`[tbl]`) and `3:06c6` (`[frmlay]`) to `1:705c`.  `F 1:85bd`
searches `3:06cf` (`[data]`) and inner `3:06d6` (`[tbl]`).  The parser at
`F 1:85e0-864e`, file `0x97e0-0x984e`, reads only fields 0, 1, 2, 4, and 5.
Its outer loop compares against field 0 at `1:8afe`; the nested loop compares
against field 1 at `1:8bb4-8bbd`.

The writer path is `T 1:beac` (frame type 4), `1:beb7 -> 1:75f4` (frame),
`1:bede -> 1:7c30` (table), then `1:beeb` (`[data]`).  At
`T 1:7c6e-7ccd`, file `0x8e6e-0x8ecd`, it emits:

```text
f0 = [table+2]   f1 = [table+4]   f2 = 286   f3 = 86
f4 = [table+8]   f5 = 86          f6 = 1
```

Representative 64-byte packet at `T 1:7c6e`:

```text
ff362a048e460826ff75029a84958f7c83c404ff362a048e460826ff75049a8495
9f7c83c404ff362a04b81e01509a8495af7c83c404ff362a04b85600509a84
7c72 mov es,[bp+8]       7c75 push [es:di+2]
7c85 mov es,[bp+8]       7c88 push [es:di+4]
7c98 mov ax,0x011e       7ca8 mov ax,0x0056
```

The selector fixups at `1:7c7c`, `1:7c8f`, and `1:7c9f` resolve internally to
segment 1; raw displayed selectors are relocation-chain words.

**Independent aggregate evidence.** The bounded private-corpus analyzer finds 373
exact nine-field definitions and 369 data-bearing tables.  All 13,099 exact
12-field cell coordinates fit `[0, f0) x [0, f1)`, with no duplicate coordinate in
a table.  The maximum row reaches `f0-1` in 367/369 tables, the maximum column
reaches `f1-1` in 363/369, and 274 contain a complete declared rectangle.  This is
independent of the filter control-flow trace, subject to the possibility that some
source documents originated through the same conversion ecosystem.

The analyzer also applies the deliberately wrong swapped-axis interpretation.  It
fits only 5,542/13,099 records and all records in 37/369 tables, versus 13,099 and
369 under the stated orientation.  Restricting the comparison to unequal declared
dimensions leaves 7,557/12,913 records outside the swapped grid and only 4/336
tables entirely inside it.  This is a discriminating control, not merely a check
that the preferred labels happen to fit.

**Alternatives and limits.** Fields 0/1 could have been maximum indexes rather than
counts, but the writer's one-based internal values and the observed maxima disprove
that.  The filter emits only f0-f6 and ignores f3/f6+, so it cannot explain corpus
tails f7/f8.  The observed tails are equal (`43` in 368 definitions and `80` in
five); calling them “reserved” is unsupported.  After applying explicit row/column
metrics or table defaults, the frame-height residual equals the common tail in
322/373 tables (324 within three units), while the width residual does so in
225/373 (248 within three).  This makes an outer allowance or border extent a
discriminating corpus hypothesis, not a field meaning.

**Confidence:** **strong** for the native-format interpretation of f0 rows and f1
columns, with the W4W filter behavior itself executable-confirmed; **tentative** for
f7/f8 as an outer-extent allowance, with the exact purpose still **open**.  Missing
corpus provenance prevents a confirmed converter change.

### RE-TBL-02 — row and column metrics

**Claim.** `[h]` f0 and `[w]` f0 are zero-based row/column indexes.  `[w]` f1/f2
form per-column width/gutter geometry.  `[h]` f1/f2 are consistent with row
height/gutter, but the reader filter intentionally ignores row records.

**Static evidence.** `F 1:86c0` recognizes `[h]`, reads until `[e]`, and never calls
the integer-field parser `1:978a`; this establishes lossy filter behavior, not absent
row semantics.  The row writer at `T 1:7e10`, called from `1:bc0a`, emits seven
fields:

```text
f0 = iteration index
f1 = [row+6], default 286 when zero
f2 = 86
f3 = 1 iff [row+0x22] > 1 or [row+0x24] > 1
f4 = 0   f5 = 0   f6 = 0
```

The caller initializes `T 3:2442` to zero at `1:b4f5` and increments it after
each row at `1:bc12`, proving a zero-based f0.

`F 1:868b-86ac` initializes two column-length arrays from `[tbl]` f4/f5.
`F 1:878f-87d7`, file `0x998f-0x99d7`, parses `[w]` f0/f1/f2, uses f0 as the
array index, and overrides both values.  `F 1:9330-940f` accumulates the first
array across columns as successive right boundaries; the previous second-array
value adjusts the next left boundary.  That is direct width/gutter behavior.

The column writer at `T 1:7ed8` emits f0=`[column+0x20]-1`, two caller-supplied
metrics, the same span-present boolean, and zero.  No direct far-call xref was found,
so its surrounding callback context remains less certain.

Paired direct reader packets at `F 1:9396`, file `0xa596`, and `1:93e8`, file
`0xa5e8`; the first arithmetic window has no relocation overlap and the second's
far calls carry the ordinary selector annotations:

```text
8b46168b561848488946f68956f88b46128b56148bf88956f2c45e0a268b4704
8bc803c68946fc8e46f226030d03ce894efa
8b46fa054800b990002bd2f7f1509af0f102945b33c0509af0f172945b8e46f2
8bdf47472603378346f602ff46f48b46f4c45e0e26394704
939c dec ax              939d dec ax
93b2 mov ax,[es:bx+4]    93b8 add ax,si
93c0 add cx,[es:di]      93c3 add cx,si
940a inc di              940c add si,[es:bx]
941c cmp [es:bx+4],ax
```

The two decrements and loop comparison expose a zero-based iteration boundary;
the separate accumulators and `add si,[es:bx]` are the cumulative geometry path.

**Aggregate evidence.** All 3,112 exact `[h]` f0 values are below the declared row
count and all 973 exact `[w]` f0 values are below the declared column count.  Among
data-bearing tables, 342 have a complete zero-based row-index set and 236 have a
complete column-index set.  Row f1 equals the table default in 2,077/3,107
data-bearing row records; column f1 equals the default in only 16/973 records.  Row
f2 equals the default in 2,781/3,112 records and column f2 in 808/973.  These are
stored equality/override relationships, not proof of UI intent or rendered units.
As an orientation control, interpreting `[h]` f0 against the column bound fails for
2,024/3,112 records, while interpreting `[w]` f0 against the row bound fails for
118/973.  Native bounds fit every record.  The unequal-dimension strata retain all
of those failures, so equal-size tables do not explain the result.

**Alternatives and limits.** The paired `[w]` values could be width plus inter-column
spacing, or two edges.  The accumulation/update flow discriminates in favor of
width plus gutter.  `[h]` f1/f2 naming relies on serializer symmetry, defaults, and
public reverse-engineering rather than a reader/formatter consumer.  The span-linked
f3 is not a general header/repeat flag.  Remaining row/column tail bits are open.

**Confidence:** **strong** for f0 index bases and `[w]` f1/f2 as width/gutter
geometry, with the corresponding W4W paths executable-confirmed; **tentative** for
`[h]` f1/f2 as row height/gutter because the reader skips these values; **open** for
broader flags, exact UI terminology, and tails.

### RE-DATA-01 — cell coordinates and connected cells

**Claim.** `[data]` f0/f1 are zero-based row/column coordinates.  In f2, bit
`0x80` marks connected-cell participation and bit `0x100` distinguishes the anchor;
f3/f4 carry connected-cell topology, with a horizontal relationship observed here.

**Static evidence.** The cell serializer at `T 1:7f70`, called from `1:a29b` and
`1:bd60`, emits 12 fields.  At `1:7f8d-7fae`, file `0x918d-0x91ae`, it deliberately
subtracts one from two one-based internal members:

```text
56c45e06268b470248509a8495ac7f83c404
56c45e06268b472048509a8495c37f83
7f91 mov ax,[es:bx+2]     7f95 dec ax     ; f0
7fa3 mov ax,[es:bx+0x20]  7fa7 dec ax     ; f1
```

The writer uses f2 `0x0200` for an ordinary cell, `0x0280` for a connected member,
and `0x0380` for an anchor.  `T 1:7fe7-803e` obtains f3/f4 from structure offsets
`+0x24`/`+0x22` or a merge lookup.  The reader at `F 1:955e` tests `0x80` and
`0x100`: an `0x100` cell stores f3/f4 in per-column anchor state; an `0x80` cell
without `0x100` consumes that state to compute connected extents.

Representative direct packet at `F 1:955e`, file `0xa75e`; the selector fixup at
`1:9582` resolves the call at `1:957f` to internal `1:9728`:

```text
8cd89045558bec1e8ed883ec0257568b7608f7c680007416f7c600017510ff7606
9a289793955b0bd07503e92801
9570 test si,0x0080      9574 jz 958c
9576 test si,0x0100      957a jnz 958c
957c push [bp+6]         957f call 1:9728
```

**Aggregate evidence.** All 13,099 exact coordinates in data-bearing tables are
within declared bounds and the dimension maxima are reached as described in
RE-TBL-01.  The swapped-axis control fits only 5,542/13,099 records and leaves
7,557 outside; correct orientation fits all 13,099.  The corpus has 195 `0x180`
low-flag anchors and 265 `0x80` non-anchor members.  Every observed anchor has
f3=1; f4 is usually 2 or 3.  Relative member offset `(0,1)` occurs 195 times and
`(0,2)` 44 times.  Requiring member bodies to be raw-empty validates 174 anchor
rectangles covering 244 members and leaves 21 of each uncovered.  Treating
control-only bodies as empty validates all 195 anchors and 265 members.  This
distinction is preserved because the reader flow does not prove whether
control-only storage invalidates a merge.  No vertical example exists.

**Alternatives and limits.** The field order could be column,row, but the writer's
row/column iteration state and the swapped-axis corpus control reject that.  The
static bit behavior plus complete horizontal rectangles supports the observed
horizontal case; vertical f3/f4 orientation remains unobserved rather than
generalized by symmetry.

**Confidence:** **strong** for native-format f0 row, f1 column, and zero origin and
for the `0x80`/`0x100` anchor/member control flow and observed horizontal
relationship; the filter paths themselves are executable-confirmed.  The
member-body rule and vertical orientation outside this sample remain **open**.

### RE-DATA-02 — alignment, borders, shading, formula, text, and protection

**Claim inventory.** This packet separates structure directly used by the filters
from semantic labels that remain inferential.

**Static evidence.** Modules and hashes are the filter identities above.

- f2 alignment bits 8/16/24/32 are not tested by a traced filter consumer.  Corpus
  co-occurrence with inline alignment is strong but not an executable meaning.
- The writer emits f5=`11`; the reader ignores f5.  Neither path names shading.
- `T 1:826e-829d`, file `0x946e-0x949d`, collapses four input nibbles into f6
  output bits `0x0001`, `0x0010`, `0x0100`, and `0x1000`.  This proves four packed
  components and loss of the input magnitude.  Calling them border sides remains a
  semantic inference; the path does not establish side order or line style.
- For f7-f11, `T` emits `1,0,0xffffff,0,0` when structure member `+0x28` is `-1`,
  otherwise `0,0,0xffffff,1,[+0x28]`.  `F` parses f10/f11 only when f7 is zero;
  nonzero f10 triggers a lookup of records keyed by f11.  This is a presence plus
  reference-ID path.  The code does not call the target a formula.
- `F` ignores f5, f8, and f9; `T` always emits f8=0 and f9=`0xffffff`.

Representative direct f6-helper packet at `T 1:826e`, file `0x946e`, with no
relocation overlaps:

```text
8cd89045558bec1e8ed88b5e0633d2f6c70f7403ba0100f6c30f740380ca10f6
c7f0740380ce01f6c3f0740380ce108bc2
827d test bh,0x0f        8282 mov dx,0x0001
8285 test bl,0x0f        828a or dl,0x10
828d test bh,0xf0        8292 or dh,0x01
8295 test bl,0xf0        829a or dh,0x10
```

Paired direct writer packets at `T 1:8080`, file `0x9280`, and `1:80c3`, file
`0x92c3`, cover the `+0x28 == -1` branch and its alternative.  The far calls are
selector-annotated internal numeric writers:

```text
26837f28ff753c56b80100509a84959b8083c4045633c0509a8495ac8083c404
56b8ffffbaff0052509ab495b88083c4065633c0509a8495ca8083c4045633c0
5633c0509a8495d68083c4045633c0509a8495e78083c40456b8ffffbaff0052
509ab495f48083c40656b80100509a8495a37983c40456c45e0626ff77289a6c
8080 cmp [es:bx+0x28],-1  8085 jnz 80c3
8088 mov ax,1              8095 xor ax,ax
80a1 mov ax,0xffff         80a4 mov dx,0x00ff
80c4 xor ax,ax             80d0 xor ax,ax
80dc mov ax,0xffff         80df mov dx,0x00ff
80ed mov ax,1              80fd push [es:bx+0x28]
```

The paired reader packets bind the complementary path.  At `F 1:8926`, file
`0x9b26`, field 7 is parsed and tested; only when it is zero are fields 10 and 11
parsed into separate locals.  At `F 1:8a35`, file `0x9c35`, nonzero field 10 gates
a scan of 49-byte records, and field 11 is compared with the candidate key.  The
selector fixups in both windows resolve their far calls internally:

```text
ff76f056b80700509a8a97518983c4068946fa0bc075358b46f05056b90a0051
89b6d8fa8986dafa9a8a97688983c4068946f2ffb6dafaffb6d8fab80b00509a
892a mov ax,7             8936 mov [bp-6],ax
8939 or ax,ax             893b jnz 8972
8942 mov cx,10            8956 mov [bp-0xe],ax
8961 mov ax,11

837ef200744a33ffbbee3e8b56ea39177410837f0aff740a4783c33181fb8048
72ec8bc7b93100f7e98bd88b46ea3987ee3e751cb8010050579a48bd8e8a83c4
8a35 cmp [bp-0xe],0       8a39 jz 8a85
8a40 mov dx,[bp-0x16]     8a43 cmp [bx],dx
8a4e add bx,0x31          8a63 cmp [bx+0x3eee],ax
```

**Aggregate/public correlation.** After deduplicating repeated inline alignment
commands per cell, 5,144/5,178 observations match the expected low f2 value:
`0x18`/center 3,444 times, `0x10`/right 1,668, `0x08`/left 31, plus one suppressed
justify/`0x20` match; the 34 mismatches include plausible inline overrides.  Fields
f5 and f9 are simultaneously zero in
12,313 cells or simultaneously nonzero in 786, with no one-sided case; this is
consistent with but does not prove a shading-code/color pair.  Every f6 nibble is
in 0..4, and common values such as `0x1111` and zero look side-like.

f7 is binary: 9,926 values are one and 3,173 are zero.  With raw stored bodies the
matrix is `(1,body)=9,774`, `(1,empty)=152`, `(0,empty)=3,173`; after stripping
control-only material it is `(1,material)=8,558`, `(1,empty)=1,368`,
`(0,empty)=3,173`.  Thus f7 selects a content/storage condition but is not a simple
rendered-text predicate.  Exactly 152 post-close nonblank metadata lines occur, all
on f7=1 cells, without identifying their meaning.  f8 is one only 43 times.  Corpus
f10/f11 are always zero, so it cannot corroborate the filter's reference path.
KOffice's own format notes say frames, images, and tables remained largely
unexplained ([FileFormat.txt lines 215-217](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/FileFormat.txt/#L215)),
so they are not independent support for these table-field labels.

**Alternatives and limits.** Alignment correlations can reflect redundant inline
state.  f5/f9 could describe another fill/background property.  f7 could select a
storage mode rather than text presence.  f8 has no discriminating behavior.
Fields f10/f11 may reference a formula or some other keyed metadata; the 152
post-close lines do not connect to that path.  “Formula” is therefore not confirmed.

**Confidence:** **strong** for f6 as four packed components and f10/f11 as a
conditional presence/reference path; **tentative** for border interpretation,
alignment labels, shading/color, and f7 as a content/storage selector; **open** for
border side/style/color, formula, protection, f8, and other undocumented tails.

### RE-I-01 — four-field `<:I...>`

**Claim.** The traced writer emits four numeric fields, and every bounded corpus
instance has that shape.  f0 can be a left-container inset and f3 can be a nonzero
right-container inset.  f1/f2 are distinct line-start measures, but their first/rest
ordering and any reader-accepted variants are unresolved.

**Static evidence.** Module: `W4W33T.DLL`, 110,960 bytes, SHA-256
`0a1fd9f33120f16f2b96a5eb7716ee35d52ad2829fc1d610eb17302bd9c8eba1`.
The command is emitted character-by-character through relocated calls to `T 2:013a`;
there is no contiguous `<:I` literal.

`T 1:a0a0`, called by paragraph writer `1:a258` at `1:a48e`, emits
`<:I0,[3:246e],[3:2470],[3:2472]>`.  It bounds the first two computed values
against container geometry, but not the third.  The values flow through
`1:9f78 -> 2:24b2 -> 2:1f0e`; `2:240a-2422` stores three separate accumulators.

`T 1:a19c`, file `0xb39c`, emits `<:I[3:538b],0,0,[3:538d]>` and is called at
`1:a4a9` if either value is nonzero.  Its prefix packet shows the relocated
character writer calls:

```text
8cd89045558bec1e8ed8ff362404b83c00509a3a01c1a183c404ff362404b83a00
509a3a01d1a183c404ff362404b84900509a3a01f1a183c404ff362404ff36
a1aa mov ax,0x3c    a1ba mov ax,0x3a    a1ca mov ax,0x49
a1ae/a1be/a1ce call 2:013a after selector relocation
```

The producers are direct geometry arithmetic:

```text
T 1:d87d-d883:  3:538b = absolute_left - container_left
T 1:d8c5-d8ca:  3:538d = container_right - absolute_right
```

These are file windows `0xea70-0xeaaf` and `0xeab8-0xeaf7`.

**Aggregate evidence.** All 750 corpus instances have arity four.  Observed f1 and
f3 are always zero; f2 is nonzero (`3240`) only six times.  No material character
precedes any command in its blank-delimited storage unit; 630 share a unit with a
region command and 665 with a font command.  This supports a start-of-layout
association, not that material necessarily follows or what a field means.  The
writer's second path proves that the corpus invariant f3=0 is not a grammar
constraint.

**Alternatives and limits.** The combined paths suggest shared/all-line, first-line,
rest-line, and right insets, but do not order f1/f2 or prove whether f0 adds to both.
The static writer can emit nonzero f3, but no controlled Ami Pro load/save/render
observation shows how a reader treats it.

**Confidence:** **strong** for the observed exact four-field shape, nonzero-f3
writer capability, and the f0/f3 left/right inset arithmetic in the second writer
path; **tentative** for the combined shared/first/rest/right model.  The corpus's
relationship to the W4W ecosystem is not attested, and no reader path excludes
other arities.  Relaxing the converter's trailing-zero condition is not a confirmed
change because only the static subsystem demonstrates nonzero f3.

### RE-REGION-01 — paragraph region `<:#first,width>`

**Claim review.** The second field behaves like a container measure in common cases.
The first field is contradicted as a general horizontal region origin or left
margin.  A first-line offset remains possible but is strongly disfavored; a
height/line-box interpretation is plausible but not confirmed.

**Aggregate evidence.** The privacy-preserving analyzer selected 384 regular `.sam`
files, decoded all 384, and had one parser-rejected file.  Decoded command and
storage-unit aggregates remain eligible for that file; parser-derived body geometry
and all table scans do not.  It found 11,446 bounded exact-arity two-field unsigned-
decimal commands in 299 documents; 11,376 commands in 295 documents had usable
primary page geometry.

- The second field is within 3 twips of body width 8,844 times and within 5 twips
  8,860 times.  All 16 additional cases have signed delta `+5`; this resolves the
  earlier three-versus-five-twip discrepancy without changing policy.
- If f0 were a horizontal origin added to f1, `f0 + width` would exceed the body in
  8,910 usable cases.  That falsifies a general origin/left-margin interpretation,
  but not every possible indent applied inside a full-width region.
- f0 has mode 284 (4,592 occurrences); 5,384 values are exact multiples of 284.
  Multipliers 2 through 6 occur 333, 159, 80, 61, and 58 times.
- 6,646 commands have a following numeric font command in the same storage unit.
  Dominant pairs include size 240 -> f0 284 (1,440), 360 -> 422 (311),
  280 -> 332 (262), 480 -> 562 (260), and 120 -> 144 (224).  The median ratio is
  1.183333.
- At width 4,394, median material-character counts for f0 values
  284/568/852/1136/1420/1704 are 0/56.5/87/126/160/196.5.  Similar growth occurs
  at widths 9,025 and 10,468.

**Negative static result.** Neither direct W4W filter yielded a resolved `<:#`
emitter/consumer.  No paragraph-region coordinate transform was established in the
packed core.

**Alternatives and limits.** f0 may be line-box height, stored paragraph height,
layout-cache height, or another vertical extent.  The next font command is not proof
of the active line metric.  Blank-delimited material length is a syntactic proxy,
not rendered glyph measurement.  The body comparison uses the first valid
source-order primary layout, not proven active layout at each paragraph.  The private
corpus has no committed expected-hash inventory, so historical identity needs an
operator-owned out-of-band hash list.

**Confidence:** **contradicted** for “f0 is a general horizontal origin/left margin”;
**tentative and disfavored** for a first-line offset; **tentative** for “f0 is a
line/paragraph height” and f1 as a container measure.  Nested/frame/header/footer
origin cases remain **open** pending the oracle.

### RE-FRM-01 — frame header, layout, and placement bits

**Claim.** `[frm]` f2-f5 are left, top, right, bottom edges.  In the legacy writer,
`[frmlay]` f0 is `top+height`, a bottom edge in the writer's coordinate system, and
f1 is width.  The coordinate origin and exact placement flag names are unresolved.

**Static evidence.** Module: `W4W33T.DLL`, identity above.  Serializer `T 1:75f4`,
called at `1:beb7` for table frames, emits `[frm]` at `1:76df`.  Its fields are:

```text
f0 = 3:0570 - 3:0580      f1 = assembled 32-bit flags
f2 = [frame+0x10]         f3 = [frame+0x12]
f4 = f2 + [frame+0x0c]    f5 = f3 + [frame+0x0e]
```

The same routine emits an 18-field `[frmlay]` at `1:7904`, file `0x8b04`:

```text
f0=top+height  f1=width  f2=1  f3=[+0x16]  f4=[+0x18]  f5=1
f6=top+[+0x14]  f7=[+0x1a]  f8..f13=0  f14=1
f15=left+[+0x16]  f16=left+width-[+0x16]  f17=0
```

Representative prefix:

```text
ff362a04b89e071e509afe951c7983c406b80200509ae69534795bff362a048e46
08268b440e26034412508cc79a6c96467983c404ff362a048ec726ff740c9a
7926 mov ax,[es:si+0x0e]  792a add ax,[es:si+0x12]  ; bottom
793f push [es:si+0x0c]                              ; width
```

Flag construction is exact but unlabeled: the type-4 table call starts with
`0x00080004`; structure `+0x1c != 0` adds `0x00010000`; structure `+0x0a != 0`
adds low `0x0080`, otherwise it adds `0x00020000`; global `T 3:0476 != 0` adds
`0x04000000`.

**Corpus status.** A bounded reconnaissance run suggested useful frame-width,
bottom-coordinate, and anchor-bit experiments, but general frame aggregates are not
yet emitted by the committed analyzer.  They are therefore excluded from this
claim's confidence and are not a second evidence source.  The observed 18-field
writer also cannot explain the multiple longer-arity corpus variants.  Exact variant
counts remain uncommitted reconnaissance and are intentionally not published here.

**Alternatives and limits.** `[frm]` f0 is computed from two globals and is consistent
with a page/layout index, but the executable path does not name it.  A bottom edge may
use frame-, page-, or containing-layout origin depending on context.  Static flag
construction does not by itself name fixed, repeating, wrap, z-order, or header/footer
semantics.

**Confidence:** **strong** for `[frm]` edge order and, in this writer path,
`[frmlay]` f0 bottom/f1 width arithmetic; **open** for anchored/fixed/repeating
placement bits, z-order, fields 18+, and exact origin outside the demonstrated
writer path.  No frame-field meaning reaches the two-source confirmation gate.

### RE-INLINE-01 — `:Z`, `:O`, `:R`, and `:p`

**Static syntax evidence.** Module: `W4W33T.DLL`, identity above.  These are emitters,
not semantic consumers.

- `T 1:c3aa` emits `<:Zdescriptor>` for modes 1/3 and `<:Z~descriptor>` for modes
  2/3 (`1:c3dc-c476`).  No direct caller was found; callback use is likely.
- `T 1:d9bc`, called at `1:d97f` and `1:d9a1`, emits `<:O+X>` or `<:O-X>` using
  raw sign/state characters, not decimal integers (`1:d9cd-da0c`).
- `T 1:d4d4`, called at `1:a454`, emits variable-length
  `<:R1,N,x0,y0,...,>` with `N=[structure+0x59]` and N coordinate pairs
  (`1:d4e9-d58b`).  This disproves a fixed-four-field grammar.
- `T 1:9378`, called at `1:91dc`, `1:badf`, and `1:be54`, emits
  `<:p</@>Standard>` for index zero and `<:p</@>{index+1}>` otherwise.
  `T 1:db50` emits exact noncanonical `<:p >` under state-dependent conditions.

Paired direct `:R` packets at `T 1:d510`, file `0xe710`, and `1:d59b`, file
`0xe79b`, include selector fixups to the character writer `2:013a` and numeric
writers `1:94ec`/`1:9552`:

```text
57b83100509a3a0125d583c40457b82c00509a3a0142d583c404578e460a26ff74
599aec9469d583c40457b82c00509a3a0176d583c404c746f800008e460a26
4646ff46f88b46f8c45e08263947597fb357b83e00509a3a01bfd183c4045e5f
8d66fe1f5d4dcb
d52e push [es:si+0x59]  ; N
d54f cmp [es:si+0x59],0
d59d inc [bp-8]         ; emitted-pair count
d5a6 cmp [es:bx+0x59],ax
d5aa jg d55f            ; emit another pair
```

**Negative-search boundary.** No contiguous command literals occur because the
writer emits characters.  A manual bounded grouping of resolved `T 2:013a` calls
found no lower-case `:r`, no `:b`, and no `<:#` emitter, but that grouping is not a
single reproducible committed command and contributes no confidence.  An apparent
`F` `<:b` at file `0x1744d` is the middle of far-call bytes
`9a 42 3c 3a 62`, not a string.  Absence from these filters would not prove absence
from Ami Pro in any event.

**Reconnaissance only.** A one-off corpus pass saw 75 `:Z` commands in two documents,
ten `:r`, ten `:O`, six `:b`, one variable-looking `:R`, and 37 noncanonical `:p`
forms.  These counts are not emitted by the committed aggregate analyzer and do not
contribute to confidence; they provide only future syntax/context leads.

**Confidence:** **strong** for the signed `:O`, variable `:R`, and `:p` shapes;
**tentative** for `:Z` because no caller was resolved; **open** for all command
semantics and for `:r`/`:b` meanings.

### RE-STYLE-01 — `[algn]` and `[spc]`

**Claim.** The legacy serializer arity/order and backing structure offsets are known;
most semantic names and disputed flags are not executable-confirmed.

**Static evidence.** Module: `W4W33T.DLL`, identity above.  Style serializer
`T 1:82a6`, directly called at `1:c0f2` and `1:cb37`, emits:

```text
[algn]: [+0x4e], 1, [+0x52], [+0x54], [+0x56]
[spc]:  [+0x5a], [+0x5c], [+0x5e], [+0x60], [+0x62], [+0x64], [+0x66]
```

At `T 1:8388`, file `0x9588`, the packet begins:

```text
b80200509ae695a0835bff3632048e460826ff744e9a6c96b08383c404ff363204
b80100509a6c96c38383c404ff3632048e460826ff74529a6c96d68383c404
8399 push [es:si+0x4e]   83a9 mov ax,1   83bc push [es:si+0x52]
```

The public KOffice reverse-engineering notes label `[algn]` as flags, unit,
all-indent, first-line indent, and rest-lines indent; they label `[spc]` as flags,
custom spacing, a sentinel, space-before, space-after, another sentinel, and
tightness ([FileFormat.txt lines 83-96](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/FileFormat.txt/#L83)).
The same notes propose low alignment and line-spacing bits, while explicitly
presenting themselves as unfinished reverse engineering
([lines 155-169](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/FileFormat.txt/#L155)).

**Reconnaissance correlation.** A bounded one-off corpus pass found 3,673
parser-accepted styles with five numeric `[algn]` and seven numeric `[spc]` values.
Exactly one low alignment bit was present in every style, which supports but does
not identify the four choices.  `[algn]` f1 was 1 in 3,661 styles and 2 in 12;
f3=f4 in 3,631; extra bits 16/256 occurred only 33 times.  The `[spc]` tail `(1,100)`
occurred 3,655/3,673.  Joint counts conflict with simple bit16/bit32 before/after
gate stories.  These style aggregates are not yet output by a committed analyzer,
so they remain reconnaissance and are not used to raise confidence.

**Alternatives and limits.** Serializer field order plus KOffice labels do not show
how Ami Pro's layout engine consumes all-indent, first/rest differences, tightness,
or high flag bits.  The corpus is dominated by defaults.  Current renderer behavior
is not evidence.

**Confidence:** **strong** for arity/order/structure offsets; **tentative** for the
public low-alignment and indent labels; **tentative conflict** with simple
bit16/bit32 before/after gates; **open** for high alignment flags, nondefault
tightness, and remaining spacing flags.  No disputed style semantic is confirmed.

## Confirmed converter work and implementation gate

No parser or renderer change is made in this research commit.  No native converter
semantic change currently clears the plan's strict independence gate.  The two
highest-priority candidates after evidence review are:

1. Use `[tbl]` f0/f1 as declared row/column counts rather than inferring dimensions
   solely from maximum cell coordinates.
2. Treat `[data]` f0/f1 and `[h]`/`[w]` f0 as zero-based row/column indexes.
   Bounds checks and duplicate diagnostics remain safety policy; the corpus contains
   no duplicate cell coordinate and supplies no evidence for duplicate precedence.

The sources behind these candidates are dependency-audited.  One is the
direct W4W parser/serializer control flow; the other is a raw-record corpus analyzer
that does not import the converter's semantic table model, run vendor code, or use
the current table implementation as an expected result.  The files predate this
investigation, although their individual native-save provenance is not attested and
some could have passed through the same conversion ecosystem.  Every corpus table
also has a nine-field definition, whereas this W4W writer emits seven.  Together
with the non-marginal native-versus-swapped controls (13,099/13,099 versus
5,542/13,099 cell coordinates, plus 2,024 and 118 opposite-bound failures for `[h]`
and `[w]`) and the independent static subtraction/count loops, this rules out a
mere restatement of the current converter and makes the mappings strong.  It does
not prove that the corpus was produced independently of the filter family, so the
word “confirmed” is withheld pending provenance or an oracle result.  Adjacent
metric and flag labels remain below that threshold as well.

Commit `c49d102` already implements some table behavior and synthetic tests, but it
landed before this evidence review.  Its code and expected outputs must be audited
against packets RE-TBL-01 through RE-DATA-02 before items 1-2 can be promoted.  In
particular, this evidence does not validate its header-row flag,
alignment-bit names, shading/protection/content/formula labels, default/override
terminology, or general merge orientation and member-body policy.

All strong or contradicted findings are deliberately held out of the confirmed
queue.  Beyond items 1-2, that includes row-height/gutter labels, column
width/gutter labels, merge behavior, all cell styling/protection/formula semantics,
nonzero `<:I>` f3 handling, removal of the current `<:#` horizontal interpretation,
and every frame change.
They require another independent source or a controlled Ami Pro oracle observation.
This pass adds only synthetic/runtime-invented research fixtures; it copies no
private or vendor document content.  A later converter commit should add or retain
only synthetic SAM fixtures for each accepted semantic.

## Prioritized open questions and oracle experiments

1. **Paragraph region.** Vary font size, wrapped line count, container type, and
   overflow independently.  Observe whether `<:#` f0 follows line-box height,
   paragraph height, or a cache value and whether f1 is always the active container.
2. **`<:I...>`.** Create four documents that isolate shared, first-line, rest-line,
   and right indentation.  Include nonzero f3 load/save/render behavior before
   relaxing the current parser condition.
3. **Tables.** Generate vertical and two-dimensional merges to orient f3/f4; vary
   each border side/style/color, shading, protection, formula/cached-value state,
   text presence, row flags, and table tails one at a time.
4. **Frames.** Hold coordinates constant while toggling anchored, fixed-page,
   repeating, header/footer, wrapping, and z-order behavior.  Compare `[frm]` flags,
   anchor commands, `[frmlay]` f0 origins, and all longer-arity layout tails.
5. **Styles.** Toggle one `[algn]`/`[spc]` control at a time, especially all-indent,
   first/rest differences, high bits 16/256, custom line spacing, before/after
   spacing, and tightness.
6. **Inline commands.** Produce controlled `:Z`, `:r`, `:O`, `:b`, `:R`, and
   noncanonical `:p` cases; the current filter evidence establishes only syntax.
7. **Packed primary.** Reconstruct `AMIPRO.EXE` only if these oracle tests cannot
   discriminate a field.  Any loader must expand and validate iterated/self-loaded
   memory with a source-record map; the current indexer intentionally stops.

## Reproduction and review record

The supported commands and hard limits are documented in
`../../tools/research/README.md`.  Core checks for this pass are:

```sh
python tools/research/inventory.py \
  --payload-dir "$AMIPRO_PAYLOAD_DIR" \
  --output /tmp/amipro-module-manifest.json
cmp docs/research/module-manifest.json /tmp/amipro-module-manifest.json
python tools/research/winedump_crosscheck.py \
  --manifest docs/research/module-manifest.json \
  --payload-dir "$AMIPRO_PAYLOAD_DIR" --module AMIPRO.EXE
python tools/research/winedump_crosscheck.py \
  --manifest docs/research/module-manifest.json \
  --payload-dir "$AMIPRO_PAYLOAD_DIR" --module W4W33F.DLL
python tools/research/winedump_crosscheck.py \
  --manifest docs/research/module-manifest.json \
  --payload-dir "$AMIPRO_PAYLOAD_DIR" --module W4W33T.DLL
pytest -q tools/research
python -m py_compile tools/research/*.py
python tools/research/safety_audit.py --repo .
```

The final manifest is 133,358 bytes with SHA-256
`6434434bdcb4d33ae94360b79a9d94a3bee46050d1c1fc4e17f5439538c26bec`;
two independent inventory runs were byte-identical.  The exact `winedump` binary is
453,248 bytes with SHA-256
`5da8b2aeb32aa73eb1ced8f38bd3cbb179a68572f49e20f6c3883b4e889b39b1`.
All three bounded cross-checks returned `pass` with no disagreements:

| Module | Entry | Segments | Module refs | Compared per-segment fields | Iterated mappings decoded? |
|---|---:|---:|---:|---:|---|
| `AMIPRO.EXE` | `5:0001` | 209 | 9 | 5 x 209 | no; stored metadata only |
| `W4W33F.DLL` | `1:0f78` | 3 | 3 | 5 x 3 | not applicable |
| `W4W33T.DLL` | `1:1008` | 3 | 3 | 5 x 3 | not applicable |

The five per-segment comparisons are file offset, stored length, raw flags,
allocation size, and relocation-record count.  Raw `winedump` resource/name/byte
output is discarded and is not a committed artifact.

Final verification ran 53 research-tool tests successfully.  The repository suite
ran 529 tests successfully with 27 optional `python-docx` skips; `pypdf` was supplied
read-only from the bundled workspace dependency runtime for that run.  All research
scripts passed `py_compile`, the final diffs passed Git's whitespace check, and the
repository safety audit reported no finding after all 19 research-scope paths were
staged.  Ruff was not installed in either available Python runtime, so a Ruff result
could not be produced; this is a recorded toolchain limitation rather than a claimed
lint pass.

Review limitations that remain explicit:

- no vendor executable was run and no controlled Ami Pro oracle result exists yet;
- direct-filter behavior can be lossy and does not automatically describe the native
  Ami Pro reader/writer;
- absent direct xrefs can be callbacks or packed-core behavior;
- corpus aggregation strips content and uses syntactic layout proxies;
- modern NE tool availability does not make its loaded memory map correct;
- no claim derives semantics from a nearby string alone.
