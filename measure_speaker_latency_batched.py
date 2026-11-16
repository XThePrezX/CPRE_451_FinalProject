#!/usr/bin/env python3
"""Measure approximate acoustic latency with persistent PipeWire streams.

Runs multiple batches while keeping ONE microphone recorder and ONE Bluetooth
playback stream open for the complete test. Reports event timestamps relative
to the test start, plus per-batch and overall averages.
"""
import argparse
import datetime as dt
import math
import shutil
import subprocess
import sys
import time
from array import array

RATE = 48000
READ_BYTES = 1920                 # 20 ms, mono s16le
ENVELOPE_MS = 5
PRE_ROLL_SECONDS = 2.0
BETWEEN_TRIALS_SECONDS = 1.2
DEFAULT_BATCHES = 5
DEFAULT_TRIALS_PER_BATCH = 10
DEFAULT_BATCH_PAUSE = 2.0
DEFAULT_MIC_SOURCE = (
    "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"
)
AMPLITUDE = 11000
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
            value = AMPLITUDE * max(0.0, fade) * math.sin(2 * math.pi * frequency * i / RATE)
            samples.append(int(value))
        if index != len(BURSTS) - 1:
            samples.extend([0] * int(RATE * GAP_SECONDS))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def start_player(sink):
    return subprocess.Popen(
        ["pw-cat", "--playback", "--raw", "--target", sink,
         "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=None, bufsize=0,
    )


def start_recorder(source):
    return subprocess.Popen(
        ["pw-cat", "--record", "--raw", "--target", source,
         "--rate", str(RATE), "--channels", "1", "--format", "s16", "-"],
        stdout=subprocess.PIPE, stderr=None, bufsize=0,
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


def build_template():
    template = []
    for i, (_, duration) in enumerate(BURSTS):
        template.extend([1.0] * max(1, round(duration * 1000 / ENVELOPE_MS)))
        if i != len(BURSTS) - 1:
            template.extend([0.0] * round(GAP_SECONDS * 1000 / ENVELOPE_MS))
    return template


def stats(values):
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {"avg": sum(values) / n, "median": median,
            "min": min(values), "max": max(values), "count": n}


def print_stats(label, values, unit="ms"):
    s = stats(values)
    if not s:
        print(f"  {label}: no valid values")
        return
    print(f"  {label}: avg {s['avg']:.1f} {unit} | median {s['median']:.1f} {unit} | "
          f"min/max {s['min']:.1f}/{s['max']:.1f} {unit} | n={s['count']}")


def resolve_sink(args):
    if args.sink:
        return args.sink
    if not args.speaker:
        raise SystemExit("Specify --sink or --speaker.")
    from config import SPEAKERS
    matches = [s for s in SPEAKERS if s["name"].lower() == args.speaker.lower()]
    if not matches:
        raise SystemExit("Speaker not found in config.py. Names: " + ", ".join(s["name"] for s in SPEAKERS))
    mac = matches[0]["device_mac"].replace(":", "_")
    sinks = command_text(["pactl", "list", "short", "sinks"])
    sink = next((line.split()[1] for line in sinks.splitlines()
                 if len(line.split()) >= 2 and line.split()[1].startswith("bluez_output." + mac)), None)
    if not sink:
        raise SystemExit("No PipeWire sink found. Connect the speaker and run --list.")
    return sink


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="List PipeWire sources and sinks.")
    ap.add_argument("--source", default=DEFAULT_MIC_SOURCE)
    ap.add_argument("--sink", help="PipeWire speaker sink; overrides --speaker.")
    ap.add_argument("--speaker", help="Speaker name from config.py, e.g. 'JBL Go 3'.")
    ap.add_argument("--batches", type=int, default=DEFAULT_BATCHES)
    ap.add_argument("--trials-per-batch", type=int, default=DEFAULT_TRIALS_PER_BATCH)
    ap.add_argument("--batch-pause", type=float, default=DEFAULT_BATCH_PAUSE,
                    help="Quiet pause between batches; streams stay open (default: 2 seconds).")
    args = ap.parse_args()

    if args.batches < 1 or args.trials_per_batch < 1 or args.batch_pause < 0:
        raise SystemExit("Batches/trials must be >= 1 and batch pause must be >= 0.")
    for cmd in ("pw-cat", "pactl"):
        if not shutil.which(cmd):
            raise SystemExit(f"Missing command: {cmd}")
    if args.list:
        print("=== Sources (microphones) ===\n" + command_text(["pactl", "list", "short", "sources"]))
        print("=== Sinks (speakers) ===\n" + command_text(["pactl", "list", "short", "sinks"]))
        return
    try:
        import numpy as np
    except ImportError:
        raise SystemExit("Install NumPy first: sudo apt install python3-numpy")

    sink = resolve_sink(args)
    total_trials = args.batches * args.trials_per_batch
    spacing = PATTERN_SECONDS + BETWEEN_TRIALS_SECONDS
    pattern = make_pattern()
    silence = bytes(int(RATE * PRE_ROLL_SECONDS) * 2)
    schedule = []
    cursor = PRE_ROLL_SECONDS
    for batch in range(args.batches):
        for trial_in_batch in range(args.trials_per_batch):
            schedule.append({"batch": batch + 1, "trial": trial_in_batch + 1,
                            "global": len(schedule) + 1, "offset": cursor})
            cursor += spacing
        if batch != args.batches - 1:
            cursor += args.batch_pause

    print(f"Microphone: {args.source}\nSpeaker:    {sink}")
    print(f"Batches: {args.batches} x {args.trials_per_batch} = {total_trials} trials")
    print(f"Quiet pause between batches: {args.batch_pause:.1f} s")
    print("ONE recorder and ONE playback stream will stay open through every batch.")
    print("Keep the microphone a few inches from the speaker; do not move it during the test.")
    print("Opening streams...", flush=True)

    player = recorder = None
    captured = bytearray()
    capture_origin = None
    submitted = []
    test_wall = None
    test_mono = None
    try:
        player = start_player(sink)
        recorder = start_recorder(args.source)
        # Warm both streams with silence before timing any trials.
        player.stdin.write(silence)
        player.stdin.flush()
        time.sleep(0.25)
        test_mono = time.monotonic() + 0.5
        test_wall = dt.datetime.now().astimezone() + dt.timedelta(seconds=0.5)
        end_offset = schedule[-1]["offset"] + PATTERN_SECONDS + 1.5
        end_mono = test_mono + (end_offset - PRE_ROLL_SECONDS)
        print(f"Streams open at wall clock: {dt.datetime.now().astimezone().isoformat(timespec='milliseconds')}")
        print("Warm-up complete. Starting continuous capture and scheduled trials...", flush=True)
        next_idx = 0
        while time.monotonic() < end_mono:
            block = recorder.stdout.read(READ_BYTES)
            read_time = time.monotonic()
            if not block:
                raise RuntimeError("Microphone stream ended; check the source name.")
            if capture_origin is None:
                capture_origin = read_time - len(block) / (RATE * 2)
            captured.extend(block)
            now = time.monotonic()
            while next_idx < len(schedule) and now >= test_mono + schedule[next_idx]["offset"]:
                if player.poll() is not None:
                    raise RuntimeError("Bluetooth playback stream stopped unexpectedly.")
                event = dict(schedule[next_idx])
                event["submit_mono"] = time.monotonic()
                event["submit_wall"] = dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
                player.stdin.write(pattern)
                player.stdin.flush()
                submitted.append(event)
                next_idx += 1
        if len(submitted) != total_trials:
            print(f"WARNING: submitted {len(submitted)} of {total_trials} trials.")

        # Analyze the complete continuous recording after all scheduled trials.
        raw = bytes(captured[:len(captured) - len(captured) % 2])
        audio = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        win = RATE * ENVELOPE_MS // 1000
        usable = len(audio) // win * win
        frames = audio[:usable].reshape(-1, win)
        envelope = np.sqrt(np.mean(frames * frames, axis=1))
        noise_floor = float(np.percentile(envelope, 25))
        envelope = np.maximum(envelope - noise_floor, 0.0)
        peak = float(np.percentile(envelope, 95))
        template = np.asarray(build_template(), dtype=np.float32)
        norm = max(float(np.linalg.norm(template)), 1e-6)
        if peak < 100:
            print("No clear microphone signal detected. Check mic placement and speaker volume.")
        else:
            envelope = np.clip(envelope / peak, 0.0, 1.5)
            for event in submitted:
                expected = int((event["submit_mono"] - capture_origin) * RATE / win)
                lo = max(0, expected - int(0.20 * RATE / win))
                hi = min(len(envelope), expected + int(2.0 * RATE / win))
                segment = envelope[lo:hi]
                if len(segment) < len(template):
                    event["latency_ms"] = None
                    continue
                scores = np.correlate(segment, template, mode="valid") / norm
                best = int(np.argmax(scores))
                score = float(scores[best])
                if score < 2.0:
                    event["latency_ms"] = None
                    event["score"] = score
                    continue
                detected_mono = capture_origin + ((lo + best) * win / RATE)
                event["detected_elapsed_ms"] = (detected_mono - test_mono) * 1000
                event["submit_elapsed_ms"] = (event["submit_mono"] - test_mono) * 1000
                event["latency_ms"] = (detected_mono - event["submit_mono"]) * 1000
                event["score"] = score
                event["detected_wall"] = (test_wall + dt.timedelta(milliseconds=event["detected_elapsed_ms"])).isoformat(timespec="milliseconds")

        print("\n=== Per-trial results ===")
        for e in submitted:
            if e.get("latency_ms") is None:
                print(f"Batch {e['batch']} trial {e['trial']:02d} (#{e['global']:02d}): not detected")
            else:
                print(f"Batch {e['batch']} trial {e['trial']:02d} (#{e['global']:02d}): "
                      f"submit +{e['submit_elapsed_ms']:.1f} ms | "
                      f"detected +{e['detected_elapsed_ms']:.1f} ms | "
                      f"latency {e['latency_ms']:.1f} ms | score {e['score']:.2f}")

        print("\n=== Batch summaries ===")
        for b in range(1, args.batches + 1):
            group = [e for e in submitted if e["batch"] == b]
            print(f"Batch {b} ({len(group)} trials):")
            print_stats("Latency", [e["latency_ms"] for e in group if e.get("latency_ms") is not None])
            print_stats("Submit timestamp from test start", [e["submit_elapsed_ms"] for e in group if "submit_elapsed_ms" in e])
            print_stats("Detected timestamp from test start", [e["detected_elapsed_ms"] for e in group if "detected_elapsed_ms" in e])

        print("\n=== Overall summary ===")
        valid = [e for e in submitted if e.get("latency_ms") is not None]
        print_stats("Latency", [e["latency_ms"] for e in valid])
        print_stats("Submit timestamp from test start", [e["submit_elapsed_ms"] for e in submitted if "submit_elapsed_ms" in e])
        print_stats("Detected timestamp from test start", [e["detected_elapsed_ms"] for e in valid])
        print(f"  Total test duration: {time.monotonic() - test_mono:.2f} s")
        print("\nTimestamp note: submit times are host-side write/flush times; detected times are estimated from the mic capture timeline.")
        print("Latency includes PipeWire, Bluetooth, speaker processing, acoustics, and microphone/capture timing uncertainty.")
        print("The streams are closed now, after all batches and analysis.")
    finally:
        stop_process(recorder)
        stop_process(player)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
