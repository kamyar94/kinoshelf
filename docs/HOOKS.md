# Hooks

A hook is an optional step that runs on a file after it has been identified and given its final
name, while it waits under `<Inbox>/_Working`. When every enabled hook of every stage has answered
`done` or `skipped`, the file moves to `<Inbox>/_Ready` and waits for the weekly move.

Stages, in this order:

| Stage | Meant for | Shipped hook |
|---|---|---|
| `playable` | will the device play it; rewrap or convert if not | `kinoshelf.default_hooks.playable` (reports only, unless `rewrap` / `convert` options are on) |
| `subtitles` | fetch subtitles in the language order from Settings | `kinoshelf.default_hooks.subtitles` (subliminal; off by default) |
| `nfo` | write a details file for the media server | `kinoshelf.default_hooks.nfo` |
| `icon` | poster or folder icon | `kinoshelf.default_hooks.poster` (saves `poster.jpg`) |
| `after_move` | anything after the file reached the library (runs once, answer ignored) | none |

Hooks are set in Settings > Hooks (YAML), for example:

```yaml
subtitles:
  - name: My subtitle service
    kind: http
    enabled: true
    url: http://subtitle-service:9000/job
    headers: {X-Token: replace-me}
icon:
  - name: Folder icon on the host
    kind: folder
    enabled: true
    folder: 'X:\Inbox\_Hook jobs\icon'
```

## Three kinds

**command**: a program inside the container. Put your own scripts in a folder mounted into the
container, for example `./hooks:/hooks:ro`, and set `command: [python, /hooks/my_hook.py]` (or any
executable). `python` means the app's own Python. Optional `timeout_minutes` (default 240).

**http**: the job is POSTed as JSON to `url` (optional `headers`, `timeout_seconds`, default 120);
the response body is the answer. A service that cannot be reached counts as `pending` and is asked
again in 30 minutes.

**folder**: for programs outside the container, such as a script on the Windows host. The app writes
`<item id>-<stage>.job.json` into `folder` (a path as you see it, translated through the folder map)
and waits. Your program reads the job, does the work, and writes `<item id>-<stage>.result.json` with
the answer. The app then moves both files into `done/` beside them. Until the answer exists, the item
shows as "Still working".

## The job (what a hook receives)

```json
{
  "protocol": 1,
  "stage": "subtitles",
  "item": {
    "id": 12,
    "kind": "movie",
    "is_dvd": false,
    "file": "/media/one/Inbox/_Working/X/Movies/A Separation (2011)/A Separation (2011).mkv",
    "file_shown": "X:\\Inbox\\_Working\\X\\Movies\\A Separation (2011)\\A Separation (2011).mkv",
    "sidecars": ["/media/one/Inbox/_Working/X/Movies/A Separation (2011)/A Separation (2011).en.srt"],
    "dest_file": "/media/one/Movies/A Separation (2011)/A Separation (2011).mkv",
    "dest_shown": "X:\\Movies\\A Separation (2011)\\A Separation (2011).mkv",
    "title": "A Separation", "original_title": "Jodaeiye Nader az Simin", "year": 2011,
    "season": null, "episode": null, "episodes": [], "episode_title": null,
    "ids": {"imdb": "tt1832382", "tmdb": "60243"},
    "poster_url": "https://image.tmdb.org/t/p/w185/....jpg",
    "people": "Asghar Farhadi",
    "probe": {"duration_s": 7392.1, "video_codec": "h264", "width": 720, "height": 400, "audio": [], "subtitles": []},
    "playable": {"status": "ok", "text": "Plays on TV as it is"}
  },
  "settings": {"subtitle_languages": ["fa", "en"], "subtitle_mark_default": true, "device": {}, "proxy": ""},
  "options": {}
}
```

`file` and `dest_file` are container paths; `file_shown` and `dest_shown` are the same places as you
and your media server see them. `options` is the hook's own `options` from Settings. A command hook
also gets `KS_DATA_DIR` and `KS_HOOK_STAGE` in its environment.

## The answer

One JSON object (for a command hook: the last line of standard output that is a JSON object):

```json
{"status": "done", "message": "Persian and English found", "files": ["/media/.../A Separation (2011).fa.srt"]}
```

| Field | Meaning |
|---|---|
| `status` | `done`, `skipped`, `pending` (ask again later) or `failed` (tried again in 30 minutes; after three failures the item goes to the review page) |
| `message` | shown on the pages |
| `files` | files the hook created; they move together with the item |
| `file` | the new main file, when the hook replaced it (a rewrap) |
| `replaced` | files the hook made obsolete; the app moves them into the set-aside folder |
| `retry_after_minutes` | with `pending`: when to ask again (default 10) |
| `playable` | with the `playable` stage: `{"status": "ok", "text": "..."}` for the pages |

Hooks never delete anything. Return obsolete files in `replaced`, and the app sets them aside.
