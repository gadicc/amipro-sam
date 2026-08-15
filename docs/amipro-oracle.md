# Local Ami Pro rendering oracle

The oracle is an opt-in, local test system for comparing this converter with Ami Pro 3.1 under
Windows 3.1. It is deliberately separate from the normal converter: public CI and ordinary
conversion never execute Ami Pro or require proprietary media.

The current milestone provides a real, bounded Windows 3.1 Setup driver, an independent
Program Manager boot/clean-exit gate, and an exact-dialog Ami Pro 3.1 installer. These phases have
produced content-addressed **Windows-ready**, **Ami Pro install-candidate**, and **Ami Pro-ready**
bases. The separate launch/clean-exit and invented-document gates are exercised: Ami Pro natively
saved and then twice reopened the exact invented fixture, visibly displayed both lines, and exited
cleanly. The supplied Windows PostScript driver has also been installed through a separately
keyed, exact-screen-state gate, producing a sealed printer-ready base. Two subsequent one-file
print jobs produced byte-identical PostScript, PDF, text, bounding-box, and raster results. This is
controlled native evidence for one invented fixture and exact environment, not a general
typography or print-fidelity baseline. The resumable real-batch path has also completed one
invented-document probe through its generalized page/geometry analysis. No current manifest is
accepted as a fidelity baseline. See the [investigation and adversarial
plan](plans/amipro-oracle-plan.md).

## Commands

Run from the repository root:

```console
./scripts/amipro-oracle doctor
./scripts/amipro-oracle bootstrap --confirm-proprietary-media-rights
./scripts/amipro-oracle boot-probe --confirm-proprietary-media-rights
./scripts/amipro-oracle install-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle launch-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle smoke --confirm-proprietary-media-rights
./scripts/amipro-oracle install-printer --confirm-proprietary-media-rights
./scripts/amipro-oracle print-smoke --confirm-proprietary-media-rights
./scripts/amipro-oracle batch --input PATH --output NEW_PRIVATE_PATH \
  --confirm-proprietary-media-rights
./scripts/amipro-oracle batch-status --output PRIVATE_BATCH_PATH
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

Explicit `--win31-media` and `--amipro-media` arguments take precedence. Media inventory opens
files read-only, rejects links and special files, detects concurrent changes, and records canonical
hashes. The current owned Ami floppy images are host-writable, which `doctor` reports; future
container/guest mounts must still expose them read-only.

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

The Windows installer, boot-probe, Ami Pro installer, launch gate, document smoke, printer
installer, and PostScript smoke retain their state traces, generated configurations, bounded
stdout/stderr, changed-screen archives, observer status, runtime trees, and process cleanup
results. The boot
gate retains the accepted Program Manager and Exit Windows frames. The Ami Pro installer retains
all seven recognized installer states plus the post-install Program Manager and Exit Windows
frames. The document smoke retains the exact printer-warning, document-ready, Program Manager,
and Exit Windows frames. The printer phase retains the empty control panel, direct-to-port toggle,
model selection, source prompt, installed model, Program Manager, and Exit Windows frames. Stable
checkpoint manifests exclude volatile job paths/timing; returned results name the ignored evidence
jobs that back them. Troubleshooting starts with captured evidence, not manual guest inspection.

The prior failed Wine attempt has been copied, hash-verified, and retained in a local ignored
content-addressed evidence namespace. It is not part of the oracle runtime, and no Wine prefix or
proprietary executable was copied into source.

## Private native batches

The real batch command recursively discovers `.SAM` files, sorts their relative names
deterministically, and stages only one source at a time as `DOC00001.SAM`, `DOC00002.SAM`, and so
on. The source directory is never mounted into the guest. Every document gets a new writable copy
of the sealed printer-ready runtime, a separate wall-clock deadline, its own UI/process/capture
evidence job, and exactly one raw PostScript capture. Locked Ghostscript and Poppler then derive a
private reference PDF, per-page PNGs, text, and word boxes.

Before native execution, a no-follow, mutation-detecting preflight rejects macro/DDE/OLE/link
sections, dynamic X/Z expressions, embedded OLE payloads, nonempty external stylesheet/file/book/
master/merge metadata, and path-like printer metadata. Unknown dialogs fail closed because the
driver requires the exact editor menu, Print dialog, Program Manager, and Exit Windows states. A
blocked, crashed, timed-out, split-print, or invalid document is recorded and the remaining files
continue. Exit `1` means the batch completed with at least one such per-file result; it does not
discard successful PDFs.

For an ignored private corpus such as `mydocs`, choose a new output directory:

```console
./scripts/amipro-oracle batch \
  --input mydocs \
  --output .amipro-oracle/private-batches/mydocs-20260815 \
  --timeout-seconds 180 \
  --progress \
  --confirm-proprietary-media-rights
```

`--progress` writes privacy-safe per-document lifecycle lines to stderr, using only the stable
guest identifiers such as `DOC00001.SAM`; final JSON remains clean on stdout when `--json` is also
used. The atomic `progress.json` and `batch.json` journals are written before the first document
finishes and after every transition. Inspect an existing run, including one started without
`--progress`, from another terminal:

```console
./scripts/amipro-oracle batch-status \
  --output .amipro-oracle/private-batches/mydocs-20260815

watch -n 2 './scripts/amipro-oracle batch-status \
  --output .amipro-oracle/private-batches/mydocs-20260815'
