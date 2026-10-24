"""Kokoro neural TTS for `mother speak` (engine "kokoro"). Runs inside ~/.mother/tts/.venv, not the stdlib CLI.

usage: tts_kokoro.py <model dir> <voice> <speed> <script.txt> <out.wav>
Voices: af_* / am_* (US English), bf_* / bm_* (UK English); af_heart is the default. No Hebrew.
"""
import sys

import soundfile as sf
from kokoro_onnx import Kokoro


def main(d, voice, speed, src, out):
    k = Kokoro(f"{d}/kokoro-v1.0.fp16.onnx", f"{d}/voices-v1.0.bin")  # bundled espeak-ng via espeakng_loader
    text = open(src, encoding="utf-8").read().strip()
    samples, rate = k.create(text, voice=voice, speed=float(speed), lang="en-gb" if voice[:1] == "b" else "en-us")
    sf.write(out, samples, rate)


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    main(*sys.argv[1:])
