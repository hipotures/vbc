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

Unpacked size: about 670 MB (about 180 MB of it the bundled NVIDIA libraries).

## Process, ports, state

| Item | Value |
|---|---|
| Entrypoint | `vbc-entrypoint` → `vbc serve --config /home/app/config/vbc.yaml` |
| User | container root (an unprivileged LXC maps it to an unprivileged host user) |
| Port | `8080/tcp` (HomeStack convention; local `vbc serve` defaults to 8770) HTTP: API, `/health/live`, `/health/ready`, `/.well-known/homestack/status` |
| Persistent volume | `/home/app` (`config/vbc.yaml` incl. the Dirs list, `state/`) |
| Logs | stdout/stderr |
| Stop | SIGTERM → running jobs finish (≤ `server.stop_timeout_s`, 600 s), then exit 0 |

The HomeStack manifest sets `runtime.stop_timeout_seconds: 600`, allowing
10 minutes for running jobs to finish during image updates.

* The entrypoint refuses to start unless `/home/app` is a mounted volume.
* `VBC_UID`/`VBC_GID` (default 0): run the server as that user, e.g. the media
  owner's 1000, so it has exactly that user's permissions (no ACLs). The
  container's uid must map to the same host uid (`lxc.idmap`, root on the
  host). The volume's config and state directories must already be writable
  by that user; the entrypoint does not change ownership or permissions. Without
  `VBC_UID`, a uid the container maps to the same host uid (`u 1000 1000 1`)
  is used automatically.
  `VBC_ALLOW_EPHEMERAL=1` overrides this, for throwaway tests only.
* On the first start it writes `config/vbc.yaml` from `conf/vbc.yaml.example`:
  no input dirs, CPU encoder, server on `0.0.0.0:8080`. It never overwrites an
  existing config.
* Storage roots that lie on the volume (e.g. `/home/app/media`) are created at
  start; roots elsewhere are mounts and must exist. Readiness fails for a root that is
  not writable only when it is configured or holds an enabled input folder;
  an unused unwritable mount is a `warn` check.
* Nothing durable is written to the root filesystem. Encoder temp files live
  next to the outputs on the media mounts.

## Settings provided at deployment (never their values in the repo)

| Input | Environment | Notes |
|---|---|---|
| Extra storage roots | `VBC_STORAGE_ROOTS` | usually empty: every media mount of the container is a storage root by itself (read from `/proc/self/mountinfo` on each use, no restart); input dirs and their `_out`/`_err` siblings must lie inside a root |
| Allowed host names | `VBC_ALLOWED_HOSTS` | usually empty: the server accepts its host name, `<hostname>.<domain>` (resolv.conf search domains) and IP addresses by itself; add only other names clients use (e.g. a Pangolin subdomain) |
| GPU | `VBC_GPU` | image default `auto`: NVENC probed at every start, GPU when usable, CPU otherwise (overrides `general.gpu` of the volume config); `cpu` forces CPU |
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

FFmpeg in the image has NVENC. `vbc serve` (with the image default
`VBC_GPU=auto`) encodes a few test frames with `av1_nvenc` at every start and
uses the GPU when that works, else SVT-AV1 on the CPU (logged either way).

The container needs the GPU device nodes and NVIDIA user-space libraries of
the host driver's exact version. Libraries, first that applies:

1. **Runtime-provided**: the NVIDIA container toolkit (Docker/Podman with
   `--gpus all`) injects the host's; the image sets `NVIDIA_DRIVER_CAPABILITIES`
   incl. `video`.
2. **Host directory, read-only** (recommended on Proxmox LXC): mount the
   host's library directory at `/opt/host-lib` (`VBC_HOST_LIBS`), e.g.
   `pct set <id> -mp3 /usr/lib/x86_64-linux-gnu,mp=/opt/host-lib,ro=1`. Only
   `libcuda*`, `libnvidia-*` and `libnvcuvid*` are linked. A host driver
   update then needs a container restart, not a new image.
3. **Bundled**: `/opt/nvidia/lib` holds the libraries of one driver version
   (`NVIDIA_DRIVER_VERSION` build argument, the owner's host, 620.70.06), from
   NVIDIA's installer with a pinned SHA-256. After a host driver update they
   no longer match: the server encodes on CPU until the image is rebuilt with
   the new version (or option 2 is used).

GPU panel: `nvidia-smi` from the same driver package is bundled in
`/opt/nvidia/bin` (an `nvidia-smi` injected by the runtime takes precedence);
it gives encoder/decoder use besides temperature, fan, power and memory.
`nvtop` stays in the image and is used when `nvidia-smi` is missing or fails.

Devices: `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`,
`/dev/nvidia-uvm-tools` attached to the container in Proxmox, then declared as
`runtime.devices` in `service.yaml` (HomeStack refuses deployments with
devices it would have to attach itself).

Test with a local GPU: `VBC_E2E_IMAGE=vbc:dev VBC_E2E_GPU_LIBS=/usr/lib uv run
pytest tests/e2e/test_image.py` (host libraries of a different driver version
than the bundled ones prove option 2).

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
Not tested:          Proxmox LXC replacement; NVENC inside an LXC (tested with podman --device on an RTX 4070 Ti);
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
