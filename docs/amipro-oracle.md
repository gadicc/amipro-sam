# Local Ami Pro rendering oracle

The oracle is an opt-in, local test system for comparing this converter with Ami Pro 3.1 under
Windows 3.1. It is deliberately separate from the normal converter: public CI and ordinary
conversion never execute Ami Pro or require proprietary media.

The current milestone provides the safety/reproducibility scaffold, media validation, pinned OCI
recipe, fake CI backend, bounded process/state helpers, manifests, and structural/raster comparison.
The real Windows installer driver is not complete, and no real rendering result has been produced.
No current manifest is accepted as a fidelity baseline: eligibility remains fail-closed until the
real runner can verify media, runtime, image, process, and capture identities rather than trusting
self-asserted JSON fields.
See the [investigation and adversarial plan](plans/amipro-oracle-plan.md).

## Commands

Run from the repository root:

```console
./scripts/amipro-oracle doctor
./scripts/amipro-oracle bootstrap --win31-media PATH --amipro-media PATH
./scripts/amipro-oracle smoke
./scripts/amipro-oracle batch --input PATH --output PATH
./scripts/amipro-oracle compare --expected PATH --actual PATH
```

Add `--json` after the subcommand for machine-readable output. Exit statuses are:

| Code | Meaning |
| ---: | --- |
| 0 | success or equal comparison |
| 1 | job failure or comparison difference |
| 2 | invalid invocation |
| 3 | missing media or prerequisite |
| 4 | unsafe input, media, or cache integrity failure |
| 5 | deadline exceeded |
| 6 | backend or internal failure |

`doctor` does not create oracle state. It reports the current lawful blocker exactly:

```text
provide --win31-media PATH or set WIN31_MEDIA_DIR
```

The Ami path can likewise be set with `AMIPRO_MEDIA_DIR`. Media inventory opens files read-only,
rejects links and special files, detects concurrent changes, and records canonical hashes. The
current owned Ami floppy images are host-writable, which `doctor` reports; future container/guest
mounts must still expose them read-only.

## Fake backend for CI

The fake backend exercises orchestration without Windows, Ami Pro, DOSBox-X, or proprietary data:

```console
./scripts/amipro-oracle bootstrap --backend fake
./scripts/amipro-oracle smoke --backend fake --output ./fake-smoke
./scripts/amipro-oracle batch --backend fake --input tests/fixtures --output ./fake-batch
./scripts/amipro-oracle compare \
  --expected ./fake-smoke \
  --actual ./fake-smoke
```

Fake manifests always say `"backend": "fake"` and `"baseline_eligible": false`. Their synthetic
PS/PDF/PNG files test plumbing only and must never become fidelity baselines.

## Toolchain

The locked rootless OCI recipe contains only open-source tools. Build it with:

```console
./scripts/build-oracle-toolchain
```

This operation needs public network access for the digest-pinned Debian snapshot and the pinned
DOSBox-X Git commit. It does not download or accept proprietary media. The recipe verifies the
full DOSBox-X commit and tree, pins fidelity-relevant packages, and embeds the complete installed
package inventory. The local image record is written below `.amipro-oracle/` and is ignored by
Git. See [toolchain details](../toolchain/README.md).

At this checkpoint the corrected recipe is tracked, but a final local image digest has not been
recorded; `doctor` therefore reports `missing-locked-toolchain` until the build completes.

The build fixes its source epoch and rewrites image timestamps. It also fails if the compiled
DOSBox-X configuration or linked libraries expose pcap, SLIRP, SDL_net, modem, or IPX support.
The runtime boundary must still use `--network=none`; compile-time removal and OCI isolation are
independent controls.

GitHub marks the selected DOSBox-X release mutable and its commit is unsigned. Content hashes make
the build repeatable and fail closed on drift, but do not manufacture publisher authentication.
That provenance limitation remains in the lock and manifests.

Redistributing the image requires a separate source/notices and license review: DOSBox-X is GPLv2,
the Debian Ghostscript build includes AGPL-covered code, and the selected DOSBox-X README contains
an unusual jurisdiction-specific age-verification notice. The scaffold records these as release
gates rather than making a legal conclusion. Proprietary guest/runtime files never enter the
image.

## State and evidence

The default local root is `.amipro-oracle/`; override it with `--oracle-home` or
`AMIPRO_ORACLE_HOME`. Real/proprietary state under `/tmp`, the repository root, or the user's home
root is rejected. Fake test state may use a temporary directory because it contains no proprietary
bytes.

Every real job will use an empty capture directory and `parallel1=file timeout:2000`. It will not
use an explicit `file:`/`append:` target or an `open*` host hook. Exactly one fresh capture is
required; split captures are preserved as failure evidence. An outer monotonic host deadline is
authoritative because DOSBox-X `-time-limit` is emulated-time based and exits zero when triggered.

The checked-in OCI invocation builder additionally fixes `--pull=never`, `--network=none`, a
read-only root, dropped capabilities, no-new-privileges, resource limits, private IPC, rootless
user mapping, and a single writable `/oracle/job` bind. Media binds are read-only. Each invocation
has a unique name and cidfile so a future real supervisor can explicitly stop, kill, and remove the
container after client failure; killing only the `podman run` process group is not sufficient.

The real-runner acceptance contract requires every failure to retain its state trace, generated
configuration, stdout/stderr, screenshots, staged name map, captures, artifact hashes, and
manifest. The fake backend already retains its available partial artifacts and diagnostics; real
screenshot/state capture remains part of the media-blocked installer milestone. Troubleshooting
starts with captured evidence, not manual guest inspection.

The prior failed Wine attempt has been copied, hash-verified, and retained in a local ignored
content-addressed evidence namespace. It is not part of the oracle runtime, and no Wine prefix or
proprietary executable was copied into source.

## What is still required

To cross the lawful bootstrap gate, supply the exact local path to owned Windows 3.1 media:

```console
./scripts/amipro-oracle bootstrap \
  --win31-media /absolute/path/to/owned/windows-3.1-media \
  --amipro-media '/home/dragon/Downloads/Ami Pro 3.1 (3.5)'
```

Do not add media to the repository. Bootstrap must also find a suitable owned Windows 3.1
PostScript stack—normally `PSCRIPT.DRV` plus a built-in model or `*.WPD` definition—or stop for an
exact additional licensed-media path. It will not fetch a driver, WPD, PPD, or font.
