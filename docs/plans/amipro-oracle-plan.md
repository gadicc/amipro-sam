# Ami Pro 3.1 rendering oracle plan

Status: Phase 1 complete; the Phase 2 Ami Pro launch/known-state/clean-exit gate also passed on
2026-08-14. Exact Windows and Ami Pro media profiles, Windows Setup, Program Manager, Ami Pro
installation, and Ami Pro lifecycle checkpoints are now content-addressed. The invented-document
driver and native-text fixture are implemented and synthetic-tested; their first successful real
guest run remains the next gate. Printing is still out of scope.

This plan defines a phased, local-only rendering oracle for lawfully supplied Ami Pro and
Windows 3.1 media. It now has verified Windows-ready, Ami Pro install-candidate, and Ami Pro-ready
bases, but no document-rendering result. Proprietary execution requires the user's explicit
right-to-use affirmation.

## Scope and legal boundary

The public repository may contain orchestration source, generated configuration templates,
invented SAM fixtures, hashes, normalized measurements, and links to open-source projects. It
must never contain Windows, Ami Pro, printer-driver, PPD, font, help, template, document-corpus,
or installation-media bytes. Proprietary inputs and derived runtime state stay in a local,
Git-ignored, content-addressed cache.

The oracle will not fetch proprietary media. `bootstrap` accepts Windows media only through
`--win31-media PATH` or `WIN31_MEDIA_DIR`; a later printer-driver gate may require an additional
user-supplied, lawfully owned path if the Windows media does not contain a suitable PostScript
driver. Original images are opened read-only, mounted read-only at the OCI boundary, and mounted
with DOSBox-X read-only options where applicable. The current Ami floppy files have host mode
`0666`, so host permissions alone are not a safety boundary.

## Evidence inventory

### Repository and prior emulator work

All tracked commits, branches, reflogs, Codex checkpoint refs, unreachable Git blobs, and the
working tree were searched for Wine, DOSBox, Windows 3.1, executable-launch, and oracle work.
There is no tracked emulator implementation. Commit `5340537` deliberately deferred the oracle;
the README and format notes state that the existing converter does not execute Ami Pro, Wine, or
DOSBox. No prior work was deleted.

There is, however, a preserved Wine attempt under `/tmp` from 2026-08-13 14:34–14:44,
immediately before the first Git commit at 14:50. It is not part of Git. The material findings are:

1. Wine identified `INSTALL.EXE` as Win16, attempted its WineVDM path, and initially failed to
   start the application.
2. A manual loader attempt failed to load `kernel32.dll` with status `c0000135`.
3. Linking 1,093 Wine i386 modules into `syswow64` advanced the Lotus installer far enough to
   display `LCOMSTF.DLL not loaded.`
4. A subsequent debug run entered the Lotus installer runtime and looped until its X server was
   terminated; it did not establish a reproducible installation.
5. A flat payload copied to `C:\AMIPRO` loaded Ami Pro modules and then crashed with an unhandled
   page fault reading `FFFFFFFF` at `0000013E`.

The minimal useful evidence set is 2,164,132 bytes:

| Local-only artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `amipro-installer-wine-debug.log` | 380,012 | `f9c91d2ffc8d39f6ad95631436ac92770abbe21325098bf3e6a17b2739ced94d` |
| `amipro-installer-wow64.log` | 9,165 | `a46fd3cf3344bd8ef71e2d6f40462bc5e5a0f93cf1e5cde5ebb2400a5da0f680` |
| `amipro-installer-manual-loader.log` | 330,703 | `ef78487b5dcec2dd2b4da5e6f0af0e91803e47458db91e7bf5b0501b759f9769` |
| `amipro-installer-syswow64-linked.log` | 12,311 | `31ecd8d19d74585f5f9e121c68d0adb466f685836b75619bab19e4d17e23db9d` |
| `amipro-installer-syswow64-linked.png` | 1,882 | `e1766674f3ffe804840ae68cee1d2253c85a52ca3cd6099f8e83cc7f02c4849c` |
| `amipro-installer-lcomstf-debug.log` | 666,352 | `20ff51a846364b9431e28a3054a27555bae6e1698ed4552a274fdf7770316d75` |
| `amipro-manual-launch.log` | 763,420 | `9dec11f07fcfc025c7cbfffb400538c33ba0090b175b663813fbb4853b700a22` |
| `amipro-manual-launch.png` | 287 | `042e51922e72e14f93c1397a143e228d79f0e241db808c1cb7fe6bd569ad67ac` |

