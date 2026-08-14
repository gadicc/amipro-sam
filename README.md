# Ami Pro SAM Toolkit

A preservation-oriented, open-source converter for Lotus Ami Pro 3.x `.SAM`
documents. It reads SAM directly: Ami Pro, Windows 3.1, Wine, DOSBox, and
LibreOffice are not required.

The first release prioritizes complete readable-text recovery, transparent
diagnostics, and safe reflow into modern formats. It supports self-contained
HTML, Markdown, plain text, ODT, PDF, a JSON intermediate representation, and
experimental DOCX output.

> **Alpha:** SAM is a mixed text/binary legacy format with only partial public
> documentation. Keep originals. Review conversion warnings before disposing of
> any source material.

## Features

- Conservative decoding with BOM/code-page detection and lossless preservation
  of undecodable bytes.
- A shared, renderer-independent document model for styles, paragraphs, runs,
  tables, images, unknown records, and source locations.
- Common character and paragraph formatting: fonts, size/color, bold, italic,
  underline, strikeout, super/subscript, alignment, spacing, and indents.
- Deterministic PDF text from fixed in-package preservation fonts, with bounded
  BMP Unicode fallback for Latin, Greek, Cyrillic, Hebrew, Arabic, Han, kana,
  and Hangul. Unsupported scalars remain visible as replacement characters.
- Text recovery from the main document, text frames, and table cells.
- Typed annotations, footnotes, and body/layout header/footer streams with raw
  placement records retained in the intermediate representation.
- Bounds-checked extraction of embedded BMP data, direct inert PNG previews for
  the validated WMF/DIB subset, and typed SHA-256-bearing preservation of Ami
  Draw SDW payloads.
- Grayscale/index previews for the narrow, validated 1-, 4-, and 8-bit Ami Draw
  companion subset; SDW vector geometry and unsupported companion depths remain
  explicit placeholders.
- Visible placeholders for unsupported WMF/SDW operations, OLE, equations,
  macros, and other unsupported objects.
- Directory inventory, batch conversion that continues after corrupt files, and
  stable diagnostics.
- No execution of macros, DDE, scripts, or OLE; no remote resource loading and
  no automatic following of document-controlled file paths.

See [compatibility](docs/compatibility.md) for current fidelity and
[format notes](docs/format-notes.md) for evidence and open questions.

## Installation

Python 3.10 or later is required.

```console
python -m pip install .
```

PDF support is included in the normal installation. DOCX is a stretch format
with an optional dependency:

```console
python -m pip install '.[docx]'
```

For local development:

```console
python -m pip install -e '.[dev,docx]'
pytest
ruff check .
```

## Quick start

Create a self-contained HTML file:

```console
amipro-sam convert document.sam --format html --output document.html
```

Other modern outputs use the same parsed document:

```console
amipro-sam convert document.sam --format markdown --output document.md
amipro-sam convert document.sam --format text --output document.txt
amipro-sam convert document.sam --format odt --output document.odt
amipro-sam convert document.sam --format pdf --output document.pdf
amipro-sam convert document.sam --format docx --output document.docx
```

`md` and `txt` are accepted as aliases. With one input, the format can be
inferred from a recognized output extension:

```console
amipro-sam convert document.sam --output document.pdf
```

Write plain text to standard output:

```console
amipro-sam convert document.sam --format text
```

Dump the bounded structured intermediate representation, including unknown
records, diagnostics, and explicit descriptors wherever safe output limits omit
additional entries:

```console
amipro-sam dump document.sam --format json --output document.json
```

Inventory one file or a directory:

```console
amipro-sam inspect document.sam
amipro-sam inspect ./documents --recursive --summary
amipro-sam inspect ./documents --recursive --summary --json
```

Batch conversion continues after an individual corrupt file and exits nonzero
if any input fails:

```console
amipro-sam convert ./documents --recursive --format html --output ./converted
```

