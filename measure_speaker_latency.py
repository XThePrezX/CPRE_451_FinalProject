#!/usr/bin/env python3
"""Approximate acoustic-latency test: submit a tone to one speaker and detect it on a microphone.

Requires PipeWire (pw-cat, pactl) and NumPy. Run:
  python3 measure_speaker_latency.py --list
  python3 measure_speaker_latency.py --source YOUR_MIC_SOURCE --speaker "JBL Go 3"

Keep the microphone close to the speaker and use the same placement for each test.
The reported value is approximate and includes PipeWire capture buffering/read
timing as well as Bluetooth, speaker, and acoustic latency.
"""
import argparse
import math
import shutil
import subprocess
import sys
import time
from array import array

RATE = 48000
TONE_SECONDS = 0.18
CAPTURE_SECONDS = 3.0
READ_BYTES = 960  # 10 ms, mono s16le


def command_text(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Command failed").strip())
    return p.stdout


def make_tone():
    n = int(RATE * TONE_SECONDS)
    samples = array("h")
    for i in range(n):
        fade = min(1.0, i / (RATE * 0.01), (n - 1 - i) / (RATE * 0.01))
        samples.append(int(11000 * max(0.0, fade) *
                           math.sin(2 * math.pi * 1000 * i / RATE)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def find_tone_start(recorded_bytes, tone_bytes):
    import numpy as np
    x = np.frombuffer(recorded_bytes, dtype="<i2").astype(np.float32)
    t = np.frombuffer(tone_bytes, dtype="<i2").astype(np.float32)
    if len(x) < len(t):
        return None
    corr = np.correlate(x, t, mode="valid")
    energy = np.sqrt(np.maximum(
        1.0, np.convolve(x * x, np.ones(len(t), dtype=np.float32), mode="valid")))
    score = corr / energy
    index = int(np.argmax(score))
    return index if float(score[index]) >= 0.18 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List PipeWire sources and sinks.")
    ap.add_argument("--source", help="Microphone source name from pactl list short sources.")
    ap.add_argument("--sink", help="Speaker sink name; overrides --speaker lookup.")
    ap.add_argument("--speaker", help="Speaker name from config.py, e.g. 'JBL Go 3'.")
    ap.add_argument("--trials", type=int, default=3)
    args = ap.parse_args()

    for cmd in ("pw-cat", "pactl"):
        if not shutil.which(cmd):
            raise SystemExit(f"Missing command: {cmd}")
    if args.list:
        print("=== Sources (microphones) ===")
        print(command_text(["pactl", "list", "short", "sources"]))
        print("=== Sinks (speakers) ===")
        print(command_text(["pactl", "list", "short", "sinks"]))
        return
    if not args.source:
        raise SystemExit("Specify --source. Run --list to find your microphone.")
    sink = args.sink
    if not sink:
        if not args.speaker:
            raise SystemExit("Specify --sink or --speaker.")
        from config import SPEAKERS
        matches = [s for s in SPEAKERS if s["name"].lower() == args.speaker.lower()]
        if not matches:
            raise SystemExit("Speaker not found in config.py. Names: " +
                             ", ".join(s["name"] for s in SPEAKERS))
        mac = matches[0]["device_mac"].replace(":", "_")
        sinks = command_text(["pactl", "list", "short", "sinks"])
        sink = next((line.split()[1] for line in sinks.splitlines()
                     if len(line.split()) >= 2 and
                     line.split()[1].startswith("bluez_output." + mac)), None)
        if not sink:
            raise SystemExit("No PipeWire sink found for that speaker. Connect it and run --list.")
    try:
        import numpy  # noqa
    except ImportError:
        raise SystemExit("Install NumPy first: python3 -m pip install numpy")

    tone = make_tone()
    results = []
    print(f"Mic source: {args.source}\nSpeaker sink: {sink}")
    print("Place the mic close to the speaker. A short 1 kHz tone will play.")
    for trial in range(1, max(1, args.trials) + 1):
        recorder = subprocess.Popen(
            ["pw-cat", "--record", "--raw", "--target", args.source,
             "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
            stdout=subprocess.PIPE, stderr=None, bufsize=0)
        try:
            time.sleep(0.35)
            captured = bytearray()
            first_read_time = None
            submit_time = None
            target_size = int(CAPTURE_SECONDS * RATE * 2)
            player = None
            print(f"Trial {trial}: recording...", flush=True)
            while len(captured) < target_size:
                block = recorder.stdout.read(READ_BYTES)
                read_time = time.monotonic()
                if not block:
                    raise RuntimeError("Microphone stream ended; verify the source name.")
                if first_read_time is None:
                    first_read_time = read_time
                captured.extend(block)
                if submit_time is None and len(captured) >= RATE * 2:
                    player = subprocess.Popen(
                        ["pw-cat", "--playback", "--raw", "--target", sink,
                         "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=None, bufsize=0)
                    submit_time = time.monotonic()
                    player.stdin.write(tone)
                    player.stdin.flush()
                    player.stdin.close()
                    player.wait(timeout=5)
            raw = bytes(captured[:len(captured) - len(captured) % 2])
            recorded = array("h")
            recorded.frombytes(raw)
            if sys.byteorder != "little":
                recorded.byteswap()
            onset = find_tone_start(recorded, tone)
            if onset is None:
                print("  Tone not detected. Move mic closer or raise speaker volume.")
            else:
                # Approximate because Python's first read time includes capture buffering.
                estimated_heard_time = first_read_time + onset / RATE
                ms = (estimated_heard_time - submit_time) * 1000
                results.append(ms)
                print(f"  Estimated latency: {ms:.1f} ms")
        finally:
            if recorder.poll() is None:
                recorder.terminate()
            try:
                recorder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait()
        time.sleep(0.5)

    if results:
        print("\nResults:")
        for i, value in enumerate(results, 1):
            print(f"  Trial {i}: {value:.1f} ms")
        print(f"  Average: {sum(results) / len(results):.1f} ms")
        print(f"  Min / max: {min(results):.1f} / {max(results):.1f} ms")
        print("\nTreat this as an approximate Y_i measurement, not a lab-grade latency.")
        print("It includes microphone/ PipeWire capture buffering and Python read-timing uncertainty.")
    else:
        print("No valid measurements. Check the microphone, speaker, and source/sink names.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
