# Ami Pro SAM interoperability specification

This directory is the project's public, implementation-independent description of
the Lotus Ami Pro SAM format. It is a living interoperability RFC: precise enough for
another author to build a reader, explicit about uncertainty, and designed to improve
without erasing how each conclusion was reached.

The current draft covers the version-4 text/binary family encountered by this project.
Version 3 is reported by secondary sources but is not yet represented by inspected
samples, so version-specific assumptions must stay visible.

## Documents

- [`sam-format.md`](sam-format.md) defines the container, record grammar, known
  commands and values, and the expected behavior of a preservation-oriented reader.
- [`evidence.md`](evidence.md) registers evidence sources and gives semantic claims
  stable IDs, confidence levels, and provenance.
- [`../docs/format-notes.md`](../docs/format-notes.md) explains current implementation
  decisions and renderer behavior. It is informative, not the format authority.
- [`../docs/research/`](../docs/research/) contains detailed reproducible analysis
  supporting some evidence-ledger entries.

## Status language

Every semantic mapping is labeled **confirmed**, **strong**, **tentative**,
**contradicted**, or **open** using the definitions in [`evidence.md`](evidence.md).
Syntax may be strongly established while the meaning of one or more fields remains
open. A conforming preservation reader should retain unknown fields and make no
stronger claim than the evidence supports.

`MUST`, `SHOULD`, and `MAY` describe this project's interoperability contract, not an
official Lotus standard. This independent specification is not affiliated with or
endorsed by the format's rights holders.

## Updating the RFC

A format change should include, in one reviewable change:

1. a new or updated claim in `evidence.md`;
2. the corresponding grammar or command-table change in `sam-format.md`;
3. links to detailed evidence and an explicit dependency analysis;
4. synthetic conformance fixtures when implementation behavior changes; and
5. compatibility notes for any changed conversion output.

Implementation code and tests can demonstrate that this toolkit behaves as intended,
but cannot by themselves establish what Ami Pro means.
