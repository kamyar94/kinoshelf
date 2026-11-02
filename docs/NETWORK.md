# Network: how lookups reach the internet

Kinoshelf goes online only to look things up: TMDB, OMDb, OpenSubtitles, TVmaze, the IMDb datasets
and posters, plus your Jellyfin server. On most networks that works as it is. On a filtered network
some of these sites are blocked, for example by DNS answers that point to a private address or by
connections that are reset during the secure handshake. Settings > Network lets you send lookups
another way and shows you, per source, whether they get through.

## Three ways out

**Direct** is the default: every request leaves straight from the computer that runs Kinoshelf.

**Proxy** sends the sources you pick through an HTTP or SOCKS5 proxy that you run, with an optional
username and password. Type the address as `host:port` (for example `proxy.example.invalid:8080`) or
paste a whole proxy URL such as `socks5://host:port`; Kinoshelf splits it into its parts. With SOCKS5
the proxy also looks the names up, so a poisoned local DNS does not matter. A proxy that runs on the
Docker host is reached as `host.docker.internal:<port>`.

**Route sidecar** sends them through an optional Xray container that runs beside Kinoshelf (compose
profile `route`). Use it when the way out is a VLESS, VMess, Trojan or Shadowsocks server rather than
a plain proxy. Setting it up is described below.

Under **Sources that use it** you choose which sources take the proxy or the sidecar. Jellyfin is off
by default because it is normally on your own network, and an address on your own network (a
private or loopback IP, `localhost`, a one-word name such as a Docker service, or a name ending in
`.local`, `.lan` or `.home.arpa`) always goes direct, whatever the toggle says: a proxy cannot reach
your LAN. Posters follow their source (TMDB posters go the way TMDB goes). **Updates** is the updater
container's image registry (`ghcr.io` and `pkg-containers.githubusercontent.com`, see
[UPDATES.md](UPDATES.md)). Hooks of the http kind are your own services and are always reached directly.

Kinoshelf ignores the `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` environment variables: the Network
settings are the one place that decides.

## What stays secret

The proxy's address, username and password are secrets. The page never shows them once saved, not
even in part: each one shows **set** with a **Replace** button. They are kept only in
`data/config.yml` (mode 600). They are not written to the log, to error messages, to the job files
or requests that hooks receive, or to backups (`kinoshelf backup` leaves them out; set them again after
a restore). Typing a new address forgets the old username and password, so a sign-in is never sent to
a different server.

The route sidecar's server details (address, ID, path, TLS name) never reach Kinoshelf at all. They
live in one file on the host, `route/config.json`, which only the sidecar reads. That file is ignored
by git and by the Docker build. Keep it out of screenshots and chats.

API keys travel only inside HTTPS to the real source. A proxy or the sidecar sees which site is asked,
never the key, and the certificate check makes sure the answer comes from the real site.

## Test connections

The button sends one request to each source along the route your saved settings give it, without an
API key, and lists for each one whether it was reached, how long it took and why:

| Reason | What it means |
|---|---|
| HTTP 200, or HTTP 401 / 403 with "no key sent" | the real site answered: the route works |
| DNS answered a private address | the name was looked up locally and came back as an address inside a private network, a common filter trick; nothing was sent there |
| DNS found no address | the name could not be looked up |
| Connection reset | something cut the connection, often a filter reacting to the site's name |
| Connection cut during the secure handshake | the same, seen a step later |
| Timed out after N s | no answer within the time limit |
| HTTP 5xx | the site, or a proxy in between, answered with an error |
| The proxy is not answering / could not reach it / did not accept the username or password | the proxy or the sidecar is down, cannot get out, or refused the sign-in |

The requests are: TMDB `/3/configuration`, the IMDb `title.basics.tsv.gz` headers only, TVmaze
`/shows/1`, the OMDb front door, OpenSubtitles `/infos/formats`, and Jellyfin's public
`/System/Info/Public` when its address is set.

## Setting up the route sidecar

1. Copy `route.example.json` to `route/config.json` beside `docker-compose.yml` and put your
   server's details in the `outbounds` part. Most clients can export a full Xray config for a share
   link; keep the `inbounds` part as it is, because Kinoshelf talks to the sidecar on port 10809 inside
   Docker. The example keeps Xray's own logs off so the server's address does not end up in
   `docker logs`.
2. Add `route` to `COMPOSE_PROFILES` in `.env` (for example `COMPOSE_PROFILES=route` or
   `tunnel,route`) and run `scripts/start.sh` (Windows: `.\scripts\Start.ps1`) again. The script
   refuses to start without `route/config.json` and makes the file readable by you only.
3. In Settings > Network pick **Route sidecar** and press **Test connections**.

The example's inbound listens on all of the sidecar's addresses (`0.0.0.0`), and that matters: the app and the
updater reach the sidecar over Docker networks, and the sidecar publishes no port, so nothing outside
Docker can use it. If you write your own config and restrict the inbound to one address, the containers on other
networks (the updater, for example) cannot reach it; either listen on all addresses and allow or refuse by source
address in the routing rules, or give each of them an inbound of its own. The same holds for a proxy container of
your own that the updater should use (see [UPDATES.md](UPDATES.md)).

The sidecar uses the official image `ghcr.io/xtls/xray-core`, pinned by `XRAY_VERSION` in `.env`
(26.9.30 when this was written). It runs as your user with a read-only root filesystem and no Linux
capabilities, publishes no port, and shares only an internal network with the app. The app image
does not grow for it; the image Docker pulls for the sidecar is a separate download of about 23 MB.
SOCKS5 support in the app image adds one small library (socksio, about 30 KB).

Why the sidecar's config stays on the host instead of being pasted into the page: the share link holds
everything needed to use your server. Kept in a host file, it never travels through a browser or a
tunnel, never sits in Kinoshelf's settings or backups, and Kinoshelf needs no code that reads it.

## Upgrading from 0.1.0

0.1.0 had one setting, `network.proxy`, a proxy URL used for every lookup and for Jellyfin. On the
first start of 0.1.1 it becomes the **Proxy** choice with the URL split into kind, address, username
and password, and `data/config.yml` is written once in the new shape. All internet sources keep using
the proxy; Jellyfin now goes direct (turn it on under Sources that use it if yours is reached through
the proxy). An address that points at a proxy container you run yourself, such as
`http://xray:10809`, keeps working as a plain proxy.