Existing output files are not replaced by default. Pass `--force` to replace a
previous conversion; source inputs are protected even with `--force`. Batch
output-name collisions are rejected before any file is written.

Paths with spaces and non-ASCII characters are ordinary supported filesystem
paths; quote them in a shell as usual.

## Diagnostics and strict mode

HTML includes a diagnostic appendix by default. Use `--no-warning-summary` to omit
it from the presentation; diagnostics remain available through `inspect` and
`dump`.

By default the converter favors recovery: unsupported constructs become visible
placeholders and surrounding text remains available. Diagnostics classify
severity and preservation loss independently. Loss can be `semantic` (content
is retained but meaning, behavior, or placement is approximated) or `content`
(source material cannot be represented); `none` is an ordinary informational or
fully preserved recovery condition. `--strict` rejects semantic or content loss,
regardless of whether its diagnostic severity is info, warning, or error:

```console
amipro-sam convert document.sam --format pdf --strict --output document.pdf
```

`inspect --json` reports per-file `lossy` and `losses` fields. Aggregate summaries
also report `lossy_files`, loss categories, and severity counts. Bytes confined to
a validated indexed binary range do not become text-decoding losses.

Use `--encoding` only when a document's charset declaration is absent or wrong:

```console
amipro-sam convert document.sam --encoding cp1250 --format text
```

## Output expectations

HTML is a single UTF-8 file with inline CSS, embedded safe images, escaped source
content, and a restrictive Content Security Policy. ODT, PDF, and DOCX are
modern reflows. They are designed for legibility and preservation access, not
pixel-identical reproduction of Ami Pro pagination or printer metrics.

Raw SDW vector or companion bytes are never sent to a browser, embedded in an
office package or PDF, or passed to an external converter. Visual outputs receive
only fresh PNG data generated from the independently validated companion subset.

PDF embeds deterministic subsets of the bundled preservation fonts; it never
uses a font path supplied by a document or searches the host font collection.
The supported PDF subset is BMP-only. Bidirectional Hebrew and Arabic are
shaped and reordered conservatively at paragraph-line level, with original
bounded, sanitized logical visual-line text recorded as PDF `ActualText`;
extractor support for `ActualText`
varies, so JSON, text, ODT, or DOCX remain preferable when exact logical-text
extraction is the primary goal.

Markdown and text intentionally flatten layout that their formats cannot
represent. Unsupported data is marked rather than silently omitted.

## Security model

Treat every legacy document as untrusted. The toolkit:

- applies configurable file, line, record, table, embedded-asset, WMF, and SDW
  record/depth/point/dimension/pixel limits;
- validates every embedded offset and length before slicing bytes;
- does not invoke office software or external converters in production;
- never activates macros, OLE, DDE, external links, or document scripts;
- does not read document-referenced absolute, network, or relative paths;
- escapes document text before passing it to HTML or ReportLab markup;
- writes completed output through a temporary sibling before atomic replacement.

The original input is opened read-only and is never modified.

## Scope and provenance

The implementation was informed by public archival metadata, published
reverse-engineering work, an archived LGPL KOffice importer, locally owned Ami
Pro 3.1 installation media, and private regression documents. No proprietary
executables, help files, templates, fonts, manuals, or samples are distributed.
Synthetic fixtures contain invented content only.

The repository history contains no disassembly, decompilation, instrumentation,
or execution of an Ami Pro executable. Tests combine small invented SAM records,
seeded malformed-input/mutation cases, renderer and package assertions, PDF text
and raster checks, and an optional aggregate audit of the Git-ignored private
corpus. Some public format notes used by the project were themselves produced by
third-party reverse engineering; their uncertain fields remain labeled as such.

Lotus Ami Pro belongs to its respective rights holders. This independent project
is not affiliated with or endorsed by those rights holders. See [NOTICE](NOTICE).

## License

MIT. See [LICENSE](LICENSE).
