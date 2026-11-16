#!/usr/bin/env python3
"""Measure approximate acoustic latency to a Bluetooth speaker using a USB mic.

The Bluetooth playback stream and microphone recording stream each stay open
for the entire test. Each trial uses a distinctive three-burst tone pattern.
Detection is performed on the microphone signal's short-time RMS envelope.
"""
import argparse
import math
import shutil
import subprocess
import sys
import time
from array import array

RATE = 48000
CAPTURE_SECONDS_PER_TRIAL = 2.2
PRE_ROLL_SECONDS = 1.0
BETWEEN_TRIALS_SECONDS = 1.2
READ_BYTES = 1920  # 20 ms of mono, signed 16-bit audio
ENVELOPE_MS = 5
DEFAULT_MIC_SOURCE = (
    "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"
)
AMPLITUDE = 11000

# A distinctive pattern: three tones of different lengths/frequencies,
# separated by silence. The full envelope is used for onset detection.
BURSTS = [(1000, 0.12), (1500, 0.24), (750, 0.12)]
GAP_SECONDS = 0.14
PATTERN_SECONDS = sum(d for _, d in BURSTS) + GAP_SECONDS * (len(BURSTS) - 1)


def command_text(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Command failed").strip())
    return p.stdout


def make_pattern():
    samples = array("h")
    for index, (frequency, duration) in enumerate(BURSTS):
        count = int(RATE * duration)
        fade_n = int(RATE * 0.008)
        for i in range(count):
            fade = min(1.0, i / fade_n, (count - 1 - i) / fade_n)
            value = AMPLITUDE * max(0.0, fade) * math.sin(
                2 * math.pi * frequency * i / RATE
            )
            samples.append(int(value))
        if index != len(BURSTS) - 1:
            samples.extend([0] * int(RATE * GAP_SECONDS))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


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


def build_envelope_template():
    """Return expected 5 ms RMS-envelope shape for the three-burst pattern."""
    step = RATE * ENVELOPE_MS // 1000
    template = []
    for i, (_, duration) in enumerate(BURSTS):
        template.extend([1.0] * max(1, round(duration * 1000 / ENVELOPE_MS)))
        if i != len(BURSTS) - 1:
            template.extend([0.0] * round(GAP_SECONDS * 1000 / ENVELOPE_MS))
    return template, step


def find_pattern_start(recorded_bytes):
    """Find the pattern using correlation against its burst/silence envelope."""
    import numpy as np

    audio = np.frombuffer(recorded_bytes, dtype="<i2").astype(np.float32)
    window = RATE * ENVELOPE_MS // 1000
    usable = len(audio) // window * window
    if usable < window:
        return None

    frames = audio[:usable].reshape(-1, window)
    envelope = np.sqrt(np.mean(frames * frames, axis=1))
    # Remove the local noise floor and normalize, so mic gain is less important.
    noise_floor = float(np.percentile(envelope, 25))
    envelope = np.maximum(envelope - noise_floor, 0.0)
    peak = float(np.percentile(envelope, 95))
    if peak < 100:
        return None
    envelope = np.clip(envelope / peak, 0.0, 1.5)

    template, _ = build_envelope_template()
    t = np.asarray(template, dtype=np.float32)
    if len(envelope) < len(t):
        return None

    # Correlation on the low-rate (200 Hz) envelope is inexpensive on a Pi.
    scores = np.correlate(envelope, t, mode="valid")
    # Normalize by the template energy; then prefer strong burst energy and
    # low energy in the expected gaps.
    scores /= max(float(np.linalg.norm(t)), 1e-6)
    best = int(np.argmax(scores))
    # Require a clear signal in the burst windows.
    if float(scores[best]) < 2.0:
        return None
    return best * window


def start_player(sink):
    return subprocess.Popen(
        ["pw-cat", "--playback", "--raw", "--target", sink,
         "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=None, bufsize=0
    )


def start_recorder(source):
    return subprocess.Popen(
        ["pw-cat", "--record", "--raw", "--target", source,
         "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
        stdout=subprocess.PIPE, stderr=None, bufsize=0
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List PipeWire sources and sinks.")
    ap.add_argument("--source", default=DEFAULT_MIC_SOURCE,
                    help="Microphone source (defaults to your C-Media USB microphone).")
    ap.add_argument("--sink", help="Speaker sink name; overrides --speaker lookup.")
    ap.add_argument("--speaker", help="Speaker name from config.py, e.g. 'JBL Go 3'.")
    ap.add_argument("--trials", type=int, default=10)
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

    pattern = make_pattern()
    silence = bytes(int(RATE * 0.25) * 2)
    results = []
    player = recorder = None

    print(f"Microphone: {args.source}")
    print(f"Speaker:    {sink}")
    print("Test pattern: three short tones (1000, 1500, 750 Hz)")
    print(f"Trials: {max(1, args.trials)}")
    print("Both PipeWire streams will remain open for the entire test.")
    print("Keep the microphone a few inches from the speaker.")
    print("Starting streams and warming up...", flush=True)

    try:
        player = start_player(sink)
        recorder = start_recorder(args.source)

        # Keep both streams active before the first measured pattern.
        warm_silence = bytes(int(RATE * PRE_ROLL_SECONDS) * 2)
        player.stdin.write(warm_silence)
        player.stdin.flush()

        captured = bytearray()
        capture_origin = None
        total_bytes = 0
        next_trial = 0
        trial_submit_times = []
        trial_labels = []
        start_after_bytes = int(RATE * PRE_ROLL_SECONDS * 2)
        trial_spacing = PATTERN_SECONDS + BETWEEN_TRIALS_SECONDS
        next_submit_time = time.monotonic() + PRE_ROLL_SECONDS

        # Read continuously. Schedule each pattern against the same monotonic
        # clock used to timestamp the microphone stream.
        test_start = time.monotonic() + PRE_ROLL_SECONDS
        next_submit_time = test_start
        end_time = test_start + max(1, args.trials) * trial_spacing + 1.0
        print("Recording continuously; sending patterns...", flush=True)

        while time.monotonic() < end_time:
            block = recorder.stdout.read(READ_BYTES)
            read_time = time.monotonic()
            if not block:
                raise RuntimeError("Microphone stream ended; check the source name.")
            if capture_origin is None:
                capture_origin = read_time - len(block) / (RATE * 2)
            captured.extend(block)
            total_bytes += len(block)

            now = time.monotonic()
            if next_trial < max(1, args.trials) and now >= next_submit_time:
                if player.poll() is not None:
                    raise RuntimeError("Bluetooth playback stream stopped unexpectedly.")
                trial_submit_times.append(time.monotonic())
                trial_labels.append(next_trial + 1)
                player.stdin.write(pattern)
                player.stdin.flush()
                next_trial += 1
                next_submit_time = test_start + next_trial * trial_spacing

        # Close only after every pattern and the trailing capture interval.
        if next_trial < max(1, args.trials):
            print("Warning: not all trials were submitted.")

        # Match each expected pattern near its scheduled location. Searching
        # locally prevents one trial from being mistaken for another.
        raw = bytes(captured[:len(captured) - len(captured) % 2])
        import numpy as np
        audio = np.frombuffer(raw, dtype="<i2")
        window = RATE * ENVELOPE_MS // 1000
        frames = audio[:len(audio) // window * window].astype(np.float32)
        frames = frames.reshape(-1, window)
        envelope = np.sqrt(np.mean(frames * frames, axis=1))
        noise_floor = float(np.percentile(envelope, 25))
        envelope = np.maximum(envelope - noise_floor, 0.0)
        peak = float(np.percentile(envelope, 95))
        if peak >= 100:
            envelope = np.clip(envelope / peak, 0.0, 1.5)
            template, _ = build_envelope_template()
            t = np.asarray(template, dtype=np.float32)
            template_len = len(t)
            for trial_num, submit_time in enumerate(trial_submit_times):
                expected_sample = int((submit_time - capture_origin) * RATE)
                expected_frame = max(0, expected_sample // window)
                # Search a broad local window around expected arrival. This
                # tolerates Bluetooth delay while avoiding neighboring trials.
                lo = max(0, expected_frame - int(0.15 * RATE / window))
                hi = min(len(envelope), expected_frame + int(2.0 * RATE / window))
                segment = envelope[lo:hi]
                if len(segment) < template_len:
                    print(f"Trial {trial_num + 1}: insufficient capture window")
                    continue
                scores = np.correlate(segment, t, mode="valid")
                scores /= max(float(np.linalg.norm(t)), 1e-6)
                best_local = int(np.argmax(scores))
                best_score = float(scores[best_local])
                if best_score < 2.0:
                    print(f"Trial {trial_num + 1}: pattern not detected (score {best_score:.2f})")
                    continue
                onset_sample = (lo + best_local) * window
                detected_time = capture_origin + onset_sample / RATE
                latency_ms = (detected_time - submit_time) * 1000
                results.append(latency_ms)
                print(f"Trial {trial_num + 1}: {latency_ms:.1f} ms "
                      f"(detection score {best_score:.2f})")
        else:
            print("No clear microphone signal detected. Check mic placement and speaker volume.")

    finally:
        stop_process(recorder)
        stop_process(player)

    if results:
        print("\nResults:")
        for i, value in enumerate(results, 1):
            print(f"  Trial {i}: {value:.1f} ms")
        print(f"  Average: {sum(results) / len(results):.1f} ms")
        print(f"  Median:  {sorted(results)[len(results) // 2]:.1f} ms")
        print(f"  Min / max: {min(results):.1f} / {max(results):.1f} ms")
        print("\nUse the median as a preliminary Y_i estimate only if the trials cluster tightly.")
        print("The absolute value still includes PipeWire/microphone timing uncertainty.")
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