The useful installer screenshot visibly contains a proprietary UI, so it remains local and
ignored. The minimal evidence set was hash-verified into the ignored content-addressed cache before
relying on `/tmp`; raw logs are not publishable without path redaction. The large Wine prefixes
contain absolute host symlinks and, in the manual prefix, the proprietary payload. They remain
untracked and are not inputs to the DOSBox-X design.

The repeated, independent Win16 loader, installer-runtime, and application crashes are sufficient
evidence not to restart the Wine approach. New evidence would need to identify and validate a
specific Wine/WineVDM change against these failure modes.

### Owned Ami Pro media

Eight 1,474,560-byte FAT12 images are present at the supplied path. They were read and hashed,
not modified:

| Image | SHA-256 |
| --- | --- |
| `disk1.img` | `429ddde8fe1940143da71bc636508a7d680b40157bd38903af757325fcbb4987` |
| `disk2.img` | `302bae4676ae4938647adb2d3b95d7b0d0f3b98ee6ccdf4e87a751d8046b53a7` |
| `disk3.img` | `193bd039be9146a4c49f6a9f830216e75db11be40551310d61deb2e7f7a63428` |
| `disk4.img` | `70b1c51933bc60477f363c79de4cfadd424d097635e37d6767e5357ac22a1149` |
| `disk5.img` | `389dea0ae7d586bb45ed0307e19a29316c91d453f7359cfe3e2d4b792cb9dd04` |
| `disk6.img` | `bdd26f19b0074edeaa62324a74d2bae3b6d408be4bbb7a927f5890fad6453b55` |
| `disk7.img` | `f6916ef49952aa477f192cd91efea0aa105439476e9d6b817fc631edc4aadf22` |
| `disk8.img` | `e0419eec0a2ce2158cde82ab24d94419bd655f52df7db6f458eb3932626ab18e` |

Disk 1 contains the Lotus installer and the first multi-volume archive; disks 2 through 8 carry
the remaining archive volumes. The existing extracted payload contains 678 files and 22,964,970
bytes. Its `AMIPRO.EXE` hash is the expected
`555506d1558d61579d5c6fee8bf5fa9d960aa05a20a5d171240ac2e0ea73cbbd`.
Its canonical relative-path/content manifest digest is
`5076a4e5f1976452e3ee4d383372e56b7400604426fcec18b64ec519affaa8c4`; the analogous
installer-file digest is `d583de6ccfc56e05eee0ff3972fed7fd05fbc44ea69a95e37d23ca78b803c903`.
All 677 unique application-source basenames declared by the installer metadata exist at their
declared sizes; the remaining declared names are installer/bootstrap artifacts or a repeated
placeholder. This validates the extraction inventory, not the installation side effects.

The payload is useful for deterministic staging but is not proven to be a complete installation.
Installer metadata shows required directory placement plus changes to `WIN.INI`, `LOTUS.INI`,
Program Manager, filters, styles, macros, dictionaries, fonts, and shared tools. Bootstrap must
either drive the native installer or replay only side effects that are completely derived and
verified against that metadata. A flat copy is rejected as an installation method.

### Windows 3.1 media

The locally supplied set contains six 1,474,560-byte FAT12 images. Static inspection identifies
plain Microsoft Windows 3.1 Setup revision `3.1.040`, with American English as the default. The
images were opened read-only and hash to:

