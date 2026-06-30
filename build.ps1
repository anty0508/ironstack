# Build IronStack into a single windowed .exe (dist\IronStack.exe).
#
#   pip install -r requirements.txt   # includes PyInstaller
#   .\build.ps1
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

# --collect-data soundcard: soundcard reads a 'mediafoundation.py.h' data file
# from its package at import time; without this the frozen app crashes.
#   --icon       embeds the .ico as the .exe's Windows resource (taskbar etc.)
#   --add-data   ships files inside the bundle (extracted to sys._MEIPASS at
#                runtime): the .ico for the tray/window icon, and the .env so
#                the keys are self-contained.
pyinstaller --noconfirm --clean `
    --onefile `
    --windowed `
    --name IronStack `
    --icon assets\IronStack.ico `
    --add-data "assets\IronStack.ico;assets" `
    --add-data ".env;." `
    --collect-data soundcard `
    main.py

Write-Host ""
Write-Host "Done -> dist\IronStack.exe" -ForegroundColor Green
