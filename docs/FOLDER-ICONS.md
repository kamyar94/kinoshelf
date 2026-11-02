# Folder icons

With the filing rules on (Settings > Filing rules), Kinoshelf can give every new film, show and collection folder a
Windows folder icon: a wide picture of the title filling the folder's window edge to edge, a tab in a dark tint of
the picture's colours, and the title on the tab. The hook is
`kinoshelf.default_hooks.folder_icon` (stage `icon`).

## The rules it follows

- Wide (16:9) art only, never a portrait poster. In this order: TMDB key art with an English title, a clean backdrop
  without text, key art in the original language, any other backdrop in any language, a TV episode still, and last
  a frame of the video itself with its black bars cut off. Within a group: pictures at least 1920 px wide first,
  then votes, then width.
- The picture fills the window edge to edge, masked by the folder's shape. No blurred copy behind it.
- Content detection chooses the crop: a text detector (PP-OCRv3 detection, OpenCV model zoo) finds titles and logos,
  the YuNet face detector finds faces, a textless version of the same backdrop (when TMDB has one) marks the title
  area, and a saliency map breaks ties. The crop keeps titles and the main faces inside the window's safe area.
  Only when no crop keeps the text whole is the picture made slightly smaller and the thin uncovered sliver filled
  from its own mirrored edge.
- Collection folders get the stacked look: the collection's TMDB backdrop, two lighter folder outlines behind, and
  the tab `NAME · n FILMS`.

The look comes from the author's Photoshop template. Its shape layers were exported once into
`src/kinoshelf/icons/assets/` (folder back, tab shape, window shape, the window's tint); Pillow puts the icon
together from them at 512 px, reduces it to 256 px and writes an `.ico` with PNG frames of 256, 128, 64, 48, 32 and
16 px. The window's tint changes only the green and blue channels by about 10 % (the template layer's own blending
setting, measured by rendering it over flat colours), so whites stay white as in the Photoshop icons.

The detectors run with ONNX Runtime (no OpenCV). The font (Oswald) and the two models are downloaded at build time
by `scripts/fetch-icon-assets.sh`; without them the picture is centred and the tab uses a plain font.

## Files

`FolderIcon_<IMDb id>.ico` (films and shows), `FolderIcon_tmdbcoll<TMDB collection id>.ico` (collections),
`FolderIcon_tt-unknown.ico` / `FolderIcon_noid.ico` without an id, and `desktop.ini`:

```
[.ShellClassInfo]
IconResource=FolderIcon_tt0120338.ico,0
IconFile=FolderIcon_tt0120338.ico
IconIndex=0
```

ASCII, CRLF line ends. A folder already in the library is never given a new icon or a new `desktop.ini`.

## The Windows-side step

Explorer shows a folder's icon only when its `desktop.ini` is Hidden + System and the folder itself is ReadOnly (or
System). A Linux container writing to a Windows drive through a WSL mount (drvfs) cannot set those attributes, so
a small Windows script does it after the weekly move:

`scripts/windows/Set-FolderIconAttributes.ps1 -Roots <library folders> [-Days 8] [-Log <csv>] [-WhatIf]`

From a PowerShell prompt the roots can be a list (`-Roots 'D:\Movies','E:\Series'`). Through
`powershell.exe -File` (a scheduled task) PowerShell passes a list as one string, so give the roots as one
comma-separated string there (`-Roots "D:\Movies,E:\Series"`); the script splits it. A root whose own name holds a
comma is kept as it is.

- It looks at folders written in the last `-Days` days, up to three levels below each root (category, collection,
  title), and only at folders that hold both a `desktop.ini` and a `FolderIcon_*.ico`.
- It adds Hidden + System to `desktop.ini` and the icon, and ReadOnly to the folder (left alone when already ReadOnly
  or System). Nothing else: it never creates, renames, moves or deletes a file.
- Every change goes to the CSV log; `-WhatIf` lists them without changing anything; a second run changes nothing.
- `-RefreshIconCache` runs `ie4uinit.exe -show` afterwards; that works only in the signed-in user's own session.

Suggested schedule (not installed by Kinoshelf): a Windows scheduled task one hour after the weekly move, running
as the user who owns the drives:

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File ' +
     '"C:\Kinoshelf\Set-FolderIconAttributes.ps1" -Roots "D:\Movies,E:\Series" ' +
     '-Days 8 -Log "C:\Kinoshelf\logs\icon-attributes.csv"')
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 04:00
Register-ScheduledTask -TaskName 'Kinoshelf-FolderIconAttributes' -Action $a -Trigger $t -Description 'Folder icon attributes after the Kinoshelf weekly move'
```

Run it by hand with `-WhatIf` first. Until it has run, a new folder shows the plain folder icon; nothing breaks.

## Turning it on

The hook is off by default. Turn it on in Settings > Hooks (stage `icon`, `kinoshelf.default_hooks.folder_icon`)
once the first icons look right to you: it only ever writes into new folders, and never replaces an icon.
