#!/bin/bash
set -euo pipefail

AN_H=480
AN_FPS=0.2
OUT_CODEC=h.264
IN="${1:?Usage: scripts/manual_proxy_cpu.sh INPUT_VIDEO [OUTPUT_PREFIX]}"
OUT_PROXY="${2:-proxy}"

ffmpeg -hide_banner -y -i "$IN" -map 0:v:0 -an \
  -vf "fps=${AN_FPS},scale=-2:${AN_H}:flags=fast_bilinear,format=yuv420p" \
  -c:v libx264 -preset veryfast -crf 18 -g 1 -keyint_min 1 -sc_threshold 0 -bf 0 \
  -movflags +faststart "$OUT_PROXY.mp4"
