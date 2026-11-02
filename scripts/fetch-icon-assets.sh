#!/bin/sh
# Downloads what the folder-icon maker needs besides the template layers in src/kinoshelf/icons/assets/:
#   Oswald (SIL Open Font License 1.1) for the tab title, from the Google Fonts repository
#   the PP-OCRv3 text detector (Apache-2.0) and the YuNet face detector (MIT) from the OpenCV model zoo
# and checks each file against a pinned SHA-256. Used by the Dockerfile; for local development run it and set
# KS_ICON_ASSETS to the folder.
set -eu
DEST="${1:-.icon-assets}"
FONTS="https://cdn.jsdelivr.net/gh/google/fonts@8b0a1d0f5983c89bc2b93f1b5fb55f9e252744b5/ofl/oswald"
ZOO="https://github.com/opencv/opencv_zoo/raw/main/models"
HF="https://huggingface.co/opencv"
mkdir -p "$DEST"
fetch() { # name sha256 url [fallback url]
  name="$1"; sum="$2"; shift 2
  for url in "$@"; do
    if curl -sSfL --retry 3 -o "$DEST/$name" "$url" && echo "$sum  $DEST/$name" | sha256sum -c - >/dev/null 2>&1; then
      return 0
    fi
  done
  echo "Could not fetch $name with the expected checksum" >&2
  rm -f "$DEST/$name"
  exit 1
}
fetch "Oswald[wght].ttf" 5b38c246e255a12f5712d640d56bcced0472466fc68983d2d0410ec0457c2817 "$FONTS/Oswald%5Bwght%5D.ttf"
fetch OFL-Oswald.txt 0fd731a904b729a4e02eaf5e8ebd06783edd9abe400e8882760160230675b652 "$FONTS/OFL.txt"
fetch text_detection_en_ppocrv3_2023may.onnx 03f550c6b406fda8bf54bd8327815f6c7e2edd98cea02348c93d879254366587 \
  "$ZOO/text_detection_ppocr/text_detection_en_ppocrv3_2023may.onnx" \
  "$HF/text_detection_ppocr/resolve/main/text_detection_en_ppocrv3_2023may.onnx"
fetch face_detection_yunet_2023mar.onnx 8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4 \
  "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  "$HF/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"
echo "Icon assets ready in $DEST"
