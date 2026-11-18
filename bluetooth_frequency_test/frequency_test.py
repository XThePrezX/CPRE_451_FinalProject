#!/usr/bin/env python3
"""
Play a rising sine sweep through one configured Bluetooth speaker while
recording the Raspberry Pi's default microphone input.

Requirements:
  - PipeWire tools: pw-cat, pactl
  - numpy: sudo apt install python3-numpy

Run from the Bluetooth_Hub project directory:
  python3 frequency_test.py

The script does NOT use or modify the live hub scheduler.
"""
import csv
import math
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from config import SAMPLE_RATE, SPEAKERS

MIC_RATE = 44100
MIC_CHANNELS = 1
START_HZ = 500.0
END_HZ = 18000.0
SWEEP_SECONDS = 30.0
TAIL_SECONDS = 3.0
AMPLITUDE = 0.08          # Conservative digital level; adjust only cautiously.
WRITE_CHUNK_SECONDS = 0.10
ANALYSIS_WINDOW_SECONDS = 0.25
OUTPUT_DIR = Path("frequency_test_results")


def require_commands():
    missing = [cmd for cmd in ("pw-cat", "pactl") if shutil.which(cmd) is None]
    if missing:
        raise SystemExit("Missing required command(s): " + ", ".join(missing))


def find_sink(cfg):
    prefix = "bluez_output." + cfg["device_mac"].replace(":", "_")
    result = subprocess.run(
        ["pactl", "list", "short", "sinks"],
        capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("pactl could not list sinks: " + result.stderr.strip())
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].startswith(prefix):
            return fields[1]
    raise RuntimeError(
        f"No PipeWire/PulseAudio sink found for {cfg['name']} ({prefix}...). "
        "Connect the speaker, then check: pactl list short sinks"
    )


def choose_speaker():
    print("Configured Bluetooth speakers:")
    for i, cfg in enumerate(SPEAKERS, 1):
        print(f"  {i}. {cfg['name']}  ({cfg['device_mac']})")
    while True:
        choice = input("Select speaker number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(SPEAKERS):
            return SPEAKERS[int(choice) - 1]
        print("Enter one of the listed numbers.")


def make_sweep():
    """Generate a phase-continuous logarithmic chirp."""
    total = int(round(SAMPLE_RATE * SWEEP_SECONDS))
    t = np.arange(total, dtype=np.float64) / SAMPLE_RATE
    log_ratio = math.log(END_HZ / START_HZ)
    phase = (2.0 * math.pi * START_HZ * SWEEP_SECONDS / log_ratio) * (
        np.exp(log_ratio * t / SWEEP_SECONDS) - 1.0
    )
    # Brief fade-in/out avoids clicks at the boundaries.
    fade_n = int(0.05 * SAMPLE_RATE)
    envelope = np.ones(total, dtype=np.float64)
    ramp = np.linspace(0.0, 1.0, fade_n, endpoint=True)
    envelope[:fade_n] = ramp
    envelope[-fade_n:] = ramp[::-1]
    samples = np.clip(AMPLITUDE * envelope * np.sin(phase), -1.0, 1.0)
    return (samples * 32767).astype("<i2")


def capture_microphone(stop_event, chunks, errors):
    cmd = [
        "pw-cat", "--record", "--raw",
        "--rate", str(MIC_RATE), "--channels", str(MIC_CHANNELS),
        "--format", "s16", "-"
    ]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None, bufsize=0)
        while not stop_event.is_set():
            block = proc.stdout.read(8192)
            if not block:
                if not stop_event.is_set():
                    errors.append("Microphone capture stream ended.")
                break
            chunks.append(block)
    except Exception as exc:
        errors.append(str(exc))
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def analyze(recording):
    """Estimate strongest tone in each short window and compare with sweep."""
    window_n = int(ANALYSIS_WINDOW_SECONDS * MIC_RATE)
    if len(recording) < window_n:
        raise RuntimeError("Microphone recording was too short to analyze.")

    # Remove DC and use a Hann window to reduce FFT leakage.
    rows = []
    nyquist = MIC_RATE / 2
    for start in range(0, len(recording) - window_n + 1, window_n):
        segment = recording[start:start + window_n].astype(np.float64)
        segment -= np.mean(segment)
        rms = float(np.sqrt(np.mean(segment * segment)))
        if rms < 1.0:
            peak_hz, peak_dbfs = 0.0, -120.0
        else:
            spectrum = np.abs(np.fft.rfft(segment * np.hanning(window_n)))
            freqs = np.fft.rfftfreq(window_n, 1.0 / MIC_RATE)
            # Ignore rumble and frequencies above Nyquist.
            valid = (freqs >= 300) & (freqs <= min(END_HZ + 500, nyquist))
            idx = np.argmax(spectrum[valid])
            peak_hz = float(freqs[valid][idx])
            peak_dbfs = 20.0 * math.log10(max(rms / 32768.0, 1e-9))
        elapsed = start / MIC_RATE
        # Approximate chirp frequency at this recording time. Bluetooth adds
        # latency, so this is a guide, not a precise calibration.
        sweep_t = min(max(elapsed - 1.0, 0.0), SWEEP_SECONDS)
        expected_hz = START_HZ * (END_HZ / START_HZ) ** (sweep_t / SWEEP_SECONDS)
        rows.append((elapsed, expected_hz, peak_hz, rms, peak_dbfs))
    return rows


