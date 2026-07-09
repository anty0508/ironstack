# Build IronStack into a single .exe (dist\IronStack.exe).
#
#   pip install -r requirements.txt   # includes PyInstaller
#   .\build.ps1            # normal windowed build  -> dist\IronStack.exe
#   .\build.ps1 -Debug     # console build (shows logs) -> dist\IronStack-debug.exe
#
# Run from the project root inside the virtualenv that has the deps installed.
#
# The build bakes the project's .env (your API keys) INTO the exe so it runs
# standalone on any PC. WARNING: anyone with the exe can extract those keys, so
# don't distribute this build publicly. A .env placed next to the .exe still
# overrides the baked-in one.

param(
    # -Debug: build a CONSOLE exe (a terminal window opens alongside the app and
    # shows the log output). Use this to debug networking on a machine where you
    # can only run the exe, not python. The normal build is windowed (no console).
    [switch]$Debug
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path .env)) {
    Write-Error "No .env found. Create one with OPENAI_API_KEY / DEEPGRAM_API_KEY before building (it gets baked into the exe)."
}

if ($Debug) {
    $windowFlag = '--console'
    $name = 'IronStack-debug'
    Write-Host "Building DEBUG (console) build -> dist\IronStack-debug.exe" -ForegroundColor Yellow
} else {
    $windowFlag = '--windowed'
    $name = 'IronStack'
}

# --collect-data soundcard: soundcard reads a 'mediafoundation.py.h' data file
# from its package at import time; without this the frozen app crashes.
#   --icon       embeds the .ico as the .exe's Windows resource (taskbar etc.)
#   --add-data   ships files inside the bundle (extracted to sys._MEIPASS at
#                runtime): the .ico for the tray/window icon, and the .env so
#                the keys are self-contained.
#
# NOTE: we call PyInstaller via "python -m PyInstaller", NOT the pyinstaller.exe
# wrapper. That wrapper bakes in the absolute path of the python.exe present when
# it was pip-installed; if this venv was copied/renamed from another location the
# wrapper points at a python.exe that no longer exists and fails with
# "Unable to create process ... The system cannot find the file specified".
# Going through the module uses whichever python is on PATH (the active venv).
python -m PyInstaller --noconfirm --clean `
    --onefile `
    $windowFlag `
    --name $name `
    --icon assets\IronStack.ico `
    --add-data "assets\IronStack.ico;assets" `
    --add-data ".env;." `
    --collect-data soundcard `
    main.py

# A native process failure does NOT trip $ErrorActionPreference='Stop', so check
# the exit code explicitly instead of falsely reporting success.
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed (exit $LASTEXITCODE). No exe was produced."
}

Write-Host ""
Write-Host "Done -> dist\$name.exe" -ForegroundColor Green
