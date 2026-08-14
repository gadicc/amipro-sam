# Locked oracle toolchain

`Containerfile` builds DOSBox-X from the verified `2026.08.02` full commit and tree on a digest-
pinned Debian base. Apt is redirected to an immutable Debian snapshot, and fidelity-relevant
runtime packages are version-pinned. The final image embeds the complete `dpkg` package inventory
at `/opt/amipro-oracle/dpkg-versions.txt`. The build uses a fixed source epoch, rewrites image
timestamps, and retains the DOSBox-X configure arguments, generated `config.h`, and linked-library
inventory under `/opt/dosbox-x/share/amipro-oracle/`.

The build script derives the base, snapshot, source commit/tree, configure flags, package versions,
platform, entrypoint hash, and source epoch from `toolchain.lock.json`. The lock is copied into the
image, its hash is verified and labeled, and the final record is accepted only when that label
matches. A minimal `.containerignore` limits the build context to this recipe, lock, and entrypoint,
so ignored local guest/evidence state is never sent to the builder.

The build fails if pcap, SLIRP, SDL_net, modem, or IPX support appears in the generated
configuration or linked libraries. Runtime isolation still requires `--network=none`; neither
control is treated as a substitute for the other.

The build downloads only open-source tooling. It does not accept, copy, or contain Windows, Ami
Pro, or any user-supplied/proprietary printer-driver, PPD/WPD, font, help, template, or document
media. Open-source Debian font dependencies remain governed by the lock and license review.

Build with rootless Podman:

```console
./scripts/build-oracle-toolchain
```

The script writes the local image identity and lock hash to the Git-ignored
`.amipro-oracle/toolchain-image.json`. Before a real oracle result can be accepted, the runtime
runner must invoke the recorded image with networking disabled, a read-only root filesystem,
dropped capabilities, no-new-privileges, resource limits, and only the job's explicit mounts.

The image entrypoint starts a fixed 1024×768×24 Xvfb display with TCP listening disabled, waits on
a bounded readiness probe, launches DOSBox-X, propagates its status, and cleans up Xvfb. Bootstrap
may add verified read-only media mounts; document execution is restricted to one disposable
writable `/oracle/job` mount. Container cleanup uses a newly written CID plus a matching instance
label, never a caller-supplied name alone.

The release and commit are not signed, and GitHub reports that the release is not immutable.
GitHub's generated source tarball also changed byte representation during this investigation, so
its observed hashes are evidence only, not build inputs. The build fetches the tag and fails unless
both the full commit and source-tree hashes match `toolchain.lock.json`. This gives content pinning,
not publisher authentication; the limitation is explicit rather than hidden behind an unsigned
archive checksum.

Before distributing this image, review and provide the required source/notices for DOSBox-X,
Ghostscript, Poppler, Xorg/SDL, fonts, and their dependency closure. In particular, Debian's
Ghostscript package includes AGPL-covered code, and DOSBox-X's selected README contains an unusual
jurisdiction-specific age-verification notice. `NOASSERTION` in the OCI label is intentional; it
is not a redistribution clearance. No proprietary guest/runtime file belongs in an image layer.
