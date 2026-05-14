# Windows installer distribution

Folder1004 publishes a normal Windows installer named
`Folder1004-Setup.exe`.

## For users

1. Open the Folder1004 GitHub **Releases** page.
2. Download `Folder1004-Setup.exe`.
3. Run the installer.
4. Launch **Folder1004** from the Start menu, or choose the desktop
   shortcut during setup.

The installer uses per-user installation by default, so it does not
require administrator rights.  Because the executable is not code-signed
yet, Windows SmartScreen may show a warning on first launch.

If the app closes unexpectedly, open **로그 폴더 열기** after relaunching
and send the newest `gui_*.log` / `organize_*.log`.  The Windows build
records platform, memory, Qt, Python-thread, and fatal-crash diagnostics
there.

## For maintainers

Build locally on Windows:

```powershell
pip install -e ".[dev,windows]"
.\scripts\build_windows.ps1 -Installer -RequireInstaller
```

The installer is written to:

```text
dist\Folder1004-Setup.exe
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
jobs, verifies that `dist\Folder1004-Setup.exe` exists, uploads it as
the `Folder1004-Windows-Installer` artifact, and attaches it to tagged
GitHub Releases.
