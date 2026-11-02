#!/bin/sh
# Kinoshelf host step: installs an update the updater container fetched and checked (docs/UPDATES.md).
#
# Runs on the host (a cron job, a systemd timer, or a Windows scheduled task through wsl.exe), never inside a
# container: no container gets the Docker socket. Every few minutes it:
#   1. notes that it ran (data/update/host.json), and stops when no update is waiting (data/update/ready.env);
#   2. checks the archive's SHA-256, loads it, and checks the loaded image is the one the updater checked;
#      then asks the new image whether it can carry out the current settings, and refuses it when not;
#   3. keeps the running image as <image>:previous and a copy of data/config.yml, switches the tag, and
#      restarts the services with docker compose (no build, no pull);
#   4. waits until the app answers /api/health with the new version; if it does not in time, puts the previous
#      image and settings back, restarts, and remembers the bad image so it is never fetched again;
#   5. writes one line per update to data/update/journal.jsonl.
#
# Settings (environment): KS_DIR (the folder with docker-compose.yml; default: two levels above this script),
# KS_IMAGE (the tag the compose file runs, default kinoshelf:local), KS_CONTAINER (default kinoshelf),
# KS_SERVICES (default "app updater"), KS_HEALTH_SECONDS (default 180), KS_HEALTH_STEP (default 5),
# KS_DOCKER (default docker).
set -eu

DIR=${KS_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$DIR"
U=data/update
IMAGE=${KS_IMAGE:-kinoshelf:local}
REPO=${IMAGE%:*}
PREV="$REPO:previous"
CAND=kinoshelf:candidate
CONTAINER=${KS_CONTAINER:-kinoshelf}
SERVICES=${KS_SERVICES:-app updater}
WAIT=${KS_HEALTH_SECONDS:-180}
STEP=${KS_HEALTH_STEP:-5}
DOCKER=${KS_DOCKER:-docker}

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@" | cut -d' ' -f1; else shasum -a 256 "$@" | cut -d' ' -f1; fi; }
label() { $DOCKER image inspect -f "{{index .Config.Labels \"org.opencontainers.image.$2\"}}" "$1" 2>/dev/null | sed 's/<no value>//' | tr -cd 'A-Za-z0-9._:+-'; }

mkdir -p "$U"
printf '{"seen": "%s", "image": "%s"}\n' "$(now)" "$IMAGE" > "$U/host.json.tmp" && mv "$U/host.json.tmp" "$U/host.json"
[ -f "$U/ready.env" ] || exit 0
mkdir "$U/.applying" 2>/dev/null || { echo "Another update is being installed."; exit 0; }
trap 'rmdir "$U/.applying" 2>/dev/null || true' EXIT

VERSION=; REVISION=; DIGEST=; MANIFEST=; CONFIG=; LAYERS=; FILE=; SHA256=
while IFS='=' read -r k v; do
  case "$v" in *[!A-Za-z0-9._:+-]*) echo "ready.env holds an unexpected value; nothing installed."; exit 1 ;; esac
  case "$k" in
    VERSION) VERSION=$v ;; REVISION) REVISION=$v ;; DIGEST) DIGEST=$v ;; MANIFEST) MANIFEST=$v ;;
    CONFIG) CONFIG=$v ;; LAYERS) LAYERS=$v ;; FILE) FILE=$v ;; SHA256) SHA256=$v ;;
  esac
done < "$U/ready.env"

FROM_VERSION=$(label "$IMAGE" version); FROM_REVISION=$(label "$IMAGE" revision)
entry() {  # event reason seconds
  printf '{"at": "%s", "event": "%s", "from": {"version": "%s", "revision": "%s"}, "to": {"version": "%s", "revision": "%s", "digest": "%s"}, "seconds": %s, "reason": "%s"}\n' \
    "$(now)" "$1" "$FROM_VERSION" "$FROM_REVISION" "$VERSION" "$REVISION" "$DIGEST" "${3:-0}" "$2" >> "$U/journal.jsonl"
}
done_with() { rm -f "$U/ready.env" "$U/ready.json"; [ -n "$FILE" ] && rm -f "$U/incoming/$FILE"; true; }
fail() { entry failed "$1"; done_with; echo "Not installed: $1"; exit 1; }

