#!/bin/sh
# Downloads IBM Plex Sans and Mono (SIL Open Font License 1.1) into the app's static folder and checks
# each file against a pinned SHA-256. Used by the Dockerfile and for local development.
set -eu
DEST="${1:-src/kinoshelf/web/static/fonts}"
SANS="https://cdn.jsdelivr.net/npm/@ibm/plex-sans@1.1.0"
MONO="https://cdn.jsdelivr.net/npm/@ibm/plex-mono@2.5.0"
mkdir -p "$DEST"
fetch() { # url name sha256
  curl -sSfL --retry 3 -o "$DEST/$2" "$1"
  echo "$3  $DEST/$2" | sha256sum -c - >/dev/null || { echo "Checksum failed for $2" >&2; rm -f "$DEST/$2"; exit 1; }
}
fetch "$SANS/fonts/complete/woff2/IBMPlexSans-Regular.woff2" IBMPlexSans-Regular.woff2 ba711a3085ff9f27440b6b9c4550cfc47c97bf36591d5da958b975bb3add8c1a
fetch "$SANS/fonts/complete/woff2/IBMPlexSans-Medium.woff2" IBMPlexSans-Medium.woff2 5660f8a658f8bb50dbc005232f885eadffd2bc1c235c4f6fbb63469d1f9cde6d
fetch "$SANS/fonts/complete/woff2/IBMPlexSans-SemiBold.woff2" IBMPlexSans-SemiBold.woff2 f78048030eab62e860efa39a0df79e2e5581bf122eb95b9bc42c0b8a4988d205
fetch "$SANS/fonts/complete/woff2/IBMPlexSans-Bold.woff2" IBMPlexSans-Bold.woff2 fa7130d854a660b39a7fc9e6e0f2dc23dba5f1346e2adea3e1fe37b6d884133d
fetch "$MONO/fonts/complete/woff2/IBMPlexMono-Regular.woff2" IBMPlexMono-Regular.woff2 ba204497f16b6d334cee9d1e963a831b73e3a56e1d6300a8489d18df7214b350
fetch "$MONO/fonts/complete/woff2/IBMPlexMono-Medium.woff2" IBMPlexMono-Medium.woff2 33faf307fa6031fb4062276d7320a6d632de890cbb347576fd80cfa01077bc25
fetch "$SANS/LICENSE.txt" OFL.txt 7e6b2818edbd8f6a01ae80641cc8f16a51080d08fb4e532be3a0b6f74adb07da
echo "Fonts ready in $DEST"
