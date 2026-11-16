#!/usr/bin/env python3
"""Measure approximate acoustic latency to one Bluetooth speaker using a USB mic.

Run:
  python3 measure_speaker_latency.py --list
  python3 measure_speaker_latency.py --speaker "JBL Go 3"

The Bluetooth playback process stays open for the entire test. Each trial sends
a longer continuous 1 kHz tone, with silence between trials to make detection
and repeat measurements easier.
"""
import argparse
import math
import shutil
import subprocess
import sys
import time
from array import array

RATE = 48000
TONE_SECONDS = 1.5
CAPTURE_SECONDS = 4.0
READ_BYTES = 960  # 10 ms of mono, signed 16-bit audio
DEFAULT_MIC_SOURCE = "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"
TONE_HZ = 1000
AMPLITUDE = 11000


def command_text(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Command failed").strip())
    return p.stdout


def make_tone(seconds):
    """Generate a continuous 1 kHz tone with short fades to avoid clicks."""
    n = int(RATE * seconds)
    samples = array("h")
    fade_n = int(RATE * 0.015)
    for i in range(n):
        fade = min(1.0, i / fade_n, (n - 1 - i) / fade_n)
        samples.append(int(AMPLITUDE * max(0.0, fade) *
                           math.sin(2 * math.pi * TONE_HZ * i / RATE)))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def find_tone_start(recorded_bytes, template_bytes):
    """Find the first matching 120 ms section of the longer continuous tone."""
    import numpy as np
    x = np.frombuffer(recorded_bytes, dtype="<i2").astype(np.float32)
    t = np.frombuffer(template_bytes, dtype="<i2").astype(np.float32)
    if len(x) < len(t):
        return None
    corr = np.correlate(x, t, mode="valid")
    energy = np.sqrt(np.maximum(
        1.0, np.convolve(x * x, np.ones(len(t), dtype=np.float32), mode="valid")))
    score = corr / energy
    index = int(np.argmax(score))
    return index if float(score[index]) >= 0.18 else None


def start_player(sink):
    return subprocess.Popen(
        ["pw-cat", "--playback", "--raw", "--target", sink,
         "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=None, bufsize=0
    )


def stop_process(proc):
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List PipeWire sources and sinks.")
    ap.add_argument("--source", default=DEFAULT_MIC_SOURCE,
                    help="Microphone source (defaults to your C-Media USB microphone).")
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
            raise SystemExit("No PipeWire sink found. Connect the speaker and run --list.")

    try:
        import numpy  # noqa: F401
    except ImportError:
        raise SystemExit("Install NumPy first: sudo apt install python3-numpy")

    tone = make_tone(TONE_SECONDS)
    detector_template = make_tone(0.12)
    silence = bytes(int(RATE * 0.75) * 2)
    results = []
    recorder = None
    player = None

    print(f"Microphone: {args.source}")
    print(f"Speaker:    {sink}")
    print(f"Test tone:  {TONE_SECONDS:.1f} seconds at {TONE_HZ} Hz")
    print("Keep the microphone a few inches from the speaker.")
    print("Opening the Bluetooth playback stream and warming it up...", flush=True)

    try:
        # Start the playback stream once and keep it open across every trial.
        player = start_player(sink)
        player.stdin.write(silence)
        player.stdin.flush()
        time.sleep(1.0)
        if player.poll() is not None:
            raise RuntimeError(f"pw-cat playback exited with code {player.returncode}")

        for trial in range(1, max(1, args.trials) + 1):
            recorder = subprocess.Popen(
                ["pw-cat", "--record", "--raw", "--target", args.source,
                 "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
                stdout=subprocess.PIPE, stderr=None, bufsize=0
            )
            try:
                captured = bytearray()
                capture_origin = None
                submit_time = None
                target_bytes = int(CAPTURE_SECONDS * RATE * 2)
                print(f"Trial {trial}/{max(1, args.trials)}: recording; tone will start after 1 second...",
                      flush=True)

                while len(captured) < target_bytes:
                    block = recorder.stdout.read(READ_BYTES)
                    read_time = time.monotonic()
                    if not block:
                        raise RuntimeError("Microphone stream ended; check the source name.")
                    if capture_origin is None:
                        # Approximate time represented by the first sample in this block.
                        capture_origin = read_time - len(block) / (RATE * 2)
                    captured.extend(block)

                    if submit_time is None and len(captured) >= RATE * 2:
                        if player.poll() is not None:
                            raise RuntimeError("Bluetooth playback stream stopped unexpectedly.")
                        print("  Sending continuous tone...", flush=True)
                        submit_time = time.monotonic()
                        player.stdin.write(tone)
                        player.stdin.flush()

                raw = bytes(captured[:len(captured) - len(captured) % 2])
                recorded = array("h")
                recorded.frombytes(raw)
                if sys.byteorder != "little":
                    recorded.byteswap()

                onset = find_tone_start(recorded.tobytes(), detector_template)
                if onset is None:
                    print("  Tone not detected. Check mic placement and speaker volume.")
                else:
                    detected_time = capture_origin + onset / RATE
                    latency_ms = (detected_time - submit_time) * 1000
                    results.append(latency_ms)
                    print(f"  Estimated latency: {latency_ms:.1f} ms")

            finally:
                stop_process(recorder)
                recorder = None

            # Keep the same playback process alive; feed silence between trials.
            player.stdin.write(silence)
            player.stdin.flush()
            time.sleep(0.8)

    finally:
        stop_process(recorder)
        stop_process(player)

    if results:
        print("\nResults:")
        for i, value in enumerate(results, 1):
            print(f"  Trial {i}: {value:.1f} ms")
        print(f"  Average: {sum(results) / len(results):.1f} ms")
        print(f"  Min / max: {min(results):.1f} / {max(results):.1f} ms")
        print("\nThese are approximate end-to-end measurements, not lab-grade timing.")
        print("They include PipeWire, Bluetooth, speaker, acoustic, and microphone timing effects.")
    else:
        print("\nNo valid measurements. Check the microphone, speaker, and source/sink names.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
