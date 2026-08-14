# Evidence and provenance ledger

Status: living draft, 2026-08-14.

This ledger separates file-format facts from implementation choices. Stable claim IDs
are cited by [`sam-format.md`](sam-format.md); detailed investigations remain in
`docs/research/` rather than being duplicated here.

## Confidence scale

| Level | Meaning |
|---|---|
| **confirmed** | Two genuinely independent sources after dependency analysis, or a controlled Ami Pro oracle observation that isolates the behavior |
| **strong** | A complete reproducible static parser/serializer-to-use path or comparably strong observation, without independent behavioral confirmation |
| **tentative** | Partial or correlational evidence leaves plausible alternatives |
| **contradicted** | A reproducible observation falsifies the claim as stated |
| **open** | Available evidence does not distinguish among meanings |

Confidence applies to the exact statement in a claim. For example, a record's arity
can be strong while every field label remains open.

## Source registry

| Source ID | Kind | Description and independence limits |
|---|---|---|
| `PUB-PRONOM-X191` | Public registry | UK National Archives [PRONOM x-fmt/191](https://www.nationalarchives.gov.uk/pronom/x-fmt/191) identification record; useful for format identity, not detailed semantics |
| `PUB-KOFFICE-AMI` | Public reverse engineering | Ariya Hidayat's LGPL KOffice/KWord [format notes and importer](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/); notes and code are one source family |
| `PUB-BORN-AMI` | Published reverse engineering | Günter Born, *Das AMI Pro Dateiformat (Version 3.0/4.0)*; a secondary reverse-engineering source cited by the implementation notes |
| `PUB-LOTUS-GUIDES` | Vendor user documentation | Publicly archived Lotus guides to [page setup](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10701.txt), [inserted layouts](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10702.txt), and [headers/footers](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10741.txt); user-visible concepts, not a byte-level specification |
| `OBS-INSTALL-31` | Local observation | Aggregate structural inspection of 13 SAM and 108 SDW files from lawfully owned Ami Pro 3.1 media; no proprietary content is committed |
| `OBS-PRIVATE-384` | Local aggregate corpus | Structure-only aggregation over 384 private SAM files; save provenance is incomplete and output suppresses content and identifying data |
| `STATIC-W4W-20260814` | Static executable research | Hash-gated, bounded analysis of the bundled W4W33F/W4W33T SAM reader/writer, documented in [`executable-format-re.md`](../docs/research/executable-format-re.md); the two modules are one subsystem/source |
| `ORACLE-AMI31` | Controlled behavior | Reserved source family for real Ami Pro 3.1 observations made by the local oracle. No claim currently cites this source |
| `SYNTHETIC-TESTS` | Implementation validation | Invented fixtures validating this toolkit. Not independent evidence of Ami Pro behavior |
| `IMPL-AMIPRO-SAM` | Implementation state | Current Python parser/model/renderers. Useful for recording conformance, never a semantic source |

Multiple disassemblers applied to the same bytes are formatting cross-checks. A
private document may have passed through the same filter family being analyzed, so
`OBS-PRIVATE-384` and `STATIC-W4W-20260814` do not automatically satisfy the
independence requirement for **confirmed**.

## Claim ledger

| Claim ID | Claim | Confidence | Sources | Notes |
|---|---|---|---|---|
| `SAM-CONTAINER-001` | A common version-4 SAM document is a mixed line-oriented text/binary stream with header records, `[edoc]`, optional indexed payloads, `[Embedded]`, and a decimal directory offset | confirmed | `OBS-INSTALL-31`, `OBS-PRIVATE-384`, `PUB-KOFFICE-AMI`, `PUB-BORN-AMI` | Some documents omit binary/indexed material; one observed private file has a preamble |
| `SAM-CHARSET-001` | `[charset]` value `82` with the observed description denotes Windows ANSI/CP1252, not code page 82 | confirmed | `OBS-INSTALL-31`, `OBS-PRIVATE-384`, `PUB-KOFFICE-AMI` | Other locales/code-page identifiers remain incompletely sampled |
| `SAM-STYLE-001` | `[tag]` defines a named paragraph style with observed subrecords including `[fnt]`, `[algn]`, `[spc]`, `[brk]`, `[line]`, `[spec]`, and `[nfmt]` | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Only a subset of subrecord semantics is mapped |
| `SAM-STYLE-FONT-001` | `[fnt]` begins with family, size in twips, packed BGR integer, and character flags; low flag bits select the documented common emphasis states | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Unknown/high bits must remain raw |
| `SAM-STYLE-ALIGN-001` | `[algn]` begins with flags, unit, all-indent, first-line position, and rest-lines position; low flag bits select left/right/center/justify | strong | `PUB-KOFFICE-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | Exact nondefault indent and high-flag semantics remain tentative/open |
| `SAM-STYLE-SPACING-001` | `[spc]` has at least five numeric fields; common trailing `1,100` acts as a neutral structural/default-tightness pair | tentative | `PUB-KOFFICE-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | Nondefault spacing flags and tightness are open |
| `SAM-TEXT-001` | `[edoc]` begins document text, blank physical lines delimit paragraphs, nonblank storage lines concatenate, and a standalone `>` closes the stream | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Nested multiline records have their own standalone close |
| `SAM-ESCAPE-001` | `<<`, `<;>`, `<[>`, `@@`, and `</R>` encode literal `<`, `>`, `[`, `@`, and apostrophe respectively; slash/backslash four-byte families encode additional bytes | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Decoding remains subject to the document charset |
| `SAM-INLINE-STYLE-001` | The common `+`/`-` punctuation commands toggle emphasis and `+@` through `+C` select paragraph alignment as catalogued in the RFC | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Double/word underline may collapse in targets without those distinctions |
| `SAM-INLINE-FONT-001` | `<:f...>` changes font properties using a twip size, optional family, and optional RGB channels; empty groups restore style defaults | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Compact three-field forms occur in the corpus |
| `SAM-INLINE-SPACING-001` | `<:S+-1>`, `<:S+-2>`, and `<:S+-3>` select single, 1.5, and double line spacing, while `<:S->` restores the style value | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Other bounded numeric values are retained but need stronger behavioral evidence |
| `SAM-INLINE-CONTROL-001` | `<:>` restores style character state, `<:s>` represents nonprinting spelling state, and canonical `<:p>` requests a page break | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Noncanonical `:p` payload semantics remain open |
| `SAM-INLINE-REGION-001` | `<:#f0,f1>` has two bounded numeric fields and `f1` correlates strongly with an active text/container measure | strong | `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | `f0` is not a general horizontal origin; its meaning is open. Exact coordinate behavior needs the oracle |
| `SAM-INLINE-INDENT-001` | `<:If0,f1,f2,f3>` is a four-numeric-field record emitted and consumed atomically | strong | `STATIC-W4W-20260814`, `OBS-PRIVATE-384` | Field order/meaning and nonzero `f3` behavior remain open |
| `SAM-INLINE-DYNAMIC-001` | `:D`, `:P`, `:X`/`:X~`, and `:Z`/`:Z~` families encode date/page or dynamic-field/revision constructs | tentative | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | Exact variants and most payload semantics remain open; non-execution is reader safety policy |
| `SAM-INLINE-CONTAINER-001` | `<:N...>`, `<:F...>`, `<:H...>`, and `<:h...>` open annotation, footnote, header, and footer streams closed by standalone `>` | strong | `PUB-BORN-AMI`, `PUB-KOFFICE-AMI`, `OBS-PRIVATE-384` | Footnote/footer corpus coverage is sparse or absent; fixtures are synthetic |
| `SAM-FRAME-001` | `[frm]` defines a frame and `<:tN>`/`<:AN>` forms associate body positions with indexed frame records | strong | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | Anchor/fixed/repeating placement bits and exact z-order remain open |
| `SAM-PAGE-001` | `[lay]` plus `[rght]`/`[lft]` records describe page size and right/odd versus left/even geometry in twips | confirmed | `PUB-LOTUS-GUIDES`, `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Some transition and high-bit meanings remain open |
| `SAM-TABLE-001` | `[tbl]` fields 0/1 are row/column counts and `[data]` fields 0/1 are zero-based cell coordinates | strong | `STATIC-W4W-20260814`, `OBS-PRIVATE-384` | Corpus provenance prevents a confirmed promotion; many adjacent cell-style labels remain tentative/open |
| `SAM-EMBEDDED-001` | `[Embedded]` rows index asset and preview offset/length pairs, and the final zero-padded decimal value locates the directory | confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Offsets require validation against the correct stream origin |
| `SAM-ACTIVE-001` | SAM documents can contain macro, DDE, OLE, dynamic-field, and external-file/path constructs | confirmed | `PUB-LOTUS-GUIDES`, `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Exact byte fields are incomplete; inert preservation is reader safety policy, not a claim about Ami Pro execution |
| `SAM-REVISION-001` | One exact `[revisions]` value of `0` is the observed no-revisions state | tentative | `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Nonzero, duplicate, and additional-field meanings remain open |

## Oracle evidence record

When the real oracle produces a finding, add a source record containing:

- a stable observation ID and date;
- oracle backend and exact toolchain/environment manifest;
- an invented minimal SAM fixture hash and the source text needed to reproduce it;
- the single variable changed from the control;
- Ami Pro output artifact hashes plus bounded normalized measurements;
- comparison method and tolerances;
- repetitions, controls, and any nondeterminism; and
- the claim IDs supported, weakened, contradicted, or left unresolved.

Fake-backend output can test the harness but MUST NOT appear as `ORACLE-AMI31`
evidence. Pixel or PDF byte equality alone is not a semantic conclusion; preserve
the raw observation and document the inference separately.
