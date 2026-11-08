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
config="$home/config/vbc.yaml"
if [ ! -e "$config" ]; then
    cp /opt/vbc/conf/container-default.yaml "$config"
    echo "vbc: created first-start config $config"
fi

exec python -m vbc.cli serve --config "$config" "$@"
