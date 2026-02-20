#!/bin/bash

AN_H=480
AN_FPS=0.2
OUT_CODEC=h.264
IN="/arch03/V/compr/20250509-11Wawel/20250509_163831_4096x2160_120fps_17147076951.mov"
OUT_PROXY="proxy-gpu"

ffmpeg -hide_banner -y -hwaccel cuda -hwaccel_output_format cuda -i "$IN" -map 0:v:0 -an \
  -vf "fps=${AN_FPS},scale_cuda=w=-2:h=${AN_H}:format=nv12" \
  -c:v h264_nvenc -preset p1 -tune ll -rc vbr -cq 23 -b:v 0 -g 1 -bf 0 \
  "$OUT_PROXY.mp4"
