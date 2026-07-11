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
  remote.py      Remote screen stream over TCP (drop-at-source, NODELAY, ~720p).
  vcodec.py      H.264 encode/decode (PyAV, hardware-preferred).
  screencap.py   Monitor enumeration + capture (dxcam fast path, mss fallback).
  inputinject.py Win32 SendInput — mouse/keyboard injection on the Host.

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

**The host servers open at app start** (a `HostSession` owns the control + media
sockets, beacon and UPnP for the whole app run) — so the PC is discoverable,
connectable and remote-controllable from launch, no host switch, no pairing code.
A Viewer can connect during setup and stays connected when the interview starts.
Each Viewer that connects pops an **Accept / Reject** dialog on the Host; the
stream flows only once the Host accepts. (The Viewer window has a blue brand dot
to tell it from the Host.)

Also over the wire (both directions): a **shared note** panel (a live two-way
notepad), and **remote control** — toggling **Stealth**, hitting **⟳ Restart
Deepgram**, or changing the **language** on either side is mirrored to the other.

To join: open **Network** in the setup window, pick the discovered host — or type
its IP/port (`127.0.0.1` : `48921` for same-PC testing; the last address is
remembered) — and click *Join as Viewer*. Only text is sent over the wire, never
audio.

### Over the internet — connect by ID through a relay
Reaching a Host directly across the internet needs it to be reachable inbound,
which residential NAT/CGNAT/VPNs normally block. Instead, IronStack connects **by
ID through a relay server** you run once on any machine with a public IP (a $5 VPS,
an EC2 box, etc.). Both the Host and the Viewer make **outbound** connections to
the relay — which always work, even behind CGNAT or a VPN — and the relay pairs
them by ID and pipes the bytes.

**1. Run the relay** on your public box (Python 3, no dependencies):

```bash
python3 relay_server.py            # listens on 0.0.0.0:48920
```

Open that **TCP port (48920)** in the OS firewall and, on a cloud VM, the Security
Group. It's a generic byte-pipe — it carries both the meeting-control channel and
the remote-screen channel, and multiplexes any number of rooms.

**2. On the Host:** open **Network**, set the **Relay server** to your box
(`host:port`, e.g. `54.254.60.12:48920`), tick **"Make this PC reachable by ID"**,
and share **Your connection ID** (shown on the page).

**3. On the Viewer:** open **Network**, set the same **Relay server**, type the
Host's **connection ID**, and click **Join as Viewer**.

No port-forwarding, no UPnP, no public Host IP — it works through CGNAT and
alongside a VPN. The relay ([relay_server.py](relay_server.py)) keeps a small pool
of parked Host connections ([relaylink.py](services/relaylink.py)) and heartbeats
them so the NAT mapping stays open; each Viewer that joins claims one and the Host
opens a replacement. Everything on top — transcript, notes, remote screen/control —
rides through unchanged.

> Latency note: traffic goes Host → relay → Viewer, so place the relay sensibly
> (near one end, or on the path between them) to minimise the detour.

The LAN UDP discovery beacon ([netlink.py](services/netlink.py)) and automatic
UPnP port-opening ([upnp.py](services/upnp.py)) still run for same-network use, but
the relay is the reliable path over the internet.

### Remote screen view / control
Once connected, the Viewer clicks **Remote** to view and control the Host's screen
(like AnyDesk). It uses **two TCP sockets**, split by need:

- **Screen → its own TCP socket** (`media_port` = control port + 1, UPnP-opened).
  The chosen monitor is captured ([screencap.py](services/screencap.py): dxcam
  fast path, mss fallback), H.264-encoded ([vcodec.py](services/vcodec.py),
  hardware-preferred, ~720p), and streamed. TCP is *reliable* — no skipped/garbled
  frames — and latency is kept low by **drop-at-source** (the capture loop blocks
  on send, then grabs a fresh frame, so nothing queues and fps adapts to the
  link), **`TCP_NODELAY`**, and a small send buffer.
- **Input → the netlink control channel** (the other socket). Mouse/keyboard
  events (normalized monitor coords) ride the reliable, video-free channel — never
  dropped, never stuck behind a frame — and are injected on the Host via Win32
  `SendInput` ([inputinject.py](services/inputinject.py)).

Keyboard uses a global low-level hook ([keyhook.py](services/keyhook.py)) while the
remote window is focused, so **all** keys and combos reach the host — Ctrl+C/V, the
Windows key, function keys — not just what Qt would pass through. Alt+Tab is left
local so you can always switch away.

The screen socket is guarded by a one-time **token** issued over the already-
accepted control channel (no extra prompt). The Viewer window is a floating
RDP/VMware-style toolbar (drag left/right) with a **monitor picker**, a **quality**
selector (Low/Medium/High — up to full 1080p), **fullscreen** (Esc exits), and a
**View only** toggle. The Host shows nothing about the sharing.

The Host also sends its current **cursor shape** (I-beam over text, hand over
links, wait/busy, …) so the Viewer's pointer matches what the Host is doing.

When connected **by ID through the relay**, the screen rides the relay's separate
"screen" channel automatically — no second port to open, nothing extra to forward.
On a direct LAN connection it's plain TCP to `media_port`, so the Host must be
reachable there too (UPnP, or the same port-forward as the control port).

### Live meeting audio
As soon as a Viewer connects, it also **hears the meeting**: the Host captures both
its **system output** (loopback — the interviewer's voice) and its **microphone**
(the candidate's voice), mixes them, and streams the mix to the Viewer, which plays
it on its own speakers ([audiostream.py](services/audiostream.py)). It uses a third
dedicated socket (`audio_port` = control port + 2; the relay's "audio" channel over
the internet). The payload is raw PCM (mono, 24 kHz) with drop-oldest buffering, so
a slow link causes brief drop-outs rather than growing delay. Capture runs only
while a Viewer is listening. Only text and this audio mix cross the wire — never
the raw microphone/loopback device streams.

**Elevated windows.** A non-elevated app can't inject input into elevated/admin
windows (Windows UIPI) — so some modals (e.g. AnyDesk's Accept dialog, other
admin apps) can't be clicked. The build **requests UAC elevation at launch** (one
exe for both roles: the Viewer just accepts the prompt, the Host runs elevated so
it can control admin-level windows). The **UAC secure desktop** itself and
**Ctrl+Alt+Delete** still can't be reached without a SYSTEM service (a later phase).

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
