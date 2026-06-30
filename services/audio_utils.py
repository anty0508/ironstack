import numpy as np
import soundcard as sc


def find_loopback():
    speaker = sc.default_speaker()
    mics = sc.all_microphones(include_loopback=True)

    for mic in mics:
        if speaker.name.lower() in mic.name.lower():
            return mic

    return mics[0]


def resample(audio, src, dst):
    if src == dst:
        return audio

    duration = len(audio) / src
    dst_len = int(duration * dst)

    src_x = np.linspace(0, 1, len(audio))
    dst_x = np.linspace(0, 1, dst_len)

    return np.interp(dst_x, src_x, audio)