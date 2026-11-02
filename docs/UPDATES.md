# Updates: new versions that install by themselves

Every change pushed to `main` builds a new image, and an instance with updates on (the default) installs it on
its own: it fetches the image, checks it, switches over, makes sure the app answers, and goes back to the
previous version when it does not. Settings > Updates shows what is running, what is out, when it last
checked, the history, and has **Update now** and **Check now** buttons. The Week page shows one line about it.

## The three parts

**The build.** `.github/workflows/image.yml` builds the image on every push to `main` and on every `v*` tag,
publishes it to `ghcr.io/kamyar94/kinoshelf` and signs it with cosign (keyless, through GitHub's own identity for
the workflow; no key is stored anywhere). Tags:

| Tag | Moves | Updater channel |
|---|---|---|
| `latest` | every change on `main` | Every change (the default) |
| `stable` | every `v*` tag | Releases only |
| `<version>-<commit>` | never | (pinning by hand) |

Each image carries its version and commit as labels (`org.opencontainers.image.version`, `.revision`); the
run's summary lists its digest and the command to check its signature.

**The updater** (service `updater` in `docker-compose.yml`) runs the same image with `kinoshelf update-agent`.
Every few hours (Settings > Updates > Check every), or when you press a button, it asks the registry which image
the channel's tag points at. When that image is newer than the running one, it downloads it by digest and checks
every part against its digest while it downloads: a part that does not match throws the whole download away.
With `update.verify: cosign` in `data/config.yml` it also checks the signature (this needs the `cosign` program
for the updater and Sigstore's servers to be reachable). It then leaves a `docker load` archive in
`data/update/incoming/` and a short note, `data/update/ready.env`. The updater has no Docker access: it cannot
start, stop or change any container.

**The host step**, `scripts/update/apply.sh`, runs on the computer that runs Docker, from a timer. It:

1. notes that it ran (`data/update/host.json`; the page shows "Host step: seen 3 min ago");
2. checks the archive's SHA-256, loads it, and checks that the loaded image is the one the updater checked;
3. keeps the running image as `kinoshelf:previous` and a copy of `data/config.yml`, points `kinoshelf:local`
   at the new image and restarts the services (`docker compose up -d --no-build`; no build, no pull);
4. waits up to three minutes for the app to answer `/api/health` with the new version;
5. if it does not, puts the previous image and settings back, restarts, and adds the image's digest to
   `data/update/rejected.txt` so it is never fetched again;
6. writes one line per update to `data/update/journal.jsonl` (time, from, to, result, seconds, reason).

It keeps the running and one previous image and removes older ones.

## Why a host step, not the Docker socket

Restarting containers needs the Docker API, and access to the Docker API is the same as root on the host: a
container that may create containers can mount the host's disk. The usual shortcut, an updater container with
the Docker socket (or a socket proxy that opens the container and image calls), puts that power in a program
that talks to the internet. A socket proxy filters by API section, not by what a request asks for, so it does
not remove the risk.

Here the work is split. The part that talks to the internet (the updater container) has no Docker access; the
part that has Docker access (the host step) never talks to the internet and only loads an archive whose
digests were checked, then starts the services exactly as your own compose file describes them. The cost is
one timer to set up on the host, which the steps below cover.

## Setting up the host step

Run it every two minutes from the folder with `docker-compose.yml`. It needs `sh`, `docker` with the compose
plugin, and `sha256sum` (or `shasum`). Nothing happens while no update is waiting.

**Linux** (as the user that runs `docker compose`), `crontab -e`:

```
*/2 * * * * cd /path/to/kinoshelf && sh scripts/update/apply.sh >> data/update/host.log 2>&1
```

**Docker inside WSL**, a Windows scheduled task (run once in PowerShell; change the distribution, user and folder):

```powershell
$a = New-ScheduledTaskAction -Execute 'wsl.exe' -Argument '-d Ubuntu -u you -- sh /path/to/kinoshelf/scripts/update/apply.sh'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName 'Kinoshelf update step' -Action $a -Trigger $t
```

**Docker Desktop on Windows**: the same task with Git for Windows' `sh.exe` (`"C:\Program Files\Git\bin\sh.exe"`
and the script's path) or through WSL as above.

The script reads `KS_DIR`, `KS_IMAGE` (default `kinoshelf:local`), `KS_CONTAINER` (default `kinoshelf`),
`KS_SERVICES` (default `app updater`) and `KS_HEALTH_SECONDS` (default 180) from its environment. Its output
holds versions and digests only.

## On a filtered network

The updater follows Settings > Network like the lookups: the source **Updates** (on by default) sends the
registry through the proxy or the route sidecar. The hosts it needs are `ghcr.io` and
`pkg-containers.githubusercontent.com` (where ghcr.io keeps the layers); with signature checks also
`tuf-repo-cdn.sigstore.dev` and `rekor.sigstore.dev`. If your proxy has an allowlist, allow these for the
updater container only (give it a fixed address in your compose file). Docker's own settings are not involved:
the host step never pulls.

## Turning it off, and updating by hand

Settings > Updates > "Install updates by themselves" off: the updater still checks and shows what is out, and
installs only when you press **Update now**. To stop checking altogether, stop the service
(`docker compose stop updater`) or leave the host step out. A version older than the running one is never
installed (set `update.allow_downgrade: true` for that), and nor is an image that was rolled back once.

The registry must allow anonymous pulls: the package's visibility on GitHub must be Public (Package settings >
Danger Zone > Change visibility), which a repository owner sets once.
