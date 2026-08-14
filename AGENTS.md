# Project guidance

This repository has four first-class technical concerns and one planned consumer:

1. the preservation-oriented SAM converter;
2. the local Ami Pro rendering oracle;
3. interoperability and reverse-engineering research;
4. the implementation-independent SAM format specification; and
5. a future web application that consumes a stable converter interface.

Keep those concerns visibly separate. Shared repository configuration is allowed,
but production conversion must never acquire a runtime dependency on the oracle,
private corpora, proprietary media, or research tools.

## Component map

The current canonical paths are:

| Concern | Code and tools | Tests | Documentation |
|---|---|---|---|
| Converter | `src/amipro_sam/` | `tests/` except `test_oracle_*` | `README.md`, `docs/compatibility.md`, `docs/format-notes.md` |
| Rendering oracle | `src/amipro_oracle/`, `scripts/amipro-oracle`, `scripts/build-oracle-toolchain`, `toolchain/` | `tests/test_oracle_*` | `docs/amipro-oracle.md`, `docs/plans/amipro-oracle-plan.md` |
| Reverse engineering | `tools/research/` | `tools/research/test_*.py` | `docs/research/`, `docs/plans/executable-format-re-plan.md` |
| Format specification | `spec/` | Conformance tests live with the consumer they exercise | `spec/` |
| Future web app | not created yet | not created yet | `ROADMAP.md` |

`docs/architecture.md` describes the dependency boundaries and intended gradual
directory convergence. Do not create an empty `apps/web/` scaffold until the web
milestone begins.

## Change boundaries

- Keep converter changes inside the converter component unless a specification or
  compatibility update is also required.
- Keep oracle execution opt-in and local. The converter test suite must not require
  proprietary media, Windows, Wine, DOSBox, OCI tooling, or network access.
- Keep research inputs outside Git. Commit only bounded, reviewable evidence that
  passes the research safety audit.
- Do not use the converter implementation, its tests, or generated converter output
  as independent evidence for a file-format semantic.
- The future web app may depend on a public converter API. The converter must not
  depend on the web app.
- Avoid mixing path-only moves with behavioral changes. Move one component at a
  time, retain compatibility entry points where practical, update all links and
  manifests, and verify tests before and after the move.
- Do not move files being changed by unfinished work. Record the intended destination
  in `docs/architecture.md` and migrate after that work lands.

## Format evidence workflow

Treat `spec/` as the public description of the format, not as a description of what
the current parser happens to do.

At the end of every task, check whether the work established, weakened, contradicted,
or otherwise changed any fact about the SAM format. If it did, the task is not complete
until `spec/evidence.md` and `spec/sam-format.md` reflect that result. Pure refactors,
tooling changes, and renderer-only changes need no speculative RFC edit when they add
no format evidence.

For every new or changed semantic claim:

1. Give the claim a stable ID in `spec/evidence.md`.
2. Record the source IDs, dependency analysis, confidence, and important alternatives.
3. Update the relevant table or grammar in `spec/sam-format.md`.
4. Link detailed analysis rather than copying it into the specification.
5. Only then change parser/model/renderer behavior, with invented synthetic fixtures
   and compatibility notes where output changes.

Use these confidence terms consistently:

- **confirmed**: two genuinely independent sources after dependency analysis, or a
  controlled Ami Pro oracle observation that isolates the behavior;
- **strong**: a complete reproducible static path or comparably strong observation,
  but no independent behavioral confirmation;
- **tentative**: partial or correlational evidence with plausible alternatives;
- **contradicted**: reproducible evidence falsifies the claim as stated;
- **open**: available evidence does not discriminate among meanings.

Multiple tools decoding the same bytes are cross-checks, not independent sources.
KOffice notes and KOffice code are one source family. A private corpus may have
unknown save provenance and must not automatically be treated as independent of an
import/export filter. Synthetic tests validate our behavior; they do not establish
Ami Pro behavior.

When evidence conflicts, preserve both records, mark the older claim contradicted or
superseded, and explain the resolution. Do not silently upgrade confidence.

## Safety, privacy, and provenance

- Treat every SAM file as untrusted. Keep resource limits and bounds checks close to
  every new offset, length, count, nested structure, and renderer allocation.
- Never execute macros, DDE, OLE, external links, or document-controlled paths.
- Do not commit proprietary Ami Pro files, executables, documentation, templates,
  fonts, installation assets, or private documents.
- Synthetic fixtures must use invented content. Aggregate private-corpus results must
  exclude text, paths, names, identifiers, timestamps, and rare identifying groups.
- Oracle evidence must record its backend, environment/toolchain identity, input
  fixture identity, procedure, raw observation or content hash, and comparison
  method. Fake-backend output is never Ami Pro evidence.

## Verification

Run the narrow component tests while iterating, then the repository checks relevant
to the change. The normal full checks are:

```console
pytest
ruff check .
```

Research changes also require:

```console
pytest -q tools/research
python -m py_compile tools/research/*.py
python tools/research/safety_audit.py --repo .
```

Document-only restructuring still requires `git diff --check` and a review of every
relative link. Never stage unrelated work from another task.