| Image | SHA-256 |
| --- | --- |
| `Disk01.img` | `86a56b7068993037ae950d0c81b29029b154e8c068c3f72f5e5e51a5833be8e2` |
| `Disk02.img` | `c8343fd2b8be589df1d3634cd73c7e6eb493e1009a7b58fdbaf856db113665c7` |
| `Disk03.img` | `1f5e2bd0d96d1aabd4e83690f208f4b0b637f029bf53c93f0823291fe1fb6f0f` |
| `Disk04.img` | `ba4f934f75b80d8e7652a077b49724db67c7daa5c529cc877cb91a32f99ae576` |
| `Disk05.img` | `4560f34f960b54bec30a4d8cd90c94fc4e6d1f45814c8659d1e5626afce9c8c3` |
| `Disk06.img` | `3747b2670dfbc5c5e91396853f372a1ffd6aa876e5af442518172bfb59a4ad15` |

The pure in-repository FAT12 extractor yields 467 collision-free root files totaling 8,305,739
bytes, with extraction digest
`362e55b05f737072f61f11b385b5214cb96354e8115e21ef938f8142e3d80504`.
The media includes `PSCRIPT.DRV`; `CONTROL.INF` maps the built-in `QMS ColorScript 100` model to it,
so this profile does not require downloading a printer driver or WPD.

An adjacent unselected text file says the archive came from WinWorld. That is provenance evidence,
not proof of a license or right to use the bytes. The driver therefore requires the explicit
right-to-use affirmation before executing Setup and never copies this media into Git or the OCI
image.

## Open-source runtime decision

The primary runtime is a rootless OCI image built from source. The initial lock is:

- DOSBox-X `2026.08.02`, release commit
  `784240ad6d9cf3ae3f02fab819e2ed5cf5117dd4`, built with SDL2 and printer/screenshot support;
- source tree `9058fd4983b50d038e3136fcedccd41ef70a4624`; the release is mutable and unsigned,
  so both commit and tree are verified during the build;
- official Debian `bookworm-20260713-slim` multi-platform manifest digest
  `sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818`;
- an immutable Debian snapshot for build and runtime packages, with the complete installed
  package/version inventory embedded in the image;
- Ghostscript `10.0.0~dfsg-11+deb12u8`, Poppler utilities
  `22.12.0-2+deb12u2`, Xvfb `2:21.1.7-3+deb12u12`, and SDL2
  `2.26.5+dfsg-1` from that snapshot.

The final OCI image must be recorded and invoked by its built image digest, not a mutable tag.
GitHub's generated source archive produced two different compressed byte streams during Phase 1;
those observed hashes remain in the lock as evidence but are not reproducible acquisition inputs.
The build must fail unless the fetched full commit and tree both match. This is content pinning,
not publisher authentication. Native tools are a diagnostic fallback only and must exactly match
the lock before they are allowed to produce oracle results.

The build fixes its source epoch and must fail if the exact generated configuration or dynamic
linkage enables pcap, SLIRP, SDL_net, modem, or IPX support. The runtime independently uses a
networkless OCI namespace. Redistributing an image remains a separate source/notices and license
review gate: the Debian Ghostscript build includes AGPL-covered code, and the selected DOSBox-X
README contains an unusual jurisdiction-specific age-verification notice. This plan records those
facts without interpreting them as legal clearance or prohibition.

The tracked lock is the build input rather than post-hoc metadata: the builder derives every
fidelity-relevant argument from it, verifies the hashed recipe/entrypoint, embeds and labels the
lock hash, and records only an image whose label matches. The build context is limited to the
open-source toolchain directory; ignored proprietary state is not transmitted to a builder.

The official DOSBox-X Windows 3.1 guide documents installation and use on a mounted host folder.
That is sufficient for the initial runtime and avoids adding separately licensed DOS media.
Its known loss of Windows 3.1 32-bit disk I/O is immaterial to Ami Pro rendering. A disk-image
guest remains a fallback if testing exposes folder-drive incompatibility; it is not the default.

Primary references:

- <https://dosbox-x.com/wiki/Guide%3AInstalling-Windows-3.1x>
- <https://dosbox-x.com/wiki/Guide%3ASetting-up-printing-in-DOSBox%E2%80%90X>
- <https://dosbox-x.com/wiki/DOSBox%E2%80%90X%E2%80%99s-Command%E2%80%90Line-Options>
- <https://github.com/joncampbell123/dosbox-x/releases/tag/dosbox-x-v2026.08.02>

