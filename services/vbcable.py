"""VB-CABLE helper: detect / install the official virtual audio cable so the
Host can inject the Viewer's mic into a path meeting apps see as a microphone.

VB-CABLE (https://vb-cable.com) is donationware by VB-Audio. IronStack ships the
official unmodified Driver Pack and launches VB-Audio's own installer when the
cable is missing. See vendor/vbcable/NOTICE.txt.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import soundcard as sc

from config import resource_path
from logsetup import get_logger
from services.win_audio_defaults import ensure_real_playback_default

# Official pack (https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip).
PACK_NAME = "VBCABLE_Driver_Pack45.zip"
PACK_SHA256 = "B950E39F01AF1D04EA623C8F6D8EB9B6EA5C477C637295FABF20631C85116BFB"
PACK_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"

CABLE_INPUT_MARKERS = ("cable input",)
CABLE_OUTPUT_MARKERS = ("cable output",)

_install_lock = threading.Lock()


def pack_path() -> Path:
    return resource_path("vendor", "vbcable", PACK_NAME)


def is_installed() -> bool:
    """True when both CABLE Input (playback) and CABLE Output (capture) exist."""
    try:
        speakers = sc.all_speakers()
        mics = sc.all_microphones(include_loopback=False)
    except Exception:
        get_logger().exception("vbcable: could not list audio devices")
        return False
    has_in = any(any(m in s.name.lower() for m in CABLE_INPUT_MARKERS) for s in speakers)
    has_out = any(any(m in mic.name.lower() for m in CABLE_OUTPUT_MARKERS) for mic in mics)
    return has_in and has_out


def find_cable_input_speaker():
    """Playback endpoint that feeds CABLE Output (the virtual mic). None if missing."""
    try:
        for s in sc.all_speakers():
            name = s.name.lower()
            if any(m in name for m in CABLE_INPUT_MARKERS):
                return s
    except Exception:
        get_logger().exception("vbcable: could not list speakers")
    return None


def _verify_pack(path: Path) -> None:
    dig = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            dig.update(chunk)
    got = dig.hexdigest().upper()
    if got != PACK_SHA256:
        raise RuntimeError(
            f"VB-CABLE pack hash mismatch (got {got}, expected {PACK_SHA256})"
        )


def _setup_exe_name() -> str:
    # Prefer the arch matching this process; fall back to the other.
    if sys.maxsize > 2**32:
        return "VBCABLE_Setup_x64.exe"
    return "VBCABLE_Setup.exe"


def _run_setup(setup: Path) -> int:
    """Run VB-Audio's installer elevated. Returns process exit code (or -1)."""
    # -i install, -h hide UI. Windows may still show a driver-trust dialog.
    args = [str(setup), "-i", "-h"]
    get_logger().info("vbcable: launching installer %s", setup)
    try:
        # ShellExecuteW "runas" triggers UAC when we are not already elevated
        # (IronStack's frozen build usually is, via --uac-admin).
        if sys.platform == "win32":
            import ctypes
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(setup), "-i -h", str(setup.parent), 1)
            # ShellExecute returns >32 on success (a HINSTANCE-like value).
            if rc <= 32:
                get_logger().error("vbcable: ShellExecute failed (code %s)", rc)
                return -1
            # ShellExecute does not wait; poll until the setup process exits.
            deadline = time.monotonic() + 180.0
            while time.monotonic() < deadline:
                # Tasklist match is imperfect but avoids needing psutil.
                check = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {setup.name}"],
                    capture_output=True, text=True, creationflags=0x08000000)
                if setup.name.lower() not in (check.stdout or "").lower():
                    return 0
                time.sleep(1.0)
            get_logger().warning("vbcable: installer still running after timeout")
            return 0
        proc = subprocess.run(args, cwd=str(setup.parent))
        return int(proc.returncode)
    except Exception:
        get_logger().exception("vbcable: installer launch failed")
        return -1


def install() -> bool:
    """Extract the bundled pack and run VB-Audio's installer. True on launch OK."""
    src = pack_path()
    if not src.is_file():
        get_logger().error(
            "vbcable: pack missing at %s — rebuild with vendor/vbcable bundled, "
            "or download from %s", src, PACK_URL)
        return False
    try:
        _verify_pack(src)
    except Exception:
        get_logger().exception("vbcable: pack verification failed")
        return False

    tmp = Path(tempfile.mkdtemp(prefix="ironstack-vbcable-"))
    try:
        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(tmp)
        # Pack may extract flat or under a single top folder.
        setup_name = _setup_exe_name()
        setup = tmp / setup_name
        if not setup.is_file():
            found = list(tmp.rglob(setup_name))
            if not found:
                # x64 pack on 32-bit (or vice versa): try the other binary.
                alt = "VBCABLE_Setup.exe" if setup_name.endswith("x64.exe") \
                    else "VBCABLE_Setup_x64.exe"
                found = list(tmp.rglob(alt))
            if not found:
                get_logger().error("vbcable: %s not found in pack", setup_name)
                return False
            setup = found[0]
        rc = _run_setup(setup)
        return rc == 0
    finally:
        # Leave files until installer exits; best-effort cleanup later.
        try:
            # Delay delete: installer may still be reading from tmp briefly.
            def _cleanup(path=tmp):
                time.sleep(30)
                shutil.rmtree(path, ignore_errors=True)
            threading.Thread(target=_cleanup, daemon=True).start()
        except Exception:
            pass


def ensure_installed(wait_s: float = 45.0) -> bool:
    """Ensure CABLE endpoints exist; install if needed and wait for them.

    Returns True when inject is possible. After a fresh install Windows often
    requires a reboot before devices appear — in that case we return False and
    log a clear reboot message.

    When CABLE is already present (or just became ready), restore a real Windows
    default *playback* device if the installer stole it — otherwise meeting
    apps play the interviewer into CABLE and IronStack's headset loopback
    (return audio to the Viewer) goes silent in mic mode.
    """
    with _install_lock:
        if is_installed():
            ensure_real_playback_default()
            return True
        if sys.platform != "win32":
            get_logger().error("vbcable: only supported on Windows")
            return False
        get_logger().info(
            "vbcable: not detected — installing official VB-CABLE "
            "(donationware, https://vb-cable.com)")
        if not install():
            return False
        deadline = time.monotonic() + max(5.0, wait_s)
        while time.monotonic() < deadline:
            if is_installed():
                get_logger().info("vbcable: CABLE Input/Output ready")
                ensure_real_playback_default()
                return True
            time.sleep(1.0)
        get_logger().error(
            "vbcable: installer finished but CABLE devices are not visible yet. "
            "Reboot the PC to finalize VB-CABLE, then set the meeting app's "
            "microphone to 'CABLE Output (VB-Audio Virtual Cable)'.")
        return False
