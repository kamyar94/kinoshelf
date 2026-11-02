"""Subtitles through subliminal (optional extra), in the language order from Settings. The first
language found can be marked as the default track (<name>.<lang>.default.srt)."""

from __future__ import annotations

from pathlib import Path

from . import run


def handle(job: dict) -> dict:
    try:
        from babelfish import Language
        from subliminal import download_best_subtitles, region, save_subtitles, scan_video
    except ImportError:
        return {"status": "skipped", "message": "subliminal is not installed"}
    it = job["item"]
    s = job.get("settings") or {}
    langs = [x for x in s.get("subtitle_languages") or [] if x]
    if not langs:
        return {"status": "skipped", "message": "No subtitle languages set"}
    f = Path(it["file"])
    have = {p.name[len(f.stem) + 1:].split(".")[0] for p in f.parent.glob(f.stem + ".*")
            if p.suffix.lower() in (".srt", ".ass", ".ssa", ".sub", ".vtt")}
    wanted = [x for x in langs if x not in have]
    files: list[str] = []
    if wanted:
        if not region.is_configured:
            region.configure("dogpile.cache.memory")
        video = scan_video(str(f))
        found = download_best_subtitles({video}, {Language.fromietf(x) for x in wanted})
        for sub in save_subtitles(video, found.get(video, []), single=False, directory=str(f.parent)):
            lang = str(sub.language)
            files.append(str(f.with_name(f"{f.stem}.{lang}.srt")))
        have |= {Path(x).name[len(f.stem) + 1:].split(".")[0] for x in files}
    if s.get("subtitle_mark_default", True):
        first = next((x for x in langs if x in have), None)
        if first:
            plain = f.with_name(f"{f.stem}.{first}.srt")
            marked = f.with_name(f"{f.stem}.{first}.default.srt")
            if plain.exists() and not marked.exists() and not any(".default." in p.name for p in f.parent.glob(f.stem + ".*")):
                plain.rename(marked)
                files = [str(marked) if x == str(plain) else x for x in files]
                if str(marked) not in files:
                    files.append(str(marked))
    missing = [x for x in langs if x not in have]
    msg = "Subtitles: " + ", ".join(x for x in langs if x in have) if len(missing) < len(langs) else "None found"
    if missing:
        msg += f" (none for {', '.join(missing)})"
    return {"status": "done", "message": msg, "files": [x for x in files if Path(x).exists()]}


if __name__ == "__main__":
    run(handle)
