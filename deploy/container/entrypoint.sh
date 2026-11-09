#!/bin/sh
# VBC container entrypoint: check the persistent volume, create the first-start
# config if it is missing (never overwrite), then exec `vbc serve`.
set -eu

home="${VBC_HOME:-/home/app}"

if [ "${VBC_ALLOW_EPHEMERAL:-0}" != "1" ]; then
    if [ -L "$home" ]; then
        echo "vbc: refusing to start: $home is a symlink, not the persistent volume" >&2
        exit 1
    fi
    if ! python -c 'import os, sys; sys.exit(0 if os.path.ismount(sys.argv[1]) else 1)' "$home"; then
        echo "vbc: refusing to start: $home is not a mounted volume" >&2
        echo "vbc: (set VBC_ALLOW_EPHEMERAL=1 only for throwaway tests)" >&2
        exit 1
    fi
fi

mkdir -p "$home/config" "$home/state"

# Storage roots on the persistent volume (e.g. /home/app/media) are created
# here; roots elsewhere are mounts and must already exist (readiness checks them).
old_ifs=$IFS
IFS=,
for root in ${VBC_STORAGE_ROOTS:-}; do
    case "$root" in
        "$home"/*) mkdir -p "$root" ;;
    esac
done
IFS=$old_ifs
config="$home/config/vbc.yaml"
if [ ! -e "$config" ]; then
    cp /opt/vbc/conf/container-default.yaml "$config"
    echo "vbc: created first-start config $config"
fi

# NVIDIA driver libraries, first that applies:
#  1. the runtime's own (NVIDIA container toolkit injects the host's);
#  2. the host's library directory mounted read-only at $VBC_HOST_LIBS
#     (e.g. pct set <id> -mp3 /usr/lib/x86_64-linux-gnu,mp=/opt/host-lib,ro=1):
#     only libcuda/libnvidia*/libnvcuvid are linked, so a host driver update
#     needs a restart, not a new image;
#  3. the bundled ones for the owner's host driver (/opt/nvidia/lib).
# `vbc serve` probes NVENC at start and encodes on CPU when none works.
host_libs="${VBC_HOST_LIBS:-/opt/host-lib}"
host_version="$(sed -n 's/.*Kernel Module[^0-9]*\([0-9][0-9.]*\).*/\1/p' /proc/driver/nvidia/version 2>/dev/null | head -n 1)"
if ! ldconfig -p | grep -q 'libcuda.so.1 '; then
    if ls "$host_libs"/libcuda.so.* >/dev/null 2>&1; then
        linked=/tmp/vbc-host-nvidia
        rm -rf "$linked" && mkdir -p "$linked"
        for lib in "$host_libs"/libcuda.so.* "$host_libs"/libnvidia-*.so.* "$host_libs"/libnvcuvid.so.*; do
            [ -e "$lib" ] && ln -sf "$lib" "$linked/"
        done
        for lib in "$linked"/*.so.*.*; do  # soname links (libcuda.so.1 -> libcuda.so.620.70.06)
            [ -e "$lib" ] || continue
            base="${lib%%.so.*}"
            [ -e "$base.so.1" ] || ln -s "$(basename "$lib")" "$base.so.1"
        done
        export LD_LIBRARY_PATH="$linked${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        echo "vbc: NVIDIA libraries from the host ($host_libs); kernel module ${host_version:-none}"
    elif [ -d /opt/nvidia/lib ]; then
        export LD_LIBRARY_PATH="/opt/nvidia/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        echo "vbc: NVIDIA libraries $(cat /opt/nvidia/VERSION) bundled; kernel module ${host_version:-none}"
    fi
fi

# Run as the owner's user when VBC_UID is set (e.g. 1000): files the server
# creates belong to that user and it deletes only what that user may delete.
# Needs the container's uid VBC_UID mapped to the same host uid (lxc.idmap);
# without it the default (container root) keeps working as before.
# Without VBC_UID: a single non-root uid that the container maps to the same
# host uid (lxc.idmap "u 1000 1000 1") is the owner's user; run as it.
identity_id() {
    awk '$1 == $2 && $1 != 0 && $3 == 1 { print $1; exit }' "$1" 2>/dev/null
}
uid="${VBC_UID:-$(identity_id /proc/self/uid_map)}"
uid="${uid:-0}"
gid="${VBC_GID:-$(identity_id /proc/self/gid_map)}"
gid="${gid:-$uid}"
if [ "$uid" != "0" ]; then
    # The persistent volume (config, state) must be writable by that user.
    find "$home" -xdev \( ! -uid "$uid" -o ! -gid "$gid" \) -exec chown -h "$uid:$gid" {} + 2>/dev/null || true
    echo "vbc: running as uid $uid gid $gid"
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --inh-caps=-all \
        python -m vbc.cli serve --config "$config" "$@"
fi
exec python -m vbc.cli serve --config "$config" "$@"
