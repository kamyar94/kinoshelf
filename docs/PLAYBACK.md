# Make it play on your device

Kinoshelf checks every new file against the device you watch on, and can change a file the device would not play.
It never changes a file in a way it cannot prove, and it never deletes one. The step runs as the `playable` hook
(`kinoshelf.default_hooks.playable`, on by default) while the item waits in `_Working`; its settings are in
**Settings > Playback** and under `device` and `playback` in `data/config.yml`.

## The device

Pick a profile, or **Custom** and type the lists from your device's manual (names as ffprobe uses them):

| Profile | Containers | Picture | Sound | Largest | 10-bit |
|---|---|---|---|---|---|
| Plays almost anywhere | mp4, m4v | h264 | aac, mp3 | 1080 | no |
| Smart TV (recent) | mp4, mkv, m4v, mov, ts | h264, hevc, vp9 | aac, ac3, eac3, mp3 | 2160 | yes |
| Smart TV (older) | mp4, mkv, avi | h264, mpeg4 | aac, ac3, mp3 | 1080 | no |
| Web browser | mp4, webm | h264, vp9, av1 | aac, mp3, opus | 2160 | no |
| Apple TV, iPhone, iPad | mp4, m4v, mov | h264, hevc | aac, ac3, eac3, alac | 2160 | yes |
| Android TV and streaming sticks | mp4, mkv, webm | h264, hevc, vp9, av1 | aac, ac3, eac3, mp3, opus | 2160 | yes |

The profiles are starting points from what such devices commonly play, not promises for a particular model.
**Rewrap into** names the container for a file whose own container the device does not open (mp4 by default).

## What happens to a file

The device would play it as it is: nothing changes; the week page says "Plays on TV as it is".

**The picture plays, the container or a sound track does not: a rewrap.** The picture is copied into the device's
container (or stays in its own, when only the sound is the problem). Each sound track the device plays is copied,
and each other one becomes AAC (192 kb/s for stereo, 448 for 5.1, 640 for 7.1). Text subtitles an MP4 cannot hold
are saved beside the file (`<name>.<language>.srt`, `.ass`, `.vtt`; Blu-ray picture subtitles as `.sup`). A file
whose picture subtitles cannot be saved that way (DVD subtitles in an MKV, for example) keeps its original. The new
file replaces the original only when every check passes:

| Check | Passes when |
|---|---|
| Pictures decode the same | every picture of the new file hashes (MD5 of the decoded picture) the same as the original's, and the count is the same |
| Picture times match the original | every picture is shown at its original time (within 1.5 ms), none twice or out of order |
| Sound decodes the same | each copied track decodes to the same samples and stays in step (within the offset below); when a decoder trims the start differently, its packets must be identical instead |
| New AAC sound in step | a track that became AAC is, at 15, 50 and 85 % of the file, within **Sound may shift by at most** (20 ms) of the original, measured by cross-correlation of the decoded sound, cut at the exact time on each file's own timestamps |
| Lengths match | the file and each picture and sound stream within **Lengths may differ by** (0.1 s) |
| Same streams, languages and picture size | the same number of pictures and sound tracks, the same languages in the same order, the same picture size |

**The picture itself does not play (codec, size or 10-bit): a re-encode**, only when **Re-encode** is on (off by
default). The picture becomes H.264 High, 8-bit 4:2:0, with x264 at the **x264 speed** you choose (slow by default),
scaled down to the device's largest height when it is taller. Sound and subtitles are handled as in a rewrap.

1. The CRF comes from three 20-second samples at 15, 50 and 85 % of the file: the lowest CRF from **Start at CRF**
   down to **Lowest CRF** whose predicted size stays under 97 % of the original, or, when even the start does not
   fit, the first one up to **Highest CRF** that does. When none fits, the original stays: "no copy would be
   smaller".
2. The whole file is encoded. A result that is not smaller than the original is thrown away and the next CRF
   tried (at most three encodes).
3. VMAF (libvmaf, model v0.6.1) scores the new picture against the decoded original, paired picture by picture
   (long files are scored every n-th picture, at most 40,000). The copy passes when the **mean**, the **1% low**
   (the mean of the worst 1 % of the scored pictures) and the **lowest stretch** (the lowest mean of any stretch
   of **Stretch length** seconds; 0 = the single worst picture) reach their thresholds (93, 83 and 88 by default).
   A copy that misses the gate gets one more try with more bits when it is clearly smaller than the original.
4. The picture count, the lengths and each sound track's sync are checked as for a rewrap.

A copy that does not pass is thrown away, and the week page says why ("Kept the original: no smaller copy was good
enough" with the scores). Re-encodes take long: the hook may run for up to 24 hours per file (`timeout_minutes`).
When a rewrap fails its checks and re-encodes are on, the file is re-encoded instead.

Sizes are whole byte counts everywhere (files of 2 GB, 4 GB and more are ordinary); a test checks this.

## The original, the journal and undo

The new file takes the item's own name with its new extension, and the original goes to the set-aside folder on
the same drive: `<drive>\<set-aside folder>\Playback originals\<the same path>`. Nothing is deleted; empty the
folder yourself when you are happy with the results. Each step is one line in `data/playback/journal.jsonl`
(time, item, what, why, every check, sizes, where the original went).

The week page shows, under each item, what was done and why, the checks and where the original is. **Put the
original back** moves the original back in place of the new file (also after the weekly move; Jellyfin is told
the folder), and the new file goes to `Playback originals` instead. The journal gets an `undone` line.

## Hook options

The hook reads Settings > Playback. Options on the hook (Settings > Hooks) override it for that hook: any key of
the `playback` section, such as `{rewrap: true, reencode: false}`. Settings written by versions before 0.4.0 move
over by themselves: the device's lists become the Custom profile, and the hook's old `rewrap` / `convert` options
become `playback.rewrap` / `playback.reencode`, so nothing changes until you change it.
