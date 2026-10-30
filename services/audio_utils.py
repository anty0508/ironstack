import numpy as np
import soundcard as sc

from services.vbcable import find_cable_input_speaker
from storage import database

# Settings keys for the devices chosen on the setup window's Audio page. Empty
# value = follow the Windows default.
INPUT_SETTING = "audio_input_device"
OUTPUT_SETTING = "audio_output_device"


def _match(devices, name):
    """Find a device by saved name: exact match first, then case-insensitive
    substring (device names can gain/lose a suffix between runs). None if no hit."""
    if not name:
        return None
    for d in devices:
        if d.name == name:
            return d
    nl = name.lower()
    for d in devices:
        if nl in d.name.lower():
            return d
    return None


def chosen_output_name():
    return database.get_setting(OUTPUT_SETTING, "")


def chosen_input_name():
    return database.get_setting(INPUT_SETTING, "")


# Virtual-cable endpoints used for viewer-mic inject. Never use these as the
# Host's real speaker/mic for loopback or capture — after VB-CABLE install,
# Windows often makes CABLE the default device; looping that back would echo
# the Viewer's own injected voice to the Viewer.
_VIRTUAL_SPEAKER_MARKERS = (
    "cable input",
    "voicemeeter input",
    "voicemeeter aux input",
    "voicemeeter vaio3 input",
    "blackhole",
)
_VIRTUAL_MIC_MARKERS = (
    "cable output",
    "voicemeeter output",
    "voicemeeter aux output",
    "voicemeeter vaio3 output",
    "blackhole",
)


def _is_virtual_speaker(name):
    n = (name or "").lower()
    return any(m in n for m in _VIRTUAL_SPEAKER_MARKERS)


def _is_virtual_mic(name):
    n = (name or "").lower()
    return any(m in n for m in _VIRTUAL_MIC_MARKERS)


def _is_virtual_audio_endpoint(name):
    return _is_virtual_speaker(name) or _is_virtual_mic(name)


def _real_speakers():
    return [s for s in sc.all_speakers() if not _is_virtual_speaker(s.name)]


def _output_speaker():
    """The real speaker the user picked for output (or a real system default).

    Never returns VB-CABLE / VoiceMeeter Input — those are inject targets, not
    where the interviewer plays."""
    speakers = _real_speakers()
    match = _match(speakers, chosen_output_name())
    if match is not None:
        return match
    try:
        default = sc.default_speaker()
        if default is not None and not _is_virtual_speaker(default.name):
            return default
    except Exception:
        pass
    if not speakers:
        raise RuntimeError("no speaker found")
    return speakers[0]


def find_loopback():
    """Loopback of the real output device -- i.e. the interviewer's voice on the
    headset/speakers. Never binds to a virtual-cable endpoint (that would echo
    the Viewer's injected mic back to the Viewer).

    Must not fall back to a plain microphone: that made "viewer mic mode"
    (loopback-only return mix) go silent or wrong when the first non-virtual
    capture device was a mic with no meeting audio.
    """
    target = _output_speaker()
    mics = sc.all_microphones(include_loopback=True)
    target_l = target.name.lower()

    for mic in mics:
        ml = mic.name.lower()
        if _is_virtual_audio_endpoint(mic.name):
            continue
        if target_l in ml or ml in target_l:
            return mic

    # WASAPI loopbacks are usually named like the speaker they mirror.
    real_names = [s.name.lower() for s in _real_speakers()]
    for mic in mics:
        ml = mic.name.lower()
        if _is_virtual_audio_endpoint(mic.name):
            continue
        for sn in real_names:
            if sn in ml or ml in sn:
                return mic

    raise RuntimeError(
        f"no loopback for output {target.name!r}; "
        "set Windows default playback to your headphones (not CABLE Input)")


def find_microphone():
    """The real microphone (the candidate's own voice), not a loopback or
    virtual-cable capture device. Honors the chosen input device when it is a
    real mic; else the system default; else the first real mic."""
    mics = [m for m in sc.all_microphones(include_loopback=False)
            if not _is_virtual_mic(m.name)]

    match = _match(mics, chosen_input_name())
    if match is not None and not _is_virtual_mic(match.name):
        return match

    try:
        mic = sc.default_microphone()
        if mic is not None and not _is_virtual_mic(mic.name):
            return mic
    except Exception:
        pass

    if not mics:
        raise RuntimeError("no microphone found")
    return mics[0]


# Playback endpoints that feed a virtual microphone (meeting apps record from the
# paired capture side). Matched case-insensitively against speaker names.
_MIC_INJECT_MARKERS = (
    "cable input",
    "voicemeeter input",
    "voicemeeter aux input",
    "voicemeeter vaio3 input",
    "blackhole",
)


def _paired_inject_name(mic_name):
    """Map a virtual-mic capture name to its playback twin
    (e.g. 'CABLE Output …' -> 'CABLE Input …')."""
    for old, new in (("Output", "Input"), ("output", "input"),
                     ("Out", "In"), ("out", "in")):
        if old in mic_name:
            return mic_name.replace(old, new, 1)
    return mic_name


def find_mic_inject_speaker():
    """Speaker that injects into the Host's audio *input* path.

    Virtual cables (VB-Audio Cable, VoiceMeeter, BlackHole, …) expose a
    playback side and a capture side. Playing into the playback side makes the
    audio appear on the capture side, which meeting apps use as a microphone.

    Prefers VB-CABLE Input, then a speaker paired with the user's chosen input
    device, then any other known virtual-cable input. Raises if none exist —
    call services.vbcable.ensure_installed() first on the Host."""
    cable = find_cable_input_speaker()
    if cable is not None:
        return cable

    speakers = sc.all_speakers()

    mic_name = chosen_input_name()
    if mic_name:
        paired = _match(speakers, _paired_inject_name(mic_name))
        if paired is not None:
            return paired

    for marker in _MIC_INJECT_MARKERS:
        for s in speakers:
            if marker in s.name.lower():
                return s

    raise RuntimeError(
        "no virtual audio cable found to inject viewer mic into host input; "
        "IronStack will try to install VB-CABLE (donationware, vb-cable.com). "
        "After install/reboot, set the meeting app's microphone to "
        "'CABLE Output (VB-Audio Virtual Cable)'"
    )


def resample(audio, src, dst):
    if src == dst:
        return audio

    duration = len(audio) / src
    dst_len = int(duration * dst)

    src_x = np.linspace(0, 1, len(audio))
    dst_x = np.linspace(0, 1, dst_len)

    return np.interp(dst_x, src_x, audio)
