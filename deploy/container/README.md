# VBC container image (HomeStack)

Runtime contract of `ghcr.io/hipotures/vbc`, the image HomeStack deploys as an
unprivileged Proxmox LXC (`.homestack/services/vbc/`). Background:
`docs/architecture/service-split.md` ("Deployment: HomeStack").

## Base image and why

`debian:testing-slim` (pinned by digest) with distribution packages only:
FFmpeg 9.0.2 (SVT-AV1 and NVENC `av1_nvenc`), ExifTool 13.55, Python 3.14.
Python dependencies come from `uv.lock` (`uv sync --frozen --no-dev`).

* FFmpeg ≥ 8 is required. Multipart (manifest) jobs pass
  `-filter_buffered_frames` to bound memory, and Debian trixie's FFmpeg 7.1
  fails them with exit code 8.
* NVENC needs glibc. The NVIDIA driver libraries loaded at run time are
  glibc-only, so Alpine (FFmpeg 9 but musl, no NVENC) was rejected.
* Debian testing rather than Ubuntu 26.10 (FFmpeg 8.1.2): the owner's choice
  was the same FFmpeg as the development host, without hunting regressions on
  an older release.
* testing moves. The base is pinned, but the packages are whatever testing
  ships at build time, so the build fails unless `-filter_buffered_frames`,
  `libsvtav1` and `av1_nvenc` are present.

Unpacked size: about 490 MB.

## Process, ports, state

| Item | Value |
|---|---|
| Entrypoint | `vbc-entrypoint` → `vbc serve --config /home/app/config/vbc.yaml` |
| User | container root (an unprivileged LXC maps it to an unprivileged host user) |
| Port | `8080/tcp` (HomeStack convention; local `vbc serve` defaults to 8770) HTTP: API, `/health/live`, `/health/ready`, `/.well-known/homestack/status` |
| Persistent volume | `/home/app` (`config/vbc.yaml` incl. the Dirs list, `state/`) |
| Logs | stdout/stderr |
| Stop | SIGTERM → running jobs finish (≤ `server.stop_timeout_s`, 600 s), then exit 0 |

* The entrypoint refuses to start unless `/home/app` is a mounted volume.
  `VBC_ALLOW_EPHEMERAL=1` overrides this, for throwaway tests only.
* On the first start it writes `config/vbc.yaml` from `conf/vbc.yaml.example`:
  no input dirs, CPU encoder, server on `0.0.0.0:8080`. It never overwrites an
  existing config.
* Storage roots that lie on the volume (e.g. `/home/app/media`) are created at
  start; roots elsewhere are mounts and must exist (readiness checks them).
* Nothing durable is written to the root filesystem. Encoder temp files live
  next to the outputs on the media mounts.

## Settings provided at deployment (never their values in the repo)

| Input | Environment | Notes |
|---|---|---|
| Extra storage roots | `VBC_STORAGE_ROOTS` | usually empty: every media mount of the container is a storage root by itself (read from `/proc/self/mountinfo` on each use, no restart); input dirs and their `_out`/`_err` siblings must lie inside a root |
| Allowed host names | `VBC_ALLOWED_HOSTS` | usually empty: the server accepts its host name, `<hostname>.<domain>` (resolv.conf search domains) and IP addresses by itself; add only other names clients use (e.g. a Pangolin subdomain) |
| Heartbeat | `HOMESTACK_HEARTBEAT_SECONDS` | set by HomeStack from the manifest (60) |

Input folders are added at run time: `vbc dirs add PATH` or the TUI Dirs tab.
At start, on `R` in the Dirs tab and on `vbc dirs scan`, the server offers the
folders directly inside each media mount that hold videos or manifests, as
**disabled** entries; enable the right ones (Space, then S). Each folder is
offered once (`storage.discovered` in the config), so a removed one does not
come back. A folder with JSON manifests gets `metadata` and `watch` by itself,
and a folder on NFS/CIFS is watched by polling (10 s).
Media folders need HomeStack external mounts, which are a pending HomeStack
feature. Mount them at the same path the producers use (identity mounts):
manifests carry absolute paths.

## GPU (NVENC)

The image contains FFmpeg with NVENC but **no NVIDIA driver libraries**. To
encode on the GPU inside the LXC:

1. Attach `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm` and
   `/dev/nvidia-uvm-tools` to the container in Proxmox, then declare them as
   `runtime.devices` in `service.yaml`. HomeStack refuses new deployments with
   devices it would have to attach itself.
2. Provide `libcuda.so.1` and `libnvidia-encode.so.1` in the exact version
   of the host driver. The options are a HomeStack external mount of the
   host's driver library directory (pending feature) or an image variant that
   installs that driver version.
3. Set `general.gpu: true` in `/home/app/config/vbc.yaml`.

Without these the server encodes on the CPU (SVT-AV1), which is correct but
very slow for real footage.

## Measurements (evidence for `defaults.yaml`)

```text
Image / commit:      vbc:dev built from deploy/container/Containerfile (service-split branch)
Base image and why:  debian:testing-slim, see above
Runtime used:        OCI runtime (podman 6.1.4, rootless), not an LXC
Workload/duration:   e2e fixture set (9 files + 1 manifest, CPU) ~20 s;
                     2 parallel 1080p 2 s clips (CPU SVT-AV1, threads 2), sampled every 0.2 s
CPU declared:        4 cores (--cpus 4 in the test)
Memory:              idle server ~75 MiB RSS; peak anonymous 2683 MiB for two parallel
                     1080p SVT-AV1 jobs; no OOM at a 3 GiB limit -> declared 4G
Swap declared:       0M
Rootfs declared:     2G (image ~490 MB unpacked; no durable writes)
Home declared/used:  1G / a few KiB (config + state)
Health/telemetry:    /health/live, /health/ready 200 with heartbeat checks;
                     telemetry schema 3 with process_rss_bytes (tests/e2e/test_image.py)
Restart/fresh root:  refuses without a mounted /home/app; first start creates the config,
                     later starts keep it; SIGTERM exit 0 within seconds
Not tested:          Proxmox LXC replacement; NVENC (no GPU in the test runtime);
                     4K CPU encoding memory; a stalled main path turning health to 503
```

## Build and test locally

```bash
podman build -f deploy/container/Containerfile -t vbc:dev .
VBC_E2E_IMAGE=vbc:dev uv run pytest tests/e2e/test_image.py
```

## Release

`scripts/tag-image.py` creates the next `vbc-X.Y.Z` tag on a clean, pushed
HEAD (`--dry-run` shows it). The `Image` workflow publishes
`ghcr.io/hipotures/vbc:X.Y.Z`, and a manual workflow run publishes
`dev-<commit>`. Published versions are immutable.

Only changes to the image need a release: application code, dependencies, the
Containerfile, the entrypoint or the default config. Changes to the manifest,
documentation or tests do not.