```

The isolated observer continually replaces a read-only screenshot of the active DOSBox-X display.
On a host with `feh`, open that frame with automatic reload enabled:

```console
screen_path=$(./scripts/amipro-oracle batch-status \
  --output .amipro-oracle/private-batches/mydocs-20260815 \
  --screen-path)
feh --reload 1 "$screen_path"
```

Rerun those two commands when the batch advances to a new document, because every document has a
separate evidence job. This is intentionally visual-only: attaching a keyboard/mouse or the host X
socket would alter the experiment and weaken the container boundary.

If the command is interrupted, or some documents fail transiently, repeat the exact command with
`--resume`. Successful results are hash-verified and skipped; incomplete and failed documents get
new numbered attempts while previous failure evidence is retained:

```console
./scripts/amipro-oracle batch \
  --input mydocs \
  --output .amipro-oracle/private-batches/mydocs-20260815 \
  --timeout-seconds 180 \
  --resume \
  --confirm-proprietary-media-rights
```

`plan.json` and `name-map.json` map private relative source names to DOS-safe guest names. Host-side
PDFs preserve the source's relative directories and basename while changing only `.SAM` to `.pdf`;
for example, `letters/Example.SAM` becomes `reference-pdf/letters/Example.pdf`. `batch.json` is the
atomic result journal, while `progress.json` records the latest privacy-safe lifecycle event. All
of these files contain or identify private material and must remain ignored and local. The PDFs can
embed fonts from the proprietary guest environment; neither they nor the PNGs are cleared for
redistribution, and every result remains
`baseline_eligible: false`.

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

Phase 2 is therefore complete for the deliberately narrow text-presence/lifecycle claim.

Install the supplied Windows PostScript driver into a separately keyed disposable clone:

```console
./scripts/amipro-oracle install-printer --confirm-proprietary-media-rights
```

If more than one Ami Pro-ready runtime exists, add `--runtime-key HASH`. The phase mounts the
verified flattened Windows media read-only, opens Control Panel's Printers applet, disables Print
Manager for direct LPT capture, selects the built-in `QMS ColorScript 100` model, supplies `S:\` as
the driver source, and requires seven exact UI states plus a clean Windows/DOS return. It then
hashes the installed driver/help/test files and checks the exact `WIN.INI` and `CONTROL.INI`
settings before sealing a printer-ready runtime. No external PPD or WPD is used.

The exercised run on 2026-08-15 promoted Ami Pro-ready parent
`a1613ad18f592516bef907ec04d608cf64a3bdf63ea2e2f824aa7690a273d9c0` to printer-ready runtime
`215b2afac79849baca3b07098180ccc6d3274545eae99f26db89126086767fe2`. The container exited zero
without timeout after 32.91 seconds; validation and atomic promotion completed in 49.70 seconds.
The sealed tree contains 928 files in 14 directories totaling 29,311,654 bytes. Installed
`PSCRIPT.DRV` hashes to
`469a11a947b98716b5aba63e170754c2b1f055ce7e03101c6748c1b1a97ac25d`; the selected device is
`QMS ColorScript 100,pscript,LPT1:`, and `spooler=no` locks direct-to-port behavior. Evidence job
`install-printer-215b2afac798-emlvqt3q` remains local and ignored. A second invocation revalidated
the cache and all acceptance evidence without starting the guest.

Run the one-file native print gate from the sealed printer-ready runtime:

```console
./scripts/amipro-oracle print-smoke --confirm-proprietary-media-rights
```

If more than one printer-ready runtime exists, add `--runtime-key HASH`. The guest opens the exact
invented fixture, requires its known ready state, invokes Ami Pro's Print dialog, accepts the fixed
printer defaults, waits for both the DOSBox-X LPT-close event and stable capture bytes, and then
closes Ami Pro and Windows. Exactly one capture is required. The raw stream is preserved; a
separately hashed derivative removes only one leading and one trailing Ctrl-D before bounded
Ghostscript and Poppler analysis in fresh network-disabled OCI invocations.

Two production jobs on 2026-08-15, `print-smoke-_0zj4tp0` and
`print-smoke-4lvjarl9`, used printer-ready runtime
`215b2afac79849baca3b07098180ccc6d3274545eae99f26db89126086767fe2`.
They produced byte-identical 18,881-byte raw PostScript with SHA-256
`dc5c6049ade704787095728b6966ccac3047e7f2fe4429bb134e28255c77f8d9`, including boundary
Ctrl-D bytes, and byte-identical 18,879-byte sanitized PostScript with SHA-256
`8d94694fbdc481197e714686d0766c224c249beea12ee69e6421b05538d5bcf6`.
The stream is DSC 3.0 from `Windows PSCRIPT`, declares one page and bounding box
`14 91 582 782`, and names `Ami Pro - SMOKE.SAM`.

Pinned Ghostscript 10.00.0 produced the same 5,737-byte PDF in both runs, SHA-256
`bceffa6abd18e3ee0f8a27dcafbdb801fc3b41c3cbce4fce8718d54fe3bc47c9`.
It is one unrotated A4 page (595x842 points). Poppler extracted the exact two invented lines and
six word boxes; its 144-DPI raster is 1190x1684 and hashes to
`14c3ecd69fd6b5fc2801d5caff936d1b0e48c2d0c301c2db4daf714eac2ab553`.
Formal `compare` returned equal with zero differing pixels and RMSE 0. The PDF contains an embedded,
unnamed Type 3 font, so all PS/PDF/PNG artifacts remain ignored and local. Both manifests say
`baseline_eligible: false`; Phase 3 establishes this exact controlled observation, not a
redistributable golden file or general typography/layout fidelity.
