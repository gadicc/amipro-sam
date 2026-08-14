# Local Ami Pro rendering oracle

The oracle is an opt-in, local test system for comparing this converter with Ami Pro 3.1 under
Windows 3.1. It is deliberately separate from the normal converter: public CI and ordinary
conversion never execute Ami Pro or require proprietary media.

The current milestone provides a real, bounded Windows 3.1 Setup driver for the exact supplied
six-floppy profile plus an independent, media-free Program Manager boot/clean-exit gate. The gate
has produced a content-addressed **Windows-ready base**. Ami Pro installation is still required;
no real rendering result has been produced, and no current manifest is accepted as a fidelity
baseline. See the [investigation and adversarial plan](plans/amipro-oracle-plan.md).

## Commands

Run from the repository root:

```console
./scripts/amipro-oracle doctor
./scripts/amipro-oracle bootstrap --confirm-proprietary-media-rights
./scripts/amipro-oracle boot-probe --confirm-proprietary-media-rights
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

`doctor` does not create oracle state. Pass media paths explicitly or put the two allowlisted values
in the ignored `.env.local`; `scripts/amipro-oracle` loads this file without executing it:

```dotenv
WIN31_MEDIA_DIR="/absolute/path/to/windows-3.1-media"
AMIPRO_MEDIA_DIR="/absolute/path/to/Ami Pro 3.1 media"
```

Explicit `--win31-media` and `--amipro-media` arguments take precedence. Media inventory opens files read-only,
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
has a unique name and cidfile. Host UI actions first verify that exact CID and its instance label;
cleanup targets that identity rather than a reusable name. Killing only the `podman run` process
group is not treated as sufficient container cleanup.

The Windows installer and boot-probe phases retain their state traces, generated configurations,
bounded stdout/stderr, changed-screen archives, observer status, raw and normalized runtime trees,
and process cleanup results. The boot gate additionally retains the accepted Program Manager and
Exit Windows frames. Stable checkpoint manifests exclude volatile job paths/timing; returned
results name the ignored evidence jobs that back them. Troubleshooting starts with captured
evidence, not manual guest inspection.

The prior failed Wine attempt has been copied, hash-verified, and retained in a local ignored
content-addressed evidence namespace. It is not part of the oracle runtime, and no Wine prefix or
proprietary executable was copied into source.

## Windows bootstrap and boot gate

After building the locked toolchain, run the exact supplied-media phase with an explicit affirmation
that you have the right to use both local proprietary media sets:

```console
./scripts/amipro-oracle bootstrap \
  --confirm-proprietary-media-rights \
  --win31-media /absolute/path/to/owned/windows-3.1-media \
  --amipro-media /absolute/path/to/owned/ami-pro-3.1-media
```

Do not add media to the repository. A successful `bootstrap` means “Windows install candidate
created and statically verified,” not “oracle ready.” Promote that candidate through the separate,
media-free boot gate:

```console
./scripts/amipro-oracle boot-probe --confirm-proprietary-media-rights
```

If more than one install candidate exists, add `--checkpoint-key HASH`. The boot job exposes only a
disposable clone, not either media source or the cache. It requires a stable Program Manager frame,
the Exit Windows confirmation frame, a clean return from `WIN.COM`, and a post-run tree seal before
publishing a Windows-ready base.

The exercised local run on 2026-08-14 promoted parent checkpoint
`9d425d232367a64014b00eb98e5950a068d310475908e74b961505bf5f562cb9` to Windows-ready runtime
`efab02fe92a782e9d3a59540d7b8caddbff2740cbbee9a9cf1285654d8e83bd3`. The container exited zero
without timeout after 34.25 seconds; the sealed runtime contains 215 files totaling 8,502,865
bytes. The accepted Program Manager and Exit Windows screenshots hash to
`9030dc0024600f145008a52f15c8addde74e22711536d117b46c1bb3f1b8bac1` and
`0938a62a7d80d6feae2f80db5cf40515479f49444ebe54264f60af259e289df4`, respectively. A second
invocation integrity-checked and reused the same cache without starting the guest. These
observations prove the Windows lifecycle gate, not Ami Pro fidelity.

The supplied Windows set contains `PSCRIPT.DRV` and a built-in PostScript model, but printer setup
remains a later phase. Nothing fetches a driver, WPD, PPD, font, Windows, or Ami Pro bytes.
