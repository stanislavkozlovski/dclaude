# Recover Docker space on a Mac

`dclaude --space` helps you retire old launcher images, separately clear retained
build cache, and measure how much space the Mac actually gains. `dcodex --space`
provides the same commands because both launchers use the same images.

Run these commands in your **host terminal**, from any directory. Storage mode
does not launch an agent, build an image, update the launcher, or need a target
Git repository. It needs host Python 3; ordinary agent launches do not.
`dclaude --space --help` explains the commands without contacting Docker.

## Start with a preview

```bash
dclaude --space
```

The default action is an image preview. It reports Docker coverage, protected
images, cleanup candidates, cache accounting, and Mac measurements. A preview
deletes nothing. Keep Docker Desktop running so the report can inspect its active
image store and default builder.

If the Docker disk-image location cannot be found, open **Docker Desktop →
Settings → Resources → Advanced** and supply the disk-image path shown there:

```bash
dclaude --space --disk-image "/Volumes/Developer/Docker/Docker.raw"
```

Read the coverage gaps before acting. An inaccessible file is **unmeasured**, not
empty. If macOS denies access, the report identifies the path and error; review
your terminal's access in **System Settings → Privacy & Security**. `sudo` does
not grant macOS privacy access. Diagnosis does not require granting blanket Full
Disk Access.

## Clean up in two separate steps

Close other Docker clients and leave older launchers idle while applying cleanup.
These launchers coordinate their own builds and container creation with cleanup,
but Docker does not offer a transaction that locks out every other client.

First retire old images:

```bash
dclaude --space images --keep 2 --apply
```

This collects a fresh plan, displays every target, and asks for interactive
confirmation. Review legacy images carefully: old release tags establish which
tags you are reviewing but do not prove how the images were built. Deletion has
no undo. You can rebuild later, but mutable base images and package repositories
mean the result may differ.

Then take a fresh cache preview, after the image references have been removed:

```bash
dclaude --space cache
dclaude --space cache --apply
```

Cache cleanup has its own plan and confirmation. It targets eligible **private,
reclaimable records on the default builder**, by record ID. It preserves shared
and in-use records. This cache belongs to the builder, not exclusively to
`dclaude`: another project's next build can need downloads or recompilation.
Descriptions, labels, and paths are not treated as proof of cache ownership.

Neither step removes containers, volumes, repositories, credentials, host caches,
or arbitrary images from other repositories. Image cleanup never uses force or
parent-image pruning. If an ID filter or eligibility check fails, cleanup stops;
it never falls back to a global prune.

## Command reference

Both `dclaude` and `dcodex` accept the following:

| Command or option | Meaning |
| --- | --- |
| `--space` | Diagnose Docker usage and preview image cleanup. |
| `--space images` | Preview old launcher images. |
| `--space images --apply` | Replan, confirm interactively, delete eligible images, and measure recovery. |
| `--space cache` | Preview eligible default-builder cache records. |
| `--space cache --apply` | Replan, confirm separately, delete approved cache records, and measure recovery. |
| `--space verify` | Remeasure the latest cleanup receipt; delete nothing. |
| `--space retention enable` | Opt into image retention after successful launcher builds and warm-container bootstrap. |
| `--space retention status` | Show the saved image policy. |
| `--space retention disable` | Stop future automatic cleanup; preserve history. |
| `--keep N` | For image previews/applies or retention enable, retain the newest N distinct launcher builds; default 2, minimum 1. Protections can retain more. |
| `--disk-image PATH` | Supply the Docker Desktop disk-image location for image/cache commands or retention enable. `verify` uses the receipt's saved path. |
| `--json` | Emit read-only structured output for image/cache previews, verify, or retention status. Rejects `--apply` and retention enable/disable. |
| `--space --help` | Explain storage commands without requiring Docker. |

`--apply` is only for image/cache deletion. It requires a terminal and explicit
confirmation; piping `yes` or adding the launcher's update-only `--yes` does not
authorize cleanup. A saved JSON preview or receipt is not an executable plan.
Every apply collects a new inventory and rechecks each target before deleting it.