case "$FILE" in ''|*/*|.*) fail "the archive name is not valid" ;; esac
[ -f "$U/incoming/$FILE" ] || fail "the fetched archive is missing"
[ "$(sha256 "$U/incoming/$FILE")" = "$SHA256" ] || fail "the archive does not match its checksum"
$DOCKER load -q -i "$U/incoming/$FILE" >/dev/null 2>&1 || fail "docker load did not accept the archive"
ID=$($DOCKER image inspect -f '{{.Id}}' "$CAND" 2>/dev/null) || fail "the loaded image is missing"
# The classic image store names an image by its config digest. The containerd store gives a loaded image a new
# name, so there the layer list is compared: docker load has already checked every layer against that list.
LOADED_LAYERS=$($DOCKER image inspect -f '{{range .RootFS.Layers}}{{println .}}{{end}}' "$CAND" 2>/dev/null | tr -d '\r' | sed '/^$/d' | sha256)
[ "$ID" = "sha256:$CONFIG" ] || [ "$ID" = "$MANIFEST" ] || [ "$ID" = "$DIGEST" ] || \
  { [ -n "$LAYERS" ] && [ "$LOADED_LAYERS" = "$LAYERS" ]; } || {
  $DOCKER image rm "$CAND" >/dev/null 2>&1 || true; fail "the loaded image is not the one the updater checked"; }

# The new version must be able to carry out the current settings (a filing method, hook or rule it lacks would
# break this instance): it reads them on stdin, with no network and no mounts.
if [ -f data/config.yml ]; then
  if ! CHECK=$($DOCKER run --rm -i --network none "$CAND" kinoshelf check-config - < data/config.yml 2>&1); then
    WHY=$(printf '%s\n' "$CHECK" | grep -v '^Settings ' | head -3 | tr '\n' ';' | tr -cd 'A-Za-z0-9 ._:;,()/-' | cut -c1-240)
    echo "$DIGEST" >> "$U/rejected.txt"
    entry refused "the new version cannot use these settings: ${WHY:-it could not read them}"
    done_with
    $DOCKER image rm "$CAND" >/dev/null 2>&1 || true
    echo "Refused: the new version cannot use these settings."
    exit 1
  fi
fi

CUR=$($DOCKER image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)
OLDPREV=$($DOCKER image inspect -f '{{.Id}}' "$PREV" 2>/dev/null || true)
[ -f data/config.yml ] && cp -p data/config.yml "$U/config.before-update.yml"
[ -n "$CUR" ] && $DOCKER tag "$IMAGE" "$PREV"
$DOCKER tag "$CAND" "$IMAGE"

answers() {  # wait until the app reports version $1 and revision $2 (either may be empty: then any)
  t=0
  while [ "$t" -lt "$WAIT" ]; do
    v=$($DOCKER exec "$CONTAINER" python -c "import json,urllib.request;h=json.load(urllib.request.urlopen('http://127.0.0.1:8484/api/health',timeout=5));print(h.get('version',''),h.get('revision',''),sep='|')" 2>/dev/null || true)
    if [ -n "$v" ] && { [ -z "$1" ] || [ "${v%%|*}" = "$1" ]; } && { [ -z "$2" ] || [ "${v#*|}" = "$2" ]; }; then
      return 0
    fi
    sleep "$STEP"; t=$((t + STEP))
  done
  return 1
}

START=$(date +%s)
$DOCKER compose up -d --no-build $SERVICES >/dev/null 2>&1 || true
if answers "$VERSION" "$REVISION"; then
  SECS=$(( $(date +%s) - START ))
  entry updated "healthy after $SECS s" "$SECS"
  printf '{"seen": "%s", "image": "%s", "current": {"version": "%s", "revision": "%s", "digest": "%s"}}\n' \
    "$(now)" "$IMAGE" "$VERSION" "$REVISION" "$DIGEST" > "$U/host.json.tmp" && mv "$U/host.json.tmp" "$U/host.json"
  done_with
  $DOCKER image rm "$CAND" >/dev/null 2>&1 || true            # only the name; the image runs as $IMAGE
  if [ -n "$OLDPREV" ] && [ "$OLDPREV" != "$CUR" ] && [ "$OLDPREV" != "$ID" ]; then
    $DOCKER image rm "$OLDPREV" >/dev/null 2>&1 || true        # keep the running and one previous image only
  fi
  echo "Installed $VERSION."
  exit 0
fi

# The new version did not answer: back to the previous image and settings.
SECS=$(( $(date +%s) - START ))
echo "$DIGEST" >> "$U/rejected.txt"
if [ -n "$CUR" ]; then
  $DOCKER tag "$PREV" "$IMAGE"
  if [ -n "$OLDPREV" ]; then $DOCKER tag "$OLDPREV" "$PREV" || true; fi  # :previous: the one before the running one
  [ -f "$U/config.before-update.yml" ] && cp -p "$U/config.before-update.yml" data/config.yml
  $DOCKER compose up -d --no-build $SERVICES >/dev/null 2>&1 || true
  if answers "$FROM_VERSION" "$FROM_REVISION"; then BACK="the previous version answers again"; else BACK="the previous version did not answer either"; fi
else
  BACK="there was no previous image to go back to"
fi
entry rolled_back "no answer with version $VERSION within $WAIT s; $BACK" "$SECS"
done_with
$DOCKER image rm "$CAND" >/dev/null 2>&1 || true
echo "Rolled back: $BACK."
exit 1
