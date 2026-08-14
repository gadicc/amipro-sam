# Contributing

Contributions that improve recovery without concealing data loss are welcome.

## Development setup

```console
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,docx]'
pytest
ruff check .
```

The parser accepts untrusted legacy files. Keep bounds checks and resource
limits near any new length, offset, count, image, or nested structure. Never
execute macros, follow DDE links, activate OLE objects, or fetch remote assets.

## Fixtures and privacy

Do not submit proprietary Ami Pro files or personal documents. Instead:

1. Identify the smallest construct that reproduces the behavior.
2. Create a new synthetic SAM file containing invented text and metadata.
3. Remove names, addresses, identifiers, paths, document properties, and binary
   assets copied from the source.
4. Confirm that the original problem still occurs.
5. Add a short comment documenting what the synthetic fixture represents and
   how it was made.

Fixtures must be small enough to review as text. If a binary object is essential,
generate it with a documented open-source tool and record its license and SHA-256.
Do not derive image or prose content from Ami Pro installation media.

## Parser changes

Preserve unknown records and their source positions. A feature is not supported
until readable content, formatting, malformed input, and renderer behavior have
tests. New loss modes need stable diagnostic codes and compatibility notes.

File-format semantics also need a stable claim and provenance entry in
[`spec/evidence.md`](spec/evidence.md), followed by the corresponding grammar or
command update in [`spec/sam-format.md`](spec/sam-format.md). Implementation code,
synthetic fixtures, and generated converter output validate this toolkit but are not
independent evidence of Ami Pro behavior.

## Component boundaries

The converter, rendering oracle, reverse-engineering tools, and format specification
are separate components. Follow [`AGENTS.md`](AGENTS.md) for their canonical paths,
dependency rules, evidence workflow, and verification commands. Avoid combining
directory moves with behavioral changes, especially while another component has
unfinished work.
