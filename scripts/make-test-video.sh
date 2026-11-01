#!/usr/bin/env bash
# Generate deterministic test videos into tests/fixtures/ using ffmpeg inside the app image
# (no host ffmpeg needed). Known content makes pipeline assertions exact:
#   bulletin-3scenes.mp4 : 30 s, 640x360, scenes red [0-10) blue [10-20) green [20-30), 440 Hz tone
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p tests/fixtures
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/tests/fixtures:/out" --entrypoint ffmpeg newsintel-app:"${APP_IMAGE_TAG:-dev}" \
  -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=0xDC1E1E:s=640x360:d=10:r=25" \
  -f lavfi -i "color=c=0x1E3CDC:s=640x360:d=10:r=25" \
  -f lavfi -i "color=c=0x1EB43C:s=640x360:d=10:r=25" \
  -f lavfi -i "sine=frequency=440:sample_rate=16000:duration=30" \
  -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]" \
  -map "[v]" -map 3:a -c:v libx264 -pix_fmt yuv420p -preset veryfast -c:a aac -b:a 64k \
  -metadata title="Test bulletin" /out/bulletin-3scenes.mp4
ls -l tests/fixtures/bulletin-3scenes.mp4
