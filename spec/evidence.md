# Evidence and provenance ledger

Status: living draft, 2026-08-14.

This ledger separates file-format facts from implementation choices. Stable claim IDs
are cited by [`sam-format.md`](sam-format.md); detailed investigations remain in
`docs/research/` rather than being duplicated here.

## Claim dimensions and confidence

Every claim identifies the kind of conclusion being scored:

- **grammar/structure**: bytes, delimiters, record shape, field order, ranges, and
  occurrence;
- **semantics**: what a record, field, or command represents; and
- **native behavior/rendering**: what Ami Pro does with it, including layout,
  typography, pagination, interaction, and visible output.

One source can support different dimensions unequally. Seeing a value in hundreds of
documents can confirm its occurrence without establishing its meaning. Two filters
that render the same guess do not establish native behavior. If parts of a statement
have different confidence or sources, they receive separate claim IDs.

| Level | Meaning |
|---|---|
| **confirmed** | Two genuinely independent sources support the exact claim and dimension after dependency analysis, or a controlled Ami Pro observation directly isolates it |
| **strong** | A complete reproducible static parser/serializer-to-use path or comparably strong observation, without independent behavioral confirmation |
| **tentative** | Partial or correlational evidence leaves plausible alternatives |
| **contradicted** | A reproducible observation falsifies the claim as stated |
| **open** | Available evidence does not distinguish among meanings |

Confidence applies to the exact statement in a claim. For example, a record's arity
can be strong while every field label and its exact rendering remain open. A
rendering conclusion is scoped to the observed application version, fonts, printer
driver, environment, fixture, and comparison method.

## Source registry