The launchers' existing `--` separator still forwards the remaining arguments to
the agent. For example, `dcodex -- --space` sends `--space` to Codex instead of
entering host storage mode. Put `--space` first when using host storage commands.
Keep storage actions separate from agent launch, profile, rebuild, reset, SSH,
version, and update options; these combinations are rejected.

```bash
# Save a machine-readable diagnosis.
dclaude --space --json > docker-space.json

# Review more history without deleting it.
dcodex --space images --keep 4

# Use an external disk image for this measurement.
dclaude --space cache --disk-image "/Volumes/Developer/Docker/Docker.raw"
```

## Which images are protected?

Retention counts immutable image identities, not tags. Multiple release tags can
refer to the same build and therefore count once. Creation time orders builds,
with the image ID as a tie-breaker.

An image is protected if any of these apply:

- Any running **or stopped** container references it.
- It is the configured launcher image.
- It is among the newest N distinct launcher builds overall.
- It has a non-release `dclaude` tag or an alias in another repository.
- Its index, platform, or attestation relationships cannot be established safely.

Every alias of a protected identity is preserved. Container references protect the
related image family; cleanup skips uncertain relationships. A stopped warm
container can therefore keep an older build alive indefinitely. `--keep 2` is a
minimum retention policy, not a promise that only two images will remain.

New launcher builds carry ownership labels. Explicit image cleanup can offer
unlabelled legacy release tags for manual review. **Automatic retention only
deletes positively labelled launcher history**. Labelled dangling images left by
rebuilding a tag can be eligible; unlabelled dangling images remain unclassified.
Image overrides do not expand automatic cleanup into a custom repository.

## Read the measurements without double-counting

The report keeps these views separate:

| View | What it answers | What it cannot promise |
| --- | --- | --- |
| Docker images | Which image identities and tags exist, and their reported sizes? | Image sizes can overlap cache and each other. |
| BuildKit cache | Which records are shared, private, or reclaimable? | Reclaimable cache is not an estimate of additional Mac space. |
| Disk-image allocation | How many host bytes are allocated to the sparse disk-image file? | The logical maximum size is not occupied SSD space. |
| APFS free space | How did available space in each measured APFS container change? | Other programs can write or delete data during measurement. |

Do not add image usage and cache usage to estimate a recovery total. The
disk-image's allocated size is only a broad upper bound, not a useful forecast.
The report preserves exact byte values; GB means 1,000,000,000 bytes and GiB means
1,073,741,824 bytes.

For each cleanup step, the helper records a baseline, then samples the same
disk-image identity and APFS counters for up to 60 seconds. **Observed free-space
change is signed:** a negative number means the Mac had less free space afterward,
possibly because another process wrote data. Docker can release 20 GB of reported
objects while the Mac gains only 4 GB. Those are separate observations, not a
measurement failure to conceal.

The startup APFS container and the container holding `Docker.raw` are counted once
each. If the image lives on an external drive, recovery there is reported
separately from startup-disk recovery. The helper does not walk directories, sum
hard links, cross mounts, or attempt exact clone attribution.

If the result says **recovery not yet observed**, let Docker settle and run:

```bash
dclaude --space verify
```

Docker documents `Docker.raw` reclamation within seconds, but retained cache,
delayed deallocation, snapshots or clones, and concurrent writes can change the
observed result. Inactive Docker image stores and unrelated storage remain outside
coverage. The command never restarts Docker, reduces the disk limit, resets its
disk image, or invokes privileged reclamation. See [Docker's Mac disk-image
guidance](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/macfaqs/).

## Prevent old builds from accumulating

Image retention is off until you enable it:

```bash
dclaude --space retention enable --keep 2
dclaude --space retention status
```

Enabling displays and saves the policy for the exact `dclaude` repository. Once a
launcher image build and warm-container bootstrap both succeed, the launcher can
retire eligible labelled history under that policy. A standalone `--update-tool`
build waits until the next successful launch. Reusing a warm container does not
turn every agent session into a general cleanup job.

Explicit `DCLAUDE_IMAGE_NAME` or `DCLAUDE_VERSION` overrides bypass ownership
labelling and automatic retention, even when they name the default image.
`BUILDX_BUILDER` overrides and `DOCKER_BUILDKIT=0` also bypass automatic retention.
Cleanup failure produces a warning and does not prevent a healthy agent launch.
Container protections, old unlabelled builds, and volumes can still use space;
retention does not expire environments or impose a total Docker size cap.