## Automation evidence and decision order

Only the following Ami Pro facts are verified from owned local files:

- `PRNBATCH.SMM` iterates files and invokes `FileOpen`, `FilePrint`, and `FileClose`.
- `PRINTNOW.SMM` invokes `FilePrint`; `CLOSEALL.SMM` enumerates and closes documents.
- `_AUTORUN.SMM` reads `%WINDIR%\AMIAUTO.CFG` and calls each non-empty entry; installer scripts
  enable that autorun macro. This is a strong path for a generated controller macro, pending a
  guest behavior test.
- `MENULITE.SMM` maps File > Exit to a macro that calls `AppClose("")`, giving a native shutdown
  experiment that is stronger than guessed keystrokes, but not yet proven to resolve prompts.
- Installer scripts create Ami Pro's other registry-like INI entries and Program Manager state.
- The only documented installer option found locally is `INSTALL /n` for a node install. No
  supported quiet or answer-file option has been established.

Static strings strongly suggest, but do not yet verify, an Ami Pro `/p <file>` shell-print path,
DDE `StdFileEditing`, and `[FileOpen(...)]` commands. Macro autorun/autoexit strings also exist.
They are experiment candidates, not supported interfaces yet.

The automation order is therefore:

1. Test direct `WIN <path-to-AMIPRO.EXE> <document>` open behavior.
2. Test `/p` with an invented one-page SAM and inspect whether it prints and exits.
3. Test a minimal, invented macro derived from the documented macro calls, with all interactive
   selection removed and explicit success/failure sentinels written inside the disposable guest.
4. Test DDE only if the first three cannot provide deterministic lifecycle control.
5. Use keyboard/mouse automation only for installer steps or application dialogs for which no
   native interface exists.

Every promotion from hypothesis to mechanism requires a guest transcript, before/after state,
screenshot, hard timeout, and a synthetic fixture. Bundled proprietary macros may inform the
experiment but are never copied into Git.

## Architecture and isolation

The host orchestrator owns all path resolution, hashing, lifecycle, and manifests. The OCI
container runs with `--network=none`, a read-only root filesystem, dropped capabilities, no-new-
privileges, a fixed locale/time zone, and only explicit bind mounts. The guest config disables
IPX, NE2000, modem/serial networking, host clipboard integration, shell escape helpers, and any
automatic host opener.

Bootstrap and document execution use different mount profiles. Bootstrap may expose only verified
media directories as read-only binds plus its disposable job tree. Once any untrusted SAM runs,
the invocation exposes only the disposable writable `/oracle/job`; original media and the pristine
cache are absent. Container cleanup requires a new host-side CID file outside the guest-writable
tree and a matching per-job label before stop/kill/remove, preventing name-collision cleanup from
targeting another container.

The base runtime is stored below:

```text
.amipro-oracle/
  evidence/<evidence-set-hash>/
  cache/runtime/<runtime-key>/
    media-manifest.json
    build-manifest.json
    pristine-c/
  jobs/<job-id>/
    staging-c/        disposable copy/reflink, never a writable hard link
    input/            DOS-safe staged names only
    capture/          raw LPT output
    diagnostics/      logs, config, screenshots, state trace
    output/           PS, PDF, PNG, analysis and manifest
```

The runtime key hashes a canonical schema version, sorted Windows and Ami media manifests,
DOSBox-X version and binary hash, OCI digest, generated config hash, installer-driver version,
and selected video/printer profile. Each cached file is hashed in a canonical tree manifest.
Timestamps, inode numbers, absolute source paths, and directory enumeration order do not enter
the key. Human-facing provenance may record redacted source labels separately.

A job never mounts the user's source directory in the guest. The orchestrator copies one bounded
input at a time to a DOS-safe name in the disposable C: tree, preserving a host-side name map.
The base runtime and proprietary media are absent from the job once staging completes. Capture and
diagnostic directories are the only writable host outputs. Per-job full copies are the portable
baseline; verified reflinks or a rootless copy-on-write layer are optional optimizations.