| Source ID | Kind | Description and independence limits |
|---|---|---|
| `PUB-PRONOM-X191` | Public registry | UK National Archives [PRONOM x-fmt/191](https://www.nationalarchives.gov.uk/pronom/x-fmt/191) identification record; useful for format identity, not detailed semantics |
| `PUB-KOFFICE-AMI` | Public reverse engineering | Ariya Hidayat's LGPL KOffice/KWord [filter tree](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/), explicitly [unfinished notes](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/FileFormat.txt/) and [limited status](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/status.html/); notes and code are one source family. The committed tests are sample documents, not a discovered native-rendering comparison harness |
| `PUB-BORN-AMI` | Published reverse engineering | Günter Born, [*Das AMI Pro Dateiformat (Version 3.0/4.0)*](https://s3-eu-west-1.amazonaws.com/gxmedia.galileo-press.de/supplements/233/galileocomputing_dateiformate_2.pdf); detailed secondary reverse engineering with no documented native-output comparison harness found in the chapter |
| `PUB-LOTUS-GUIDES` | Vendor user documentation | Publicly archived Lotus guides to [page setup](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10701.txt), [inserted layouts](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10702.txt), and [headers/footers](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10741.txt); user-visible concepts, not a byte-level specification |
| `OBS-INSTALL-31` | Local observation | Aggregate structural inspection of 13 SAM and 108 SDW files from lawfully owned Ami Pro 3.1 media; no proprietary content is committed |
| `OBS-PRIVATE-384` | Local aggregate corpus | Structure-only aggregation over 384 private SAM files; save provenance is incomplete and output suppresses content and identifying data |
| `STATIC-W4W-20260814` | Static executable research | Hash-gated, bounded analysis of the bundled W4W33F/W4W33T SAM reader/writer, documented in [`executable-format-re.md`](../docs/research/executable-format-re.md); the two modules are one subsystem/source and establish that converter's behavior, not necessarily native Ami Pro behavior |
| `ORACLE-AMI31` | Controlled behavior | Reserved source family for real Ami Pro 3.1 observations made by the local oracle. No claim currently cites this source; conclusions will be scoped to the recorded environment |
| `SYNTHETIC-TESTS` | Implementation validation | Invented fixtures validating this toolkit. Not independent evidence of Ami Pro behavior |
| `IMPL-AMIPRO-SAM` | Implementation state | Current Python parser/model/renderers. Useful for recording conformance, never a semantic source |

Multiple disassemblers applied to the same bytes are formatting cross-checks. A
private document may have passed through the same filter family being analyzed, so
`OBS-PRIVATE-384` and `STATIC-W4W-20260814` do not automatically satisfy the
independence requirement for **confirmed**.

KOffice is fit for basic grammar and semantic leads where its notes or executed code
path actually address the field. Its importer advertises text and basic formatting,
marks styles incomplete, leaves frames unsupported in its sample set, and does not
provide evidence of page or pixel fidelity. Born is broader, but remains a secondary
account. Neither source establishes native rendering without a controlled Ami Pro
comparison. Corpus observations establish occurrence, shape, range, and correlation;
they do not turn a field label into a visible behavior. No AbiWord-derived claim is
currently registered.

## Claim ledger

| Claim ID | Claim | Dimension and confidence | Sources | Notes |
|---|---|---|---|---|
| `SAM-CONTAINER-001` | A common version-4 SAM document is a mixed line-oriented text/binary stream with header records, `[edoc]`, optional indexed payloads, and `[Embedded]` | grammar/structure: confirmed | `OBS-INSTALL-31`, `OBS-PRIVATE-384`, `PUB-BORN-AMI` | Some documents omit binary/indexed material; one observed private file has a preamble. KOffice supports only the textual subset of this claim |
| `SAM-CHARSET-001` | `[charset]` value `82` with the observed description denotes Windows ANSI/CP1252, not code page 82 | semantics: confirmed | `OBS-INSTALL-31`, `OBS-PRIVATE-384`, `PUB-KOFFICE-AMI` | Other locales/code-page identifiers remain incompletely sampled |
| `SAM-STYLE-001` | `[tag]` defines a named paragraph style with observed subrecords including `[fnt]`, `[algn]`, `[spc]`, `[brk]`, `[line]`, `[spec]`, and `[nfmt]` | grammar/structure: confirmed; subrecord semantics: mixed/open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Only individually claimed subrecord meanings are established |
| `SAM-STYLE-FONT-001` | `[fnt]` begins with family, size in twips, packed BGR integer, and character flags; low flag bits select the documented common emphasis states | grammar: confirmed; low-bit semantics: confirmed; native metrics/appearance: open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Unknown/high bits must remain raw |
| `SAM-STYLE-ALIGN-001` | `[algn]` begins with flags, unit, all-indent, first-line position, and rest-lines position; low flag bits select left/right/center/justify | grammar: strong; low-bit semantics: strong; exact geometry/rendering: open | `PUB-KOFFICE-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | KOffice labels the fields but does not validate native layout; nondefault indent and high-flag semantics remain tentative/open |
| `SAM-STYLE-SPACING-001` | `[spc]` has at least five numeric fields; common trailing `1,100` is proposed as a structural/default-tightness pair | grammar: strong; semantics: tentative; native rendering: open | `PUB-KOFFICE-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | Corpus dominance by defaults does not establish visible effect; nondefault flags and tightness are open |
| `SAM-TEXT-001` | `[edoc]` begins document text and a standalone `>` closes the current text stream | grammar/structure: confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Nested multiline records have their own standalone close |
| `SAM-TEXT-PARAGRAPH-001` | Blank physical lines delimit paragraphs and consecutive nonblank storage lines concatenate without an invented character | semantics: strong; native paragraph behavior: open | `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Corpus word splits strongly support concatenation. KOffice inserts line breaks and is not supporting evidence; native behavior still needs an isolated oracle fixture |
| `SAM-ESCAPE-001` | `<<`, `<;>`, `<[>`, `@@`, and `</R>` encode literal `<`, `>`, `[`, `@`, and apostrophe respectively; slash/backslash four-byte families encode additional bytes | grammar/semantics: confirmed | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Decoding remains subject to the document charset |
| `SAM-INLINE-STYLE-001` | The common `+`/`-` punctuation commands toggle emphasis and `+@` through `+C` select paragraph alignment as catalogued in the RFC | grammar/semantics: confirmed; exact native appearance: open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Double/word underline may collapse in targets without those distinctions |
| `SAM-INLINE-FONT-001` | `<:f...>` carries a twip size, optional family, and optional RGB channels that change font properties | grammar: confirmed; semantics: strong; native metrics/appearance: open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Compact three-field forms occur in the corpus |
| `SAM-INLINE-FONT-RESET-001` | Empty `<:f>` forms and omitted font groups restore corresponding paragraph-style defaults | semantics: tentative; native behavior: open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | The KOffice notes describe reset behavior, but its importer does not provide a native comparison and compact-form effects cannot be established from occurrence alone |
| `SAM-INLINE-SPACING-001` | `<:S+-1>`, `<:S+-2>`, and `<:S+-3>` select single, 1.5, and double line spacing, while `<:S->` restores the style value | grammar: confirmed; semantics: strong; exact native spacing: open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Other bounded numeric values are retained but need stronger behavioral evidence |
| `SAM-INLINE-CONTROL-001` | `<:>` restores style character state, `<:s>` represents nonprinting spelling state, and canonical `<:p>` requests a page break | grammar: confirmed; semantics: strong; native behavior: open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Noncanonical `:p` payload semantics remain open |
| `SAM-INLINE-REGION-001` | `<:#f0,f1>` has two bounded numeric fields and `f1` correlates strongly with an active text/container measure | grammar/correlation: strong; coordinate semantics/rendering: open | `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | `f0` is not a general horizontal origin. Exact coordinate behavior needs the oracle |
| `SAM-INLINE-INDENT-001` | `<:If0,f1,f2,f3>` is a four-numeric-field record emitted and consumed atomically | grammar: strong; field semantics/rendering: open | `STATIC-W4W-20260814`, `OBS-PRIVATE-384` | Nonzero `f3` behavior remains open |
| `SAM-INLINE-DYNAMIC-001` | `:D`, `:P`, `:X`/`:X~`, and `:Z`/`:Z~` families encode date/page or dynamic-field/revision constructs | grammar: strong; semantics: tentative; native behavior: open | `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | Exact variants and most payload semantics remain open; non-execution is reader safety policy |
| `SAM-INLINE-CONTAINER-001` | `<:N...>`, `<:F...>`, `<:H...>`, and `<:h...>` open annotation, footnote, header, and footer streams closed by standalone `>` | grammar: strong; semantics: strong; native placement/rendering: open | `PUB-BORN-AMI`, `OBS-PRIVATE-384` | Footnote/footer corpus coverage is sparse or absent; fixtures are synthetic |
| `SAM-FRAME-001` | `[frm]` defines a frame and `<:tN>`/`<:AN>` forms associate body positions with indexed frame records | grammar: strong; anchor semantics: strong; native placement/z-order: open | `PUB-BORN-AMI`, `OBS-PRIVATE-384`, `STATIC-W4W-20260814` | KOffice explicitly left frames unsupported and is not a source for this claim; fixed/repeating bits remain open |
| `SAM-PAGE-001` | `[lay]` plus `[rght]`/`[lft]` records describe page size and right/odd versus left/even geometry in twips | grammar: confirmed; field semantics: strong; native pagination/rendering: open | `PUB-LOTUS-GUIDES`, `PUB-KOFFICE-AMI`, `PUB-BORN-AMI`, `OBS-PRIVATE-384` | KOffice's notes label fields, but its importer hardcodes target page geometry and does not behaviorally validate them; transitions and high bits remain open |
| `SAM-TABLE-001` | `[tbl]` fields 0/1 are row/column counts and `[data]` fields 0/1 are zero-based cell coordinates | grammar/semantics: strong; native layout/rendering: open | `STATIC-W4W-20260814`, `OBS-PRIVATE-384` | Corpus provenance prevents a confirmed promotion; many adjacent cell-style labels remain tentative/open |
| `SAM-EMBEDDED-001` | `[Embedded]` rows index primary asset and companion/preview offset-length pairs | grammar/semantics: confirmed | `PUB-BORN-AMI`, `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Direct range checks corroborate the row interpretation. KOffice ignores this structure and is not a source for the claim |
| `SAM-EMBEDDED-POINTER-001` | In the observed version-4 corpora, a final zero-padded decimal value locates the `[Embedded]` directory | grammar/semantics: confirmed for observed files; other variants: open | `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Direct byte-offset matches support decimal interpretation. Born describes an ASCII-hexadecimal locator; preserve this conflict until version/context is isolated |
| `SAM-ACTIVE-001` | SAM documents can contain macro, DDE, OLE, dynamic-field, and external-file/path constructs | occurrence: confirmed; exact byte semantics/native execution: open | `PUB-LOTUS-GUIDES`, `PUB-BORN-AMI`, `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Inert preservation is reader safety policy, not a claim about Ami Pro execution |
| `SAM-REVISION-001` | One exact `[revisions]` value of `0` is the observed no-revisions state | occurrence: confirmed; semantics: tentative; native behavior: open | `OBS-INSTALL-31`, `OBS-PRIVATE-384` | Nonzero, duplicate, and additional-field meanings remain open |

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
