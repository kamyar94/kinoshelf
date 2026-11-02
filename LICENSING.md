# Licensing

Kinoshelf was created by Kamyar Fatemifar (https://github.com/kamyar94). Everything in this
repository that he wrote is under the **KF Source-Available License 1.0 (KF-SA-1.0)** in
[LICENSE.md](LICENSE.md).

KF-SA-1.0 is source-available, not open source. You may read the code, keep a copy, fork it on
GitHub and run it privately for your own non-commercial use on a system no one else can reach.
Hosting it for others, changing it, sharing a changed version or using it commercially needs written
permission; ask by email at kfatemifar.gt@gmail.com or through an issue on this repository.

## Third-party material

These keep their own licences. None of them is copied into this repository; they are installed or
downloaded when the image is built, or fetched by your own instance at run time.

| Material | Licence | How it is used |
|---|---|---|
| Python libraries in `requirements.lock` (FastAPI, Starlette, Uvicorn, Jinja2, httpx, PyYAML, RapidFuzz, anyascii, argon2-cffi, PyJWT, subliminal and their dependencies) | each its own (MIT, BSD, Apache-2.0, ISC and others) | installed at build time |
| guessit, rebulk | LGPL-3.0 | installed at build time as unmodified libraries |
| FFmpeg static build (`mwader/static-ffmpeg` image) | GPL (FFmpeg built with GPL libraries) | separate program the app runs |
| IBM Plex Sans and IBM Plex Mono | SIL Open Font License 1.1 | downloaded at build time by `scripts/fetch-fonts.sh`, served by the app |
| socksio (through `httpx[socks]`) | MIT | installed at build time, for SOCKS5 proxies |
| Xray-core (`ghcr.io/xtls/xray-core` image) | MPL-2.0 | optional separate container (compose profile `route`), pulled by you, never built into the app image |

## Data sources

Your instance fetches data under each provider's own terms, with your own keys:

- **TMDB**: this product uses the TMDB API but is not endorsed or certified by TMDB.
- **IMDb datasets** (datasets.imdbws.com): personal and non-commercial use only. Your instance
  downloads them; they are never part of this repository.
- **TVmaze**: data under CC BY-SA 4.0.
- **OMDb** and **OpenSubtitles**: under their API terms; both need your own key.
