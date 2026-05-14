# Build a Windows one-folder bundle (and optionally an Inno Setup installer).
#
# Usage (PowerShell, repo root, with an activated venv that has dev + windows extras):
#   pip install -e ".[dev,windows]"
#   .\scripts\build_windows.ps1                # bundle only
#   .\scripts\build_windows.ps1 -Installer     # bundle + .exe installer (requires Inno Setup 6 'iscc')
#   .\scripts\build_windows.ps1 -Installer -RequireInstaller
#
# Code signing / SmartScreen reputation is OUT OF SCOPE.  The unsigned
# bundle still runs locally; SmartScreen may warn on first run.
param(
    [switch]$Installer,
    [switch]$RequireInstaller
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
    Write-Error 'pyinstaller not found — run: pip install -e ".[dev,windows]"'
}

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

pyinstaller --noconfirm "scripts\folder1004.spec"

$bundle = "$root\dist\folder1004"
if (-not (Test-Path $bundle)) {
    Write-Error "Build did not produce $bundle"
}
Write-Host ""
Write-Host "Built bundle: $bundle"
Write-Host "Run:          $bundle\folder1004.exe"

if ($Installer) {
    $iscc = Get-Command iscc -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $candidatePaths = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )
        foreach ($candidate in $candidatePaths) {
            if ($candidate -and (Test-Path $candidate)) {
                $iscc = Get-Item $candidate
                break
            }
        }
    }
    if (-not $iscc) {
        $message = "Inno Setup 'iscc' not found — install Inno Setup 6 and re-run."
        if ($RequireInstaller) {
            Write-Error $message
        }
        Write-Warning "$message Skipping installer step."
        exit 0
    }
    $isccPath = $iscc.Source
    if (-not $isccPath) {
        $isccPath = $iscc.FullName
    }
    & $isccPath "$root\scripts\folder1004.iss"
    $installer = "$root\dist\Folder1004-Setup.exe"
    if (-not (Test-Path $installer)) {
        Write-Error "Installer build did not produce $installer"
    }
    Write-Host ""
    Write-Host "Installer: $installer"
}
