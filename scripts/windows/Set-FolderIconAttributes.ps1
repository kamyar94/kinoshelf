<#
.SYNOPSIS
  Makes the folder icons Kinoshelf wrote show up in Windows Explorer.

.DESCRIPTION
  Kinoshelf runs in a Linux container and writes FolderIcon_<id>.ico and desktop.ini into new title folders, but it
  cannot set Windows file attributes on a mounted drive. Explorer only reads a folder's desktop.ini when the file is
  Hidden + System and the folder itself is ReadOnly (or System). This script sets exactly those attributes, and
  nothing else, on folders that hold both a desktop.ini and a FolderIcon_*.ico:

    desktop.ini        + Hidden + System
    FolderIcon_*.ico   + Hidden + System
    the folder         + ReadOnly (left alone when it is already ReadOnly or System)

  It never creates, renames, moves or deletes a file. With -Days it looks only at folders written in the last days
  (a weekly run after Kinoshelf's weekly move needs -Days 8). Every change is written to the log (CSV).

.EXAMPLE
  .\Set-FolderIconAttributes.ps1 -Roots 'D:\Movies','E:\Series' -Days 8 -WhatIf
  powershell -NoProfile -File .\Set-FolderIconAttributes.ps1 -Roots "D:\Movies,E:\Series" -Days 8 -WhatIf
  (with -File, PowerShell passes a list as one string; give the roots as one comma-separated string)
  .\Set-FolderIconAttributes.ps1 -Roots 'D:\Movies','E:\Series' -Days 8 -Log C:\Kinoshelf\logs\icon-attributes.csv
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Roots,
    [int]$Days = 8,            # 0 = every folder
    [int]$Depth = 3,           # category \ collection \ title is three levels below a Movies root
    [string]$Log = '',
    [switch]$RefreshIconCache, # ie4uinit -show; works only in the signed-in user's own session
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
# powershell -File passes 'A','B' or "A,B" as the single string "A,B". Split such a string on commas, unless it
# names an existing folder as it is (a folder whose name holds a comma).
$Roots = @(foreach ($r in $Roots) {
        if ((Test-Path -LiteralPath $r -PathType Container) -or $r -notmatch ',') { $r }
        else { foreach ($part in $r -split ',') { $p = $part.Trim().Trim('"', "'"); if ($p) { $p } } }
    })
$since = if ($Days -gt 0) { (Get-Date).AddDays(-$Days) } else { [datetime]::MinValue }
$changes = New-Object System.Collections.Generic.List[string]
$seen = 0

function Add-Attr([IO.FileSystemInfo]$item, [IO.FileAttributes]$want, [string]$what) {
    $have = $item.Attributes
    if (($have -band $want) -eq $want) { return }
    $new = $have -bor $want
    $script:changes.Add(('"{0}","{1}","{2}","{3}"' -f (Get-Date -Format s), $what, $item.FullName.Replace('\\?\', ''), $new))
    if (-not $WhatIf) { $item.Attributes = $new }
}

function Visit([IO.DirectoryInfo]$dir, [int]$level) {
    if ($level -gt $Depth) { return }
    foreach ($sub in $dir.EnumerateDirectories()) {
        if ($sub.Name.StartsWith('_') -or $sub.Name.StartsWith('$') -or $sub.Name -eq 'System Volume Information') { continue }
        if ($sub.LastWriteTime -ge $since) {
            $script:seen++
            $ini = New-Object IO.FileInfo([IO.Path]::Combine($sub.FullName, 'desktop.ini'))
            $icons = @($sub.EnumerateFiles('FolderIcon_*.ico'))
            if ($ini.Exists -and $icons.Count -gt 0) {
                Add-Attr $ini ([IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System) 'desktop.ini'
                foreach ($ico in $icons) { Add-Attr $ico ([IO.FileAttributes]::Hidden -bor [IO.FileAttributes]::System) 'icon' }
                if (($sub.Attributes -band ([IO.FileAttributes]::ReadOnly -bor [IO.FileAttributes]::System)) -eq 0) {
                    Add-Attr $sub ([IO.FileAttributes]::ReadOnly) 'folder'
                }
            }
        }
        Visit $sub ($level + 1)
    }
}

foreach ($r in $Roots) {
    $root = New-Object IO.DirectoryInfo('\\?\' + $r.TrimEnd('\'))
    if (-not $root.Exists) { Write-Warning "Not found: $r"; continue }
    Visit $root 1
}
if ($Log -and $changes.Count -gt 0) {
    $dir = Split-Path -Parent $Log
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    if (-not (Test-Path -LiteralPath $Log)) { '"time","what","path","attributes"' | Out-File -LiteralPath $Log -Encoding utf8 }
    $changes | Out-File -LiteralPath $Log -Encoding utf8 -Append
}
if ($RefreshIconCache -and $changes.Count -gt 0 -and -not $WhatIf) {
    Start-Process -FilePath "$env:SystemRoot\System32\ie4uinit.exe" -ArgumentList '-show' -Wait
}
"{0} folders looked at, {1} attribute changes{2}" -f $seen, $changes.Count, $(if ($WhatIf) { ' (WhatIf: nothing changed)' } else { '' })