The generated DOSBox-X configuration uses a fixed machine/video mode, memory size, CPU core and
cycles, locale, and display geometry. It includes a per-job `captures` path and:

```ini
[parallel]
parallel1=file timeout:2000
```

An empty per-job `captures` directory is required. Explicit `file:` and `append:` targets are
forbidden because an inactivity-triggered reopen can overwrite or merge capture data. Exactly one
fresh capture is success; multiple captures are preserved and fail as a split print. No `openps`,
`openpcl`, or `openwith` hook is permitted. `-conf`, `-fastlaunch`, `-exit`, and a
hard `-time-limit` are evaluated per phase. `-silent` is unsuitable for phases requiring X11
state observation and is used only if empirical testing proves its AUTOEXEC semantics fit a
bounded non-GUI step.

## State detection and failure evidence

There are two time bounds: an outer host process deadline and a shorter DOSBox-X `-time-limit`.
The host sends a graceful termination signal at its deadline, captures final evidence, waits a
small fixed grace period, then kills only the resolved child process group. Timeouts use monotonic
deadlines. Long arbitrary sleeps are forbidden.

Installer/UI states are named and matched using two independent observations where practical:
window metadata plus a bounded screenshot-region hash/OCR result, or a guest sentinel plus a
screen state. Each transition has a deadline, retry limit, allowed successors, and evidence
action. Unknown dialogs fail closed and are screenshotted. Successful shutdown requires an
explicit guest sentinel, Windows exit, and DOSBox-X exit code before the outer deadline.

Printing completes only after a PostScript header is present, the capture size and mtime remain
stable across bounded polls after the LPT timeout, and Ami Pro reports no print dialog/error.
Stable size alone is not success. On every failure, configuration, stdout/stderr, state trace,
screenshots, staged-name map, capture bytes, process result, and a failure manifest remain in the
job directory.

## Printer and comparison contract

The primary oracle requires a lawfully supplied Windows 3.1 PostScript driver and fixed printer
description. Stock Windows 3.1 uses `PSCRIPT.DRV` with built-in models and/or binary `*.WPD`
descriptions rather than assuming a modern PPD workflow. Bootstrap first inventories the user's
Windows media for a compatible driver and definition. It must not download or embed either.
Driver file hashes, any WPD/OEM setup files, selected printer model, page size,
resolution, printable area, font substitution settings, and all installed files enter the runtime
key. DOSBox-X Epson emulation is explicitly labeled a smoke-test backend and never produces
baseline oracle measurements.

Raw LPT PostScript is the primary captured artifact. A locked Ghostscript command derives PDF;
locked Poppler commands derive page PNGs, text, and bounding boxes. PDF bytes are never compared.
The normalized comparison includes:

- page count and page geometry;
- normalized extracted text, with an explicit whitespace policy;
- word/text and image bounding boxes in a fixed coordinate system and tolerance;
- per-page raster dimensions, differing-pixel ratio, RMSE, and optional bounded diff images;
- missing/extra pages, objects, and diagnostics as first-class failures.

Thresholds and normalization schema versions are recorded in the report and runtime key. A report
cannot silently compare measurements produced by different analysis profiles.

## Stable commands and exit status

The public surface is:

```console
./scripts/amipro-oracle doctor
./scripts/amipro-oracle bootstrap --confirm-proprietary-media-rights \
  --win31-media PATH --amipro-media PATH
./scripts/amipro-oracle boot-probe --confirm-proprietary-media-rights
./scripts/amipro-oracle install-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle launch-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle smoke --confirm-proprietary-media-rights
./scripts/amipro-oracle batch --input PATH --output PATH
./scripts/amipro-oracle compare --expected PATH --actual PATH
```

Exit codes are stable: `0` success/equal, `1` job or comparison failure, `2` invalid invocation,
`3` missing prerequisite/media, `4` media or cache integrity failure, `5` timeout, and `6` internal
or backend failure. Commands support machine-readable JSON without changing diagnostic exit codes.
`doctor` is read-only. `bootstrap` is resumable only at explicit, hash-verified states and never
marks a partial runtime ready.