def main():
    require_commands()
    speaker = choose_speaker()

    print("\nSAFETY: Keep the speaker volume low. This sweep includes very high")
    print("frequencies that may be uncomfortable or inaudible, and some animals")
    print("can hear frequencies humans cannot. Stop if anything sounds uncomfortable.")
    confirm = input(f"\nReady to play a {START_HZ:.0f}-{END_HZ:.0f} Hz sweep on "
                    f"{speaker['name']}? Type YES: ").strip()
    if confirm != "YES":
        print("Cancelled.")
        return

    sink = find_sink(speaker)
    print(f"[TEST] Speaker: {speaker['name']}")
    print(f"[TEST] Playback sink: {sink}")
    print("[TEST] Microphone: PipeWire default input source")
    print("[TEST] Starting microphone capture...")

    stop_capture = threading.Event()
    mic_chunks = []
    mic_errors = []
    mic_thread = threading.Thread(
        target=capture_microphone,
        args=(stop_capture, mic_chunks, mic_errors),
        daemon=True
    )
    mic_thread.start()
    time.sleep(1.0)

    playback = None
    try:
        playback = subprocess.Popen(
            [
                "pw-cat", "--playback", "--raw", "--target", sink,
                "--rate", str(SAMPLE_RATE), "--channels", "1",
                "--format", "s16", "-"
            ],
            stdin=subprocess.PIPE, stderr=None, bufsize=0
        )
        sweep = make_sweep()
        bytes_per_chunk = int(SAMPLE_RATE * WRITE_CHUNK_SECONDS) * 2
        print(f"[TEST] Playing rising sweep for {SWEEP_SECONDS:.0f} seconds...")
        for pos in range(0, len(sweep.tobytes()), bytes_per_chunk):
            block = sweep.tobytes()[pos:pos + bytes_per_chunk]
            if not block:
                break
            playback.stdin.write(block)
        playback.stdin.flush()
        print(f"[TEST] Sweep complete; recording {TAIL_SECONDS:.0f}s more for speaker delay.")
        time.sleep(TAIL_SECONDS)
    except BrokenPipeError:
        print("[TEST] Playback stream closed unexpectedly.")
    finally:
        if playback is not None:
            try:
                playback.stdin.close()
            except Exception:
                pass
            if playback.poll() is None:
                playback.terminate()
            try:
                playback.wait(timeout=2)
            except subprocess.TimeoutExpired:
                playback.kill()
        time.sleep(0.2)
        stop_capture.set()
        mic_thread.join(timeout=3)

    if mic_errors:
        print("[MIC] Capture warning:", "; ".join(mic_errors))
    if not mic_chunks:
        raise SystemExit(
            "No microphone audio captured. Check that a microphone is connected "
            "and visible in: pw-record --list-targets"
        )

    raw = b"".join(mic_chunks)
    recording = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2")
    rows = analyze(recording)

    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"{speaker['name'].replace(' ', '_')}_{stamp}.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["recording_time_s", "approx_expected_sweep_hz",
                         "microphone_peak_hz", "microphone_rms_counts",
                         "microphone_level_dbfs"])
        writer.writerows(rows)

    detected = [r for r in rows if r[2] > 0 and r[3] >= 2.0]
    print("\n[RESULTS]")
    print(f"Captured {len(recording) / MIC_RATE:.1f} seconds from microphone.")
    print(f"Analyzed {len(rows)} windows.")
    if detected:
        print("Microphone detected measurable audio in the recorded signal.")
        print("Sample windows (recording time, expected sweep, strongest mic frequency):")
        stride = max(1, len(detected) // 10)
        for row in detected[::stride][:10]:
            print(f"  {row[0]:5.2f}s | expected ~{row[1]:7.0f} Hz | "
                  f"mic peak {row[2]:7.0f} Hz | RMS {row[3]:.1f}")
    else:
        print("No clear signal above the very-low-level threshold was found.")
        print("Check microphone selection, gain, speaker volume, and room placement.")
    print(f"\nFull analysis saved to: {csv_path}")
    print("Note: This is a basic acoustic response test, not a calibrated hearing test.")
    print("The strongest FFT peak can be room noise or harmonics; inspect the CSV "
          "and repeat in a quiet room if results look inconsistent.")


if __name__ == "__main__":
    main()
