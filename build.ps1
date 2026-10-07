# =============================================================================
# nuitka-build.ps1 - KnowledgeVista build pipeline
# Generated from nuitka-build.ps1.template
# =============================================================================
#
# Produces standalone .exe files in dist\ with no Python install required.
#
# BEFORE FIRST USE: replace these placeholders below
#   KnowledgeVista   - your project (cosmetic, used in build banner)
#   [ENTRY_SCRIPT]   - path to your main .py relative to this script
#   [OUTPUT_NAME]    - desired .exe filename
#
# Prerequisites (run once):
#   pip install nuitka ordered-set zstandard
#   + any runtime deps your project uses (pillow, pystray, etc.)
#
# Optional:
#   icon.ico - place a 256x256 icon file next to this script.
#   If absent, the --windows-icon-from-ico flag is skipped automatically.
#
# See NUITKA_GOTCHAS.md (in the same templates folder) for known issues.
# =============================================================================

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT = $PSScriptRoot
$DIST = "$ROOT\dist"
$ICON = "$ROOT\icon.ico"

# ---------- Pre-flight checks ------------------------------------------------

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python is not on PATH. Activate your venv or install Python first."
}

# Check Nuitka is installed (cheap version probe)
& python -m nuitka --version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Nuitka is not installed. Run: pip install nuitka ordered-set zstandard"
}

# ---------- Orphan cleanup ---------------------------------------------------
# Nuitka leaves *.onefile-build, *.build, *.dist dirs if --remove-output was
# blocked (AV file lock, interrupted compile). Clear them before building so
# we start clean and never accumulate stale state.

