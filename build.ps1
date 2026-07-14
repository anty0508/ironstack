# Build IronStack into a single .exe (dist\IronStack.exe).
#
#   pip install -r requirements.txt   # includes PyInstaller
#   .\build.ps1            # windowed build -> dist\IronStack.exe
#
# The build requests UAC elevation at launch (one exe for Host and Viewer); the
# Host needs it to control admin windows, the Viewer just accepts the prompt.
#
# Run from the project root inside the virtualenv that has the deps installed.
#
# The build bakes the project's .env (your API keys) INTO the exe so it runs
# standalone on any PC. WARNING: anyone with the exe can extract those keys, so
# don't distribute this build publicly. A .env placed next to the .exe still
# overrides the baked-in one.

$ErrorActionPreference = 'Stop'

if (-not (Test-Path .env)) {
    Write-Error "No .env found. Create one with OPENAI_API_KEY / DEEPGRAM_API_KEY before building (it gets baked into the exe)."
}

# --uac-admin embeds a manifest that requests elevation (UAC) at launch. ONE build
# for both roles: the Viewer just accepts the prompt (harmless), and the Host runs
# elevated so it can remote-control admin-integrity windows (e.g. AnyDesk's Accept
# dialog) — a non-elevated app can't inject input into them (Windows UIPI). The UAC
# secure desktop itself + Ctrl+Alt+Del still need a SYSTEM service (a later phase).
$uac = @('--uac-admin')

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
    --windowed `
    --name IronStack `
    --icon assets\IronStack.ico `
    --add-data "assets\IronStack.ico;assets" `
    --add-data ".env;." `
    --collect-data soundcard `
    --collect-all av `
    @uac `
    main.py

# A native process failure does NOT trip $ErrorActionPreference='Stop', so check
# the exit code explicitly instead of falsely reporting success.
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed (exit $LASTEXITCODE). No exe was produced."
}

Write-Host ""
Write-Host "Done -> dist\IronStack.exe" -ForegroundColor Green
