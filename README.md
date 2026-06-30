# IronStack

Listens to your computer's **system audio** (the interviewer's voice), turns it
into text in real time with Deepgram, and generates a short, natural answer
using the OpenAI API.

## Flow

```
launcher: pick Resume + JD (+ Projects/References)  ->  build prompt
                                                          |
system audio (loopback) -> Deepgram realtime transcription -> question text
                                                          |
                                       OpenAI Responses API -> answer (overlay)
                                                          |
                                          transcript + Q&A saved to SQLite
```

## Layout

```
main.py        Entry point. COM setup, then the setup <-> interview loop.
config.py      API keys, models, sample rates, storage paths (anchors the project root).

ui/            PySide6 windows
  overlay.py     Floating always-on-top answer overlay.
  launcher.py    Pre-interview dialog: document library, preferences, history.
  tray.py        System-tray icon (show/hide/quit) and the app icon.

services/      Live answering pipeline
  transcriber.py Captures system audio and streams it to Deepgram STT.
  audio_utils.py Finds the loopback device and resamples audio.
  answerer.py    Sends a question to the OpenAI Responses API.
  context.py     Builds the system prompt from the selected documents.

storage/       Data
  database.py    SQLite: documents, meetings, transcripts, settings.
  documents.py   Extracts text from imported .pdf / .docx / .txt files.

assets/        App icon source (IronStack.ico).
data/          SQLite database (interview.db), created at runtime.
```

## Setup

1. Create / activate a virtual environment, then install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. Put your API keys in a `.env` file at the project root:

   ```
   OPENAI_API_KEY=sk-...
   DEEPGRAM_API_KEY=...
   ```

## Run

```powershell
python main.py
```

A **setup window** opens first. Import your Resume, Job Description, Projects and
References (`.pdf`, `.docx`, or `.txt`), edit your global preferences, pick what to
use, and click **Start interview**.

The floating overlay then listens to system audio: each interviewer line appears,
and answers stream in. Past interviews (transcript + Q&A) are saved and viewable
under **Past meetings**. The windows stay off the taskbar; use the **IronStack
tray icon** (left-click to show/hide, right-click for Quit) to reach the app.

> Note: capturing system audio uses WASAPI loopback via `soundcard`, so this is
> Windows-oriented.

## Build a standalone .exe

Running `python main.py` shows up in Task Manager as **Python** (the interpreter
is the process). To ship it as **IronStack** with its own icon, bundle it into a
single windowed executable:

```powershell
pip install -r requirements.txt   # includes PyInstaller
.\build.ps1                       # -> dist\IronStack.exe
```

`build.ps1` runs PyInstaller with `assets\IronStack.ico` embedded, so
`IronStack.exe` appears with its own name and icon in the taskbar / Task Manager
/ Explorer. The SQLite database is created in a `data\` folder beside the exe.

### API keys

The build **bakes the project's `.env` into the exe**, so `IronStack.exe` runs
standalone on any PC without a separate `.env`.

> ⚠️ Anyone with the exe can extract those keys (the bundled `.env` is recoverable
> from the binary). **Do not distribute this build publicly.** Set spending limits
> on the keys and rotate them if the exe leaks.

Key resolution order at runtime (first hit wins): a `.env` next to the exe →
the working directory → the baked-in `.env`. Environment variables override all
of them. So a user can still drop their own `.env` beside the exe to override the
baked-in keys.