function Clear-NuitkaOrphans($dir) {
    if (-not (Test-Path $dir)) { return }
    $patterns = @("*.onefile-build", "*.build", "*.dist")
    foreach ($pat in $patterns) {
        Get-ChildItem -Path $dir -Directory -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host "  [clean] removing $($_.Name)" -ForegroundColor DarkGray
            try {
                Remove-Item $_.FullName -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Host "  [warn]  could not remove $($_.Name) - $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
    }
}

# ---------- Build helper -----------------------------------------------------

function Build-Exe($script, $outName, $nuArgs) {
    $isGuiBuild = $nuArgs -contains "--enable-plugin=tk-inter"

    if ((Test-Path $ICON) -and ($isGuiBuild)) {
        $nuArgs += "--windows-icon-from-ico=$ICON"
    }

    Clear-NuitkaOrphans $DIST

    Write-Host "  Building $outName ..." -ForegroundColor Cyan

    # Capture output so we can parse the uncompressed payload size for the sanity check.
    # Temporarily suspend Stop mode: with $ErrorActionPreference = "Stop", PowerShell
    # treats each native command stderr line as a NativeCommandError and aborts.
    # Nuitka writes progress to stderr, so we must use Continue while capturing.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $buildOutput = & python @nuArgs $script 2>&1
    $nuitkaExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP

    $buildOutput | ForEach-Object { Write-Host $_ }
    if ($nuitkaExit -ne 0) {
        throw "Nuitka failed (exit $nuitkaExit) building $outName"
    }
    Write-Host "  OK: $DIST\$outName" -ForegroundColor Green

    Clear-NuitkaOrphans $DIST

    # ---- Sanity check: uncompressed payload size ---------------------------
    # Nuitka compresses onefile payloads ~27%, so a healthy tkinter+PIL+pystray
    # app lands at ~14 MB on disk even though the uncompressed payload is ~55 MB.
    # Checking the compressed exe size would always fire a false WARN, so we
    # parse the uncompressed size from Nuitka's own "Onefile payload..." log line.
    # Only enforce for GUI builds (--enable-plugin=tk-inter present).
    # CLI tools can legitimately be a few MB uncompressed - skip the check.
    if ($isGuiBuild) {
        $payloadLine = ($buildOutput | Select-String "Onefile payload compression ratio") | Select-Object -Last 1
        if ($payloadLine -match "size (\d+) to") {
            $uncompressedMB = [math]::Round([long]$Matches[1] / 1MB, 1)
            if ($uncompressedMB -lt 30) {
                Write-Host ""
                Write-Host "  [WARN] $outName uncompressed payload is only $uncompressedMB MB - suspicious for a GUI build." -ForegroundColor Yellow
                Write-Host "         Likely a missing --enable-plugin or --include-package flag." -ForegroundColor Yellow
                Write-Host "         Run the exe from cmd with --windows-console-mode=attach to debug." -ForegroundColor Yellow
                Write-Host ""
            } else {
                Write-Host "  Payload OK: $uncompressedMB MB uncompressed" -ForegroundColor DarkGray
            }
        } else {
            $sizeMB = [math]::Round((Get-Item "$DIST\$outName").Length / 1MB, 1)
            Write-Host "  Compressed size: $sizeMB MB (could not parse uncompressed payload)" -ForegroundColor DarkGray
        }
    }
}

# ---------- Main -------------------------------------------------------------

Write-Host ""
Write-Host "=== KnowledgeVista - Nuitka build ===" -ForegroundColor Cyan
Write-Host ""

New-Item -ItemType Directory -Force -Path $DIST | Out-Null

# ---- GUI build (tkinter app) ------------------------------------------------
# Edit [ENTRY_SCRIPT] and [OUTPUT_NAME]. Remove PIL/pystray if your app doesn't use them.
$guiArgs = @(
    "-m", "nuitka",
    "--onefile",
    "--windows-console-mode=disable",
    "--enable-plugin=tk-inter",
    "--include-package=PIL",
    "--include-package=pystray",
    # ---- Anaconda bloat exclusions (safe to remove if not using Anaconda) ----
    # If building from an Anaconda or conda env, Nuitka traces into numpy,
    # scipy, pandas etc. even if your app never imports them, bundling ~450 MB
    # of Intel MKL DLLs and scientific libraries. These flags block that.
    # Remove any package your app actually uses.
    "--nofollow-import-to=numpy",
    "--nofollow-import-to=scipy",
    "--nofollow-import-to=pandas",
    "--nofollow-import-to=matplotlib",
    "--nofollow-import-to=sklearn",
    "--nofollow-import-to=IPython",
    "--nofollow-import-to=notebook",
    # --------------------------------------------------------------------------
    "--remove-output",
    "--assume-yes-for-downloads",
    "--output-dir=$DIST",
    "--output-filename=[OUTPUT_NAME]"
)

# ---- CLI build (no GUI plugins) ---------------------------------------------
# Uncomment and use this instead of the GUI block above for CLI-only tools.
# $cliArgs = @(
#     "-m", "nuitka",
#     "--onefile",
#     "--windows-console-mode=force",
#     "--nofollow-import-to=numpy",
#     "--nofollow-import-to=scipy",
#     "--nofollow-import-to=pandas",
#     "--nofollow-import-to=matplotlib",
#     "--nofollow-import-to=sklearn",
#     "--nofollow-import-to=IPython",
#     "--nofollow-import-to=notebook",
#     "--remove-output",
#     "--assume-yes-for-downloads",
#     "--output-dir=$DIST",
#     "--output-filename=[OUTPUT_NAME]"
# )

Build-Exe "$ROOT\[ENTRY_SCRIPT]" "[OUTPUT_NAME]" $guiArgs

# Add more Build-Exe calls here if the project ships multiple binaries.
# Example:
# Build-Exe "$ROOT\src\my-helper.py" "my-helper.exe" $cliArgs

# ---------- Stage data files (edit per project) ------------------------------
# If your app ships with config files, templates, docs, etc., copy them
# into $DIST here. Example:
#
# Copy-Item "$ROOT\README.md" "$DIST\README.md" -Force
#
# For JSON config files, use [System.IO.File]::WriteAllText so PowerShell
# doesn't add a UTF-8 BOM (which crashes Python's json.load):
#
# $config = @{ key = "value" }
# [System.IO.File]::WriteAllText("$DIST\config.json", ($config | ConvertTo-Json -Depth 5))

Write-Host ""
Write-Host "Build complete -> $DIST" -ForegroundColor Green
Write-Host ""

exit 0
