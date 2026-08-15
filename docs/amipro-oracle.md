# Local Ami Pro rendering oracle

The oracle is an opt-in, local test system for comparing this converter with Ami Pro 3.1 under
Windows 3.1. It is deliberately separate from the normal converter: public CI and ordinary
conversion never execute Ami Pro or require proprietary media.

The current milestone provides a real, bounded Windows 3.1 Setup driver, an independent
Program Manager boot/clean-exit gate, and an exact-dialog Ami Pro 3.1 installer. These phases have
produced content-addressed **Windows-ready**, **Ami Pro install-candidate**, and **Ami Pro-ready**
bases. The separate launch/clean-exit and invented-document gates are exercised: Ami Pro natively
saved and then twice reopened the exact invented fixture, visibly displayed both lines, and exited
cleanly. This is controlled native text-presence evidence, not a typography or print-fidelity
baseline; no current manifest is accepted as a fidelity baseline. See the
[investigation and adversarial plan](plans/amipro-oracle-plan.md).

## Commands

Run from the repository root:

```console
./scripts/amipro-oracle doctor
./scripts/amipro-oracle bootstrap --confirm-proprietary-media-rights
./scripts/amipro-oracle boot-probe --confirm-proprietary-media-rights
./scripts/amipro-oracle install-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle launch-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle smoke --confirm-proprietary-media-rights
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

The Windows installer, boot-probe, Ami Pro installer, launch gate, and document smoke retain their
state traces, generated configurations, bounded stdout/stderr, changed-screen archives, observer
status, runtime trees, and process cleanup results. The boot gate retains the accepted Program
Manager and Exit Windows frames. The Ami Pro installer retains all seven recognized installer
states plus the post-install Program Manager and Exit Windows frames. The document smoke retains
the exact printer-warning, document-ready, Program Manager, and Exit Windows frames. Stable
checkpoint manifests exclude volatile job paths/timing; returned results name the ignored
evidence jobs that back them. Troubleshooting starts with captured evidence, not manual guest
inspection.

The prior failed Wine attempt has been copied, hash-verified, and retained in a local ignored
content-addressed evidence namespace. It is not part of the oracle runtime, and no Wine prefix or
proprietary executable was copied into source.

## Windows bootstrap, boot gate, and Ami Pro install

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

Install Ami Pro only into a disposable clone of that Windows-ready runtime:

```console
./scripts/amipro-oracle install-amipro --confirm-proprietary-media-rights
```

If more than one Windows-ready runtime exists, add `--runtime-key HASH`. The installer mounts only
the verified flattened eight-floppy source read-only and the disposable job writable. It advances
only after matching seven exact, cursor-free dialog-title crops, then separately proves Program
Manager, confirms Exit Windows, validates the DOS return sentinel, checks `AMIPRO.EXE` and the
installer-created directory/INI topology, and seals a new cache. It does not accept a flat payload
copy as an installation.

The exercised run on 2026-08-14 promoted Windows-ready parent
`efab02fe92a782e9d3a59540d7b8caddbff2740cbbee9a9cf1285654d8e83bd3` to Ami Pro install candidate
`c7c79b26e9779a3c2f95b00c8f2301e95523cde960d1e287aacc79aa9dee6745`. The container exited zero
without timeout after 223.88 seconds. The sealed tree contains 924 files in 14 directories totaling
28,946,822 bytes; installed `AMIPRO.EXE` matches
`555506d1558d61579d5c6fee8bf5fa9d960aa05a20a5d171240ac2e0ea73cbbd`. The post-install Program
Manager and Exit Windows frames hash to
`ab1cf3925d8f14986f27b5ea70a0f333754ee8271286d0e9f4bd1e2c65165a92` and
`103131c51e5a51bca7dc5c371eecd95e2abdaa014ec6f3518266a1a1f195761f`. Evidence job
`install-amipro-c7c79b26e977-a9gyaes6` remains local and ignored. A second invocation verified and
reused the cache without executing Setup. This proves the installation checkpoint, not that Ami
Pro launches, renders, or prints correctly.

Promote the install candidate through the separate, media-free lifecycle gate:

```console
./scripts/amipro-oracle launch-amipro --confirm-proprietary-media-rights
```

If more than one install candidate exists, add `--checkpoint-key HASH`. Direct launch through
`WIN.COM C:\AMIPRO\AMIPRO.EXE` predictably reports that no printer driver is available and uses
screen formatting; the current profile requires that exact warning, dismisses it, requires the
blank untitled editor with the warning absent, closes Ami Pro, recognizes minimized Program
Manager, and confirms Exit Windows. Only the disposable job is mounted, and no source media or
cache is exposed to the guest.

The exercised gate promoted install candidate
`c7c79b26e9779a3c2f95b00c8f2301e95523cde960d1e287aacc79aa9dee6745` to Ami Pro-ready runtime
`a1613ad18f592516bef907ec04d608cf64a3bdf63ea2e2f824aa7690a273d9c0`. The container exited zero
without timeout after 21.87 seconds. Its sealed tree contains 925 files in 14 directories totaling
28,952,075 bytes. The accepted printer-warning, blank-editor, minimized-Program-Manager, and
Exit-Windows frames hash to
`527b8c7bc7fb0240867ffa2381d4c5b1540019f55b1689ab01c17f79a427db0e`,
`8444ee8afdccf7e014b4bb0bafc02d70bc2ed1cf83ab9a6f1e56bd3dce29dd0b`,
`87fec483c0dcb4575be61b1eef6f953337e615a06bf870b892bb1da7640293c3`, and
`f01b1aa3374d0be4f4b47354c0b1c1712e7d88ce02a577a74711659f19aab902`. Evidence job
`launch-amipro-a1613ad18f59-ssfqbjiz` remains local and ignored. Cache reuse was verified without
launching Ami Pro again. This proves the application lifecycle gate under screen formatting, not
document fidelity or printing behavior.

The next gate opens the invented, text-only fixture as the fixed guest name `SMOKE.SAM`:

```console
./scripts/amipro-oracle smoke --confirm-proprietary-media-rights
```

If more than one Ami Pro-ready runtime exists, add `--runtime-key HASH`. The real command does not
accept an arbitrary output directory: it writes evidence below the isolated oracle home and mounts
only that disposable job. It verifies the fixture through a no-follow, mutation-detecting read and
requires canonical CRLF, a version-4 text envelope, and a self-consistent `[Embedded]` directory
offset before starting the guest. The checked-in fixture is the exact output of Ami Pro's Save As
command after typing the two invented lines `NATIVE SMOKE DOCUMENT` and `INVENTED CONTENT ONLY`.
It is 4,584 bytes, hashes to
`bab52c077acf1cd67fde5fa285ffacd81febca8fc8da0e16c73a8bcf24ff0aa1`, and points its trailer at
byte 4,562.

Success requires the exact `SMOKE.SAM` title, at least 256 dark pixels in the expected upper
document body, rejection of the exact retained loading-hourglass crop, an unchanged staged-source
hash, clean Ami Pro and Windows return sentinels, valid observer evidence, and a clean bounded
container exit. The known ready state includes a text insertion caret, so readiness does not
incorrectly require an empty center crop. It produces a local `document-smoke-passed` result with
`baseline_eligible: false`; it does not promote another base or print.

The exercised production smoke on 2026-08-15 used Ami Pro-ready runtime
`a1613ad18f592516bef907ec04d608cf64a3bdf63ea2e2f824aa7690a273d9c0` and evidence job
`smoke-document-cjt8ea3j`. The container exited zero without timeout after 24.59 seconds; the full
state machine completed in 30.07 seconds. Its ready screenshot hashes to
`7cbbcdea5ebd451f287b9e3222ade59258747e7bb770e6a7c234a704d7f62b4c` and contains 1,027 dark
pixels in the body crop, compared with 34 in the retained blank intermediate frame. The exact
ready screenshot was independently observed in the preceding direct-open attempt, whose timeout
was isolated to the former stale title/caret predicate. The staged document tree digest is
`49ba5a1b93c2d96e03aa219b69f755fbc2138f05a142261e44e07fa54cc37aab`.

Phase 2 is therefore complete for the deliberately narrow text-presence/lifecycle claim. The
supplied Windows set contains `PSCRIPT.DRV` and a built-in PostScript model, but printer setup and
the first PostScript capture remain Phase 3. Nothing fetches a driver, WPD, PPD, font, Windows, or
Ami Pro bytes.
