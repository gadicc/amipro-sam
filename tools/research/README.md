# Ami Pro interoperability research tools

These scripts inspect a lawfully supplied Ami Pro installation without copying,
extracting, executing, or modifying vendor assets.  They use only Python's standard
library, except that the optional evidence decoder adapters call locally installed
command-line tools.  Generated repository artifacts contain hashes and bounded
structural metadata, not executable or resource bytes.

## Safety boundary

- The payload is volatile input.  Set `AMIPRO_PAYLOAD_DIR` for every run; never copy
  it into this repository.
- `inventory.py` scans only direct children of that directory.  It opens regular,
  non-symlink files with `O_NOFOLLOW`, hashes and parses the same descriptor, checks
  file identity before and after reading, and requires the known `AMIPRO.EXE` size
  and SHA-256 trust anchor.
- `evidence.py` applies a second size/SHA-256 gate from the manifest.  It refuses
  iterated/self-loaded segment mappings and caps each packet at 64 raw bytes and 32
  instructions per decoder.  Token hits and little-endian word matches are leads,
  not semantic cross-references.
- Resource names and bodies are omitted.  A version probe may execute `wine
  --version`, but no vendor module is executed through Wine.  Decoder subprocesses
  receive only a capped temporary byte window.
- `corpus_layout_shapes.py` reads an explicitly supplied private corpus and emits
  only bounded numeric aggregates.  It omits paths, names, text, raw records,
  timestamps, source metadata, and corpus hashes, and suppresses rare histogram
  groups.  It reuses the converter's bounded decoder/parser for section framing and
  page geometry, then scans paragraph-layout commands and raw table records
  independently of the semantic table model.  Its output is correlation evidence,
  never an oracle result.
- `winedump_crosscheck.py` invokes only `winedump dump -x` after verifying the
  exact manifested tool and module hashes.  It retains bounded output in memory,
  compares only header/segment invariants, re-hashes both inputs afterwards, and
  serializes neither raw tool output nor resource/name data.
- `safety_audit.py` examines Git-visible blobs and deliberately does not traverse an
  ignored `mydocs/` tree.  Run it before and after staging research artifacts.

## Reproduce the manifest

From the repository root:

```sh
# Set AMIPRO_PAYLOAD_DIR to the volatile extracted payload outside this repository.
python tools/research/inventory.py \
  --payload-dir "$AMIPRO_PAYLOAD_DIR" \
  --output /tmp/amipro-module-manifest.json
cmp docs/research/module-manifest.json /tmp/amipro-module-manifest.json
```

The committed manifest is intentionally nonrecursive: it records 134 signature-
selected NE containers among the payload root's direct regular files.  A separate
read-only reconnaissance found nine additional NE containers below `DIALOGED/` and
`SPELL/`; those UI/spelling support modules were outside the committed plan's flat
inventory scope and were not used for format claims.

## Search and make a bounded packet

```sh
python tools/research/evidence.py \
  --manifest docs/research/module-manifest.json \
  --module W4W33T.DLL \
  search '[tbl]' '[h]' '[w]' '[data]'

python tools/research/evidence.py \
  --manifest docs/research/module-manifest.json \
  --module W4W33T.DLL \
  packet --claim-id re-tbl-01 --segment 1 --offset 0x7c6e \
  --byte-count 64
```

`ndisasm`, `cstool`, and GNU `objdump` output are convenience views of the same
bounded bytes.  They do not understand the NE container or apply its relocations;
the packet's relocation annotations and the analyst's call-path review remain
essential.

## Cross-check NE metadata

The three modules used directly by the ledger can be compared against the exact
`winedump` recorded in the manifest:

```sh
for module in AMIPRO.EXE W4W33F.DLL W4W33T.DLL; do
  python tools/research/winedump_crosscheck.py \
    --manifest docs/research/module-manifest.json \
    --payload-dir "$AMIPRO_PAYLOAD_DIR" \
    --module "$module"
done
```

This validates stored NE metadata only.  In particular, agreement does not decode
or validate `AMIPRO.EXE`'s 208 iterated/self-loaded segment mappings.

## Reproduce private-corpus aggregates

The command does not echo or serialize the supplied directory:

```sh
python tools/research/corpus_layout_shapes.py mydocs \
  > /tmp/amipro-corpus-layout-aggregates.json
```

The corpus has no committed expected-hash inventory.  The reader detects mutation
during each file read, but reproducing a historical corpus run also requires an
out-of-band private hash inventory owned by the operator.

The report covers exact-arity `<:#...>` and `<:I...>` command shapes plus raw
`[tbl]`, `[h]`, `[w]`, and `[data]` record families.  It retains all bounded
four-field `:I` values, including nonzero fourth-field values if present, rather
than building a current corpus invariant into the grammar.

## Verify the tools and staging boundary

```sh
pytest -q tools/research
python -m py_compile tools/research/*.py
python tools/research/safety_audit.py --repo .
```

`safety_audit.py` returns nonzero for known executable/archive signatures, private
SAM documents, vendor-like asset names, and conservatively selected installation
extensions.  It is a staging backstop, not proof that arbitrary extracted raw bytes
are non-proprietary; the staged diff still requires human review.