Each job manifest records schema version, source hash/size and staged name, media/runtime/config/
tool hashes, OCI digest, timings from monotonic durations, state transitions, process result,
artifact relative paths/sizes/hashes, PS validation, PDF/PNG analysis, comparison profile, and
diagnostics. Host absolute source paths are omitted by default.

## Milestones and acceptance gates

### Phase 1: deterministic bootstrap

- Implement `doctor`, canonical media manifests, cache keys, generated config, bounded process
  runner/state machine, evidence preservation, and fake backend.
- Build and verify the rootless OCI toolchain; record its digest and installed-package manifest.
- Given supplied Windows media, automate Windows setup, reach a known Program Manager state in a
  separate media-free run, exit cleanly, and re-hash the runtime.
- Install Ami Pro and verify its installer side effects without manual interaction before calling
  Phase 1 complete.
- Unit tests cover mutation-during-hash, symlinks/special files, name collisions, cache poisoning,
  timeouts, invalid transitions, atomic manifests, and missing-media status.

### Phase 2: Ami Pro smoke

- Create only an invented synthetic SAM fixture.
- Launch Ami Pro through the first verified native mechanism, prove the document is visible with a
  screenshot and state evidence, then close Ami Pro, Windows, and DOSBox-X within hard deadlines.
- Do not use `RUNEXIT.EXE` without verified provenance and redistribution terms. Prefer a native
  macro/exit path or implement a small redistributable helper from documented Windows APIs.

### Phase 3: one-file PostScript oracle

- Install and hash the fixed user-supplied printer profile.
- Capture valid PostScript verbatim, detect bounded completion, derive PDF/PNG/analysis, and write a
  complete job manifest plus self-comparison report.
- Repeat the same job to quantify nondeterminism before accepting a baseline.

### Phase 4: unattended batch

- Add deterministic 8.3 staging/name maps, per-file disposable state, resumability, dialog/crash
  classification, continue/fail policy, and per-file deadlines.
- Test collisions, hostile names, corrupt/macro-bearing files, partial captures, hangs, disk-full,
  and interruption/resume. Real private documents remain opt-in and local.

### Phase 5: converter integration

- Add invented one-feature fixtures for tables, frames, paragraph regions, inline commands, styles,
  headers/footers, geometry, and pagination.
- Store only fixture sources and normalized lawful measurements. Public CI uses the fake backend;
  local oracle tests require an explicit opt-in and ready cache.
- Document the one-command local path from owned media to bootstrap and batch comparison.

Each independently useful phase is tested, linted, smoke-tested where its backend exists, and
committed separately. A real-backend milestone is not complete merely because fake-backend tests
pass.

## Adversarial review

| Failure or false assumption | Required control or gate |
| --- | --- |
| Missing Windows media is papered over with a download | Hard exit `3`; name only `--win31-media` / `WIN31_MEDIA_DIR` as the required input. |
| Flat Ami payload is called installed | Reject until installer-derived topology and INI/font/shared-DLL side effects are verified. |
| A string inside `AMIPRO.EXE` is treated as a CLI contract | Keep `/p` and DDE experimental until synthetic guest transcripts prove behavior and exit semantics. |
| Bundled `PRNBATCH.SMM` is copied or assumed unattended | Reimplement only the minimal calls in a new local generated macro after behavior is verified; keep vendor macro bytes out of Git. |
| A printer driver/PPD is silently sourced | Inventory owned Windows media, otherwise stop for an exact user-supplied licensed path. |
| Folder mounting lets hostile documents reach the host | Stage into a disposable C: copy; expose no source/cache/media mounts during document execution. |
| Macro, DDE, or OLE content escapes isolation | No guest network/device, no host shell hooks, disposable runtime, explicit mounts, resource/time limits, and fail-closed unknown dialogs. |
| DOSBox time limit kills before evidence is saved | Outer supervisor deadline exceeds inner limit and continuously writes host-side state/log evidence. |
| LPT file stability truncates a print | Require PS structure plus stable size after LPT timeout and application print completion. |
| PDF byte equality becomes the oracle | Compare versioned normalized text/geometry/raster measurements only. |
| Cache key omits a fidelity input | Include media trees, OCI/binary/config/driver/profile hashes and schema versions; integrity-check all ready caches. |
| Writable hard links corrupt the pristine runtime | Forbid them; use full copies, verified reflinks, or copy-on-write layers. |
| Rootless OCI is assumed available | `doctor` probes it without mutation; a locked native fallback is allowed only on exact hash/version match. |
| Fake backend success is reported as a real oracle | Backend identity is mandatory in output and manifests; fake outputs cannot become baselines. |
| Old Wine work is retried without cause | The recorded Win16/installer/page-fault failures are a decision gate; require specific new evidence first. |

