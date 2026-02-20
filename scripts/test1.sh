#!/bin/bash

AN_H=480
AN_FPS=0.2
OUT_CODEC=h.264
IN="/arch03/V/compr/20250509-11Wawel/20250509_163831_4096x2160_120fps_17147076951.mov"
OUT_PROXY="proxy"

ffmpeg -hide_banner -y -i "$IN" -map 0:v:0 -an \
  -vf "fps=${AN_FPS},scale=-2:${AN_H}:flags=fast_bilinear,format=yuv420p" \
  -c:v libx264 -preset veryfast -crf 18 -g 1 -keyint_min 1 -sc_threshold 0 -bf 0 \
  -movflags +faststart "$OUT_PROXY.mp4"