To stop automatic image cleanup:

```bash
dclaude --space retention disable
```

Disabling does not delete images or erase receipts. Existing launches and manual
previews continue to work.

### Give Docker its own cache budget

Image retention and cache garbage collection solve different accumulation
problems. For a space-conscious default-builder cache budget, review **Docker
Desktop → Settings → Docker Engine** and merge these values into the existing
configuration:

```json
{
  "builder": {
    "gc": {
      "enabled": true,
      "defaultKeepStorage": "5GB"
    }
  }
}
```

This is a fragment to merge, **not a replacement for your current settings**.
Preserve other top-level settings and existing builder options. Review existing
custom GC policies before changing them. Apply the settings through Desktop when
you are ready for any restart it requires; the launcher never edits them or
restarts Docker automatically.

Docker owns cache eviction. The budget affects every project using this builder,
can increase rebuild/download time, and is a GC target rather than a total Docker
space cap. See [Docker's garbage-collection
documentation](https://docs.docker.com/build/cache/garbage-collection/).

## Receipts, interruptions, and unsupported setups

Policy, cleanup receipts, the latest-receipt pointer, and pending-build state live
under `~/.local/state/dclaude/space`. Policy and receipts use versioned JSON; the
pending-build marker holds the built image ID. This state stays on the host,
outside the agent's mounted auth and cache directories. Do not expose this state
directory through a configured host mount.

Receipts record the daemon and builder identity, approved targets, completed
actions, errors, measurements, and coverage gaps. They are evidence, not a way to
resume deletion. If a run is interrupted or a target changes during cleanup,
completed work remains recorded. Inspect the result, run `--space verify` for a
fresh measurement, then preview again before applying any further cleanup.

Apply refuses to proceed when required Docker inventory or host baselines are
missing, identities are inconsistent, saved state has an unknown version, or a
receipt cannot be written. Resolve the reported problem and collect a new plan;
do not bypass it by broadening a prune command.

Mutation is supported only on macOS with one verified local Docker Desktop
daemon exposing **Engine API 1.48 or newer**, its active image store, and the
default Buildx `docker`-driver builder.
Remote contexts, other VM products, custom builders, and Linux hosts receive
coverage or unsupported-state guidance. Selecting a remote context must never
clean another machine. Select the local Desktop context identified by the report
and clear builder overrides before retrying. A read-only report with gaps is not
proof that apply is supported.

## Validation scope

CI combines controlled unit and wrapper tests with real macOS filesystem/APFS
probes and a disposable Docker Desktop installation. The Desktop scenario creates
its own images and cache records, checks protected images and containers, exercises
cleanup and retention commands, and measures actual disk-image recovery. It also
checks exact cache-ID selection while preserving the parent and every other cache
record. Controlled tests cover refused operations, changing metadata, interrupted
applies, and receipts.

The Desktop job records versions, measurements, and results in its
`storage-desktop-validation` artifact. CI also asserts that diagnosis completes
within 60 seconds on an incident-sized image inventory. Read the current workflow
result and its artifact to assess the complete run.

### Recorded recovery on Docker Desktop

[CI run 34161861303](https://github.com/stanislavkozlovski/dclaude/actions/runs/34161861303)
used Docker Desktop **4.89.0**, Engine **29.7.2**, Buildx
**0.36.1-desktop.1**, and macOS **15.7.9**, with the containerd image store.
Its image and cache cleanup stages measured these separate results:

| Cleanup stage | `Docker.raw` allocated-byte reduction | Observed APFS free-byte change |
| --- | ---: | ---: |
| Images | 270,204,928 | +268,877,824 |
| Cache | 875,442,176 | +874,074,112 |

Image/container preservation, exact single-cache-ID selection, metadata checks,
and policy checks passed. The run subsequently failed its final 284-image
inventory check at a 30-second request timeout. Overall validation is determined
by current CI, including its diagnosis-time assertion.

These measurements establish recovery on the disposable fixture. Your Mac's
versions, warm containers, snapshots, concurrent writes, and accumulated history
can produce a different result; use your own cleanup receipts to measure it.
Usability review with the intended user and repeated-build dogfooding remain
**pending**.
