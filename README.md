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
  transcriber.py Captures an audio source (loopback OR mic) and streams it to Deepgram STT.
  audio_utils.py Finds the loopback / microphone devices and resamples audio.
  answerer.py    Sends a question to the OpenAI Responses API.
  context.py     Builds the system prompt from the selected documents.
  netlink.py     Discovery + Host↔Viewer streaming of the live meeting (TCP/UDP).
  upnp.py        Best-effort automatic router port-opening (UPnP) so the host is
                 reachable over the internet without manual port-forwarding.

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

## Networking (Host / Viewer)

The overlay can hide itself from screen capture, but that stealth silently fails
under Remote Desktop and many VMs (no GPU/DWM composition). The networking
feature is the workaround: run the meeting on the watched/remote machine (the
**Host**) but read the transcript and answers on your own **local machine** (a
**Viewer**), so nothing sensitive is ever drawn on the shared screen.

- The **Host** captures **both** sides of the conversation — the interviewer
  (system loopback) *and* the candidate's own voice (microphone) — transcribes
  each, generates the AI answer, and streams all three as text to Viewers. It
  broadcasts a UDP beacon so Viewers can discover it on the LAN.
- A **Viewer** discovers hosts (or connects by IP) and mirrors the meeting live:
  interviewer questions, the candidate's spoken answers ("You"), and the AI answer.
  The candidate's talking is shown only on Viewers, not on the Host.

**Hosting is automatic.** When you Start an interview, that PC becomes a Host and
appears to Viewers — no host switch, no pairing code. Each Viewer that connects
pops an **Accept / Reject** dialog on the Host; the stream flows only once the
Host accepts. (The Viewer window has a blue brand dot to tell it from the Host.)

Also over the wire (both directions): a **shared note** panel (a live two-way
notepad), and **remote control** — toggling **Stealth**, hitting **⟳ Restart
Deepgram**, or changing the **language** on either side is mirrored to the other.

To join: open **Network** in the setup window, pick the discovered host — or type
its IP/port (`127.0.0.1` : `48921` for same-PC testing; the last address is
remembered) — and click *Join as Viewer*. Only text is sent over the wire, never
audio.

### Over the internet
Auto-discovery only works on the same LAN. To reach a Host across the internet it
must be reachable inbound, which residential NAT normally blocks. Options:

- **UPnP (automatic).** On Start, the Host asks the router to open its port via
  UPnP ([upnp.py](services/upnp.py)); if the router allows it, Viewers can connect
  to the Host's public IP + port with no manual setup. The console logs whether it
  succeeded.
- **Manual port-forward.** Forward TCP `48921` (or your chosen port) on the Host's
  router to the Host PC — only works if the ISP isn't using CGNAT.
- **Tunnel** (Tailscale/ngrok) if UPnP is off and port-forwarding isn't possible.

The Host's **listen port** is configurable in the Network page, so it can match a
VPN/router forwarded port.

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