## Current stop condition

Phase 1 is implemented and exercised. Windows-ready runtime
`efab02fe92a782e9d3a59540d7b8caddbff2740cbbee9a9cf1285654d8e83bd3` was cloned for the bounded
Ami Pro installer. All seven exact installer dialog states, the post-install Program Manager, the
Exit Windows confirmation, a zero DOS return sentinel, the expected executable hash, and required
INI/directory side effects were validated before publishing install candidate
`c7c79b26e9779a3c2f95b00c8f2301e95523cde960d1e287aacc79aa9dee6745`. Its sealed tree contains
924 files in 14 directories totaling 28,946,822 bytes. Evidence job
`install-amipro-c7c79b26e977-a9gyaes6` is local and ignored, and cache reuse was verified without
restarting Setup.

The separate, media-free Ami Pro lifecycle gate has also passed. It classified and dismissed the
expected no-printer/screen-formatting warning, proved a blank untitled editor with that dialog
absent, closed Ami Pro, recognized minimized Program Manager, confirmed Exit Windows, and promoted
Ami Pro-ready runtime `a1613ad18f592516bef907ec04d608cf64a3bdf63ea2e2f824aa7690a273d9c0`.
The sealed runtime contains 925 files in 14 directories totaling 28,952,075 bytes. Evidence job
`launch-amipro-a1613ad18f59-ssfqbjiz` remains local and ignored; cache reuse was verified without
launching the application again.

The invented-document gate is now implemented. It stages only a bounded, text-only `SMOKE.SAM` in
a disposable clone, requires a canonical version-4 envelope and self-consistent `[Embedded]`
offset, checks the exact document title plus visible body ink and absence of the loading hourglass,
then requires clean document/application/Windows/DOSBox-X return evidence. The corrected fixture is
596 bytes with SHA-256
`22c8346b62dd3b0ad5858e752a92d4a0a1297b8dbda648c356bd5b6ab8982e49`; its trailer points to
byte 574. Synthetic tests pass, but a successful native run of this corrected fixture has not yet
been recorded, so Phase 2 is not complete. Printing remains out of scope until the separately
keyed printer phase.

For a fresh local rebuild, first affirm the right to use the supplied media:

```console
./scripts/amipro-oracle bootstrap \
  --confirm-proprietary-media-rights \
  --win31-media /absolute/path/to/owned/windows-3.1-media \
  --amipro-media /absolute/path/to/owned/ami-pro-3.1-media
```

or set `WIN31_MEDIA_DIR` and `AMIPRO_MEDIA_DIR` in ignored `.env.local`. Do not provide or download
media through the repository. Then run:

```console
./scripts/amipro-oracle boot-probe --confirm-proprietary-media-rights
./scripts/amipro-oracle install-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle launch-amipro --confirm-proprietary-media-rights
./scripts/amipro-oracle smoke --confirm-proprietary-media-rights
```

The first production probe exposed DOS 8.3 truncation of `BOOT.START` to `BOOT.STA`; it failed
closed without promotion. The explicit 8.3 sentinel and regression test were added before the
successful run. Neither Windows-ready cache nor its screenshots are publishable source artifacts.
The Ami Pro installer also failed closed during development when Windows remained open after Setup
and when a blinking cursor raced whole-screen confirmation decoding. The committed driver now
performs an explicit Program Manager exit and decodes one identity-checked observer snapshot. The
successful install candidate and all failed-attempt screenshots remain local, ignored artifacts.
