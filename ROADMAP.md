# Project roadmap

This roadmap builds a trustworthy format foundation first, then makes it easy for web
and office integrations to reuse that work without bundling the research environment.
Dates are deliberately omitted; each phase advances when its evidence and acceptance
gate are met.

## 0. Establish component and specification boundaries

Status: in progress.

- Keep converter, oracle, research, and specification ownership explicit.
- Maintain the living SAM RFC and provenance ledger under `spec/`.
- Let active oracle and reverse-engineering work land before path-only migrations.
- Then group converter/oracle tests and component documentation as described in
  `docs/architecture.md`.

Exit gate: contributors can identify the owner, evidence requirements, and narrow test
command for any change without reading the whole repository.

## 1. Complete the evidence loop

- Finish the reproducible Ami Pro 3.1 oracle with deterministic printer, environment,
  fixture, observation, and comparison manifests.
- Review the current executable-format findings without treating static filter behavior
  as native Ami Pro behavior.
- Run one-variable synthetic oracle experiments for the prioritized open fields.
- Promote, weaken, or contradict specification claims with stable evidence IDs.

Exit gate: at least one controlled real-oracle result is reproducible and linked from
the specification, and the current high-value table/paragraph findings have explicit
review dispositions.

## 2. Drive converter fidelity from the specification

- Build small invented conformance fixtures for every accepted semantic.
- Keep parser, intermediate model, and renderers separate so layout approximations do
  not contaminate decoded meaning.
- Add differential reports that compare normalized text, structure, geometry, and
  raster evidence rather than PDF bytes alone.
- Continue hardening malformed-input limits and preservation-loss reporting.

Exit gate: every implemented format semantic links to a claim ID, every output
approximation is disclosed, and real-oracle comparisons are opt-in and reproducible.

## 3. Stabilize a reusable conversion core

- Version the JSON intermediate representation and publish a schema.
- Define a library API for bytes/file to document, document to diagnostics/JSON, and
  renderer selection.
- Separate lightweight text/HTML/JSON paths from heavyweight PDF/ODT/DOCX dependencies.
- Publish a conformance fixture pack containing only invented, redistributable inputs
  and target-neutral expected structure.
- Decide whether write-back to SAM is in scope; do not let a serializer silently become
  a prerequisite for reliable import.

Exit gate: downstream tools can consume a documented API/schema without importing CLI,
oracle, or research internals.

## 4. Build the online service

Start with a serverless Next.js user interface and a narrow conversion API. As of this
roadmap, Vercel has official [Python](https://vercel.com/docs/functions/runtimes/python)
and [WebAssembly](https://vercel.com/docs/functions/runtimes/wasm) function support, so
Wasm is not required merely to deploy the existing Python converter.

Recommended sequence:

1. Prove a native Python Function using the lightweight parser plus one or two outputs,
   with explicit upload/output limits, timeouts, no persistence by default, and no
   oracle or research assets in the bundle.
2. Measure cold start, dependency bundle size, peak memory, conversion time, and common
   document sizes. Vercel's Python bundling does not automatically tree-shake unused
   dependencies, which makes renderer separation important.
3. In parallel, prototype browser-local parsing with Pyodide only if private, no-upload
   conversion is a product goal. Pyodide can run in browsers and Node, but binary
   extensions require Pyodide-specific builds and long work belongs in a Web Worker;
   see its [package FAQ](https://pyodide.org/en/stable/usage/faq.html) and
   [WebAssembly constraints](https://pyodide.org/en/stable/usage/wasm-constraints.html).
4. Consider a Rust parser/IR port only after measurements justify it. A small
   memory-safe core compiled to native and Wasm is a stronger long-term portability
   story than attempting to move PDF, font shaping, and office-package renderers into
   one browser Wasm bundle.

The likely useful split is browser/Wasm for parse, inspect, JSON, text, and possibly
self-contained HTML; server-side functions for heavyweight PDF, ODT, and DOCX. The
experiment should decide this rather than the directory structure pre-committing to it.

Exit gate: the service has an abuse model, bounded resource policy, privacy/retention
statement, deterministic deployment, end-to-end tests, and measured runtime costs.

## 5. Enable an integration ecosystem

- Document the stable IR and conformance suite for other languages.
- Offer a CLI/subprocess contract before committing to a native ABI.
- Explore LibreOffice and AbiWord import integrations once the core/schema are stable;
  upstream projects can reuse the specification and fixtures even if they do not reuse
  this Python implementation.
- Consider archival batch integrations, desktop wrappers, and a browser-only inspector.
- Treat a native plugin or shared-library ABI as its own security and maintenance
  project, not a thin packaging task.

Exit gate: at least one external consumer can implement or integrate SAM import using
only the public specification, invented fixtures, and stable converter interface.

## Longer-horizon possibilities

Useful future directions include lossless SAM normalization, a separately scoped SAM
writer, richer image/vector recovery, preservation metadata exports, and upstream
office-suite filters. These should remain behind the nearer goals of evidence quality,
safe parsing, documented fidelity, and a stable integration surface.
