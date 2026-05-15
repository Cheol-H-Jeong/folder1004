# Windows distribution

Folder1004 publishes both:

- `Folder1004-Setup.exe` — normal per-user installer.
- `Folder1004-Windows-Portable.zip` — no-install portable mode.

## For users

### Normal installer

1. Open the Folder1004 GitHub **Releases** page.
2. Download `Folder1004-Setup.exe`.
3. Run the installer.
4. Launch **Folder1004** from the Start menu, or choose the desktop
   shortcut during setup.

The installer uses per-user installation by default, so it does not
require administrator rights.  Because the executable is not code-signed
yet, Windows SmartScreen may show a warning on first launch.

### No-install portable mode

1. Download `Folder1004-Windows-Portable.zip`.
2. Extract it anywhere, for example Desktop, Downloads, or a USB drive.
3. Open the extracted `folder1004` folder.
4. Run `folder1004.exe`.

The portable ZIP contains a `folder1004.portable` marker.  When that
marker is present, Folder1004 stores settings, logs, and its local index
inside `folder1004\data` next to the executable instead of using
`%LOCALAPPDATA%\Folder1004`.  To reset the portable copy, close the app
and delete that `data` folder.

If the app closes unexpectedly, open **로그 폴더 열기** after relaunching
and send the newest `gui_*.log` / `organize_*.log`.  The Windows build
records platform, memory, Qt, Python-thread, and fatal-crash diagnostics
there.

## For maintainers

Build locally on Windows:

```powershell
pip install -e ".[dev,windows]"
.\scripts\build_windows.ps1 -PortableZip -Installer -RequireInstaller
```

The Windows distributables are written to:

```text
dist\Folder1004-Setup.exe
dist\Folder1004-Windows-Portable.zip
```

Requirements:

- Windows 10 or later.
- Inno Setup 6 installed (`iscc` on `PATH`, or in the default
  `Program Files` location).

Useful Windows stability override knobs for reproducing field reports:

- `FOLDER1004_GATHER_WORKERS` — outer file metadata/excerpt worker cap
  (default: `2` on Windows).
- `FOLDER1004_PARSE_WORKERS` — heavy document parser worker cap
  (default: `1` on Windows).
- `FOLDER1004_MAX_PARSE_MB` — skip body extraction above this size and
  classify from filename/metadata instead (default: `64`).

GitHub Actions installs Inno Setup automatically for Windows package
jobs, verifies both `dist\Folder1004-Setup.exe` and
`dist\Folder1004-Windows-Portable.zip`, uploads them as
`Folder1004-Windows-Installer` / `Folder1004-Windows-Portable`
artifacts, and attaches both to tagged GitHub Releases.
