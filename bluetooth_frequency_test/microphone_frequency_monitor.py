#!/usr/bin/env python3
"""
Continuously listen to a PipeWire microphone and print the strongest detected
frequency. Press Ctrl+C to stop.

Requirements:
    sudo apt install python3-numpy pipewire-pulse
"""

import math
import shutil
import subprocess
import sys
import time

import numpy as np

SAMPLE_RATE = 44100
CHANNELS = 1
WINDOW_SECONDS = 0.25
LOWEST_HZ = 100.0
HIGHEST_HZ = 16000.0
MIN_RMS_COUNTS = 3.0
FORMAT_BYTES = 2  # signed 16-bit PCM


def list_microphones():
    result = subprocess.run(
        ["pactl", "list", "short", "sources"],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("Could not list PipeWire sources: " + result.stderr.strip())

    sources = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            name = fields[1]
            # Skip monitor sources: these capture playback, not a physical mic.
            if ".monitor" not in name:
                sources.append((name, " ".join(fields[2:])))
    return sources


def choose_microphone():
    sources = list_microphones()
    if not sources:
        raise RuntimeError("No microphone sources found. Check: pactl list short sources")

    print("Available microphone inputs:")
    for i, (name, description) in enumerate(sources, 1):
        print(f"  {i}. {name} {description}")

    while True:
        choice = input("Select your USB microphone (number): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(sources):
            return sources[int(choice) - 1][0]
        print("Enter one of the listed numbers.")


def read_exact(stream, byte_count):
    """Read exactly byte_count bytes unless the stream ends."""
    data = bytearray()
    while len(data) < byte_count:
        block = stream.read(byte_count - len(data))
        if not block:
            break
        data.extend(block)
    return bytes(data)


def estimate_peak(samples):
    samples = samples.astype(np.float64)
    samples -= np.mean(samples)
    rms = float(np.sqrt(np.mean(samples * samples)))

    if rms < MIN_RMS_COUNTS:
        return None, rms

    window = np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(samples * window))
    frequencies = np.fft.rfftfreq(len(samples), d=1.0 / SAMPLE_RATE)

    valid = (frequencies >= LOWEST_HZ) & (frequencies <= HIGHEST_HZ)
    if not np.any(valid):
        return None, rms

    valid_indices = np.flatnonzero(valid)
    peak_index = valid_indices[int(np.argmax(spectrum[valid]))]

    # Parabolic interpolation gives a more precise estimate between FFT bins.
    peak_offset = 0.0
    if 0 < peak_index < len(spectrum) - 1:
        left = math.log(max(spectrum[peak_index - 1], 1e-12))
        center = math.log(max(spectrum[peak_index], 1e-12))
        right = math.log(max(spectrum[peak_index + 1], 1e-12))
        denominator = left - 2.0 * center + right
        if abs(denominator) > 1e-12:
            peak_offset = 0.5 * (left - right) / denominator
            peak_offset = max(-1.0, min(1.0, peak_offset))

    bin_hz = SAMPLE_RATE / len(samples)
    peak_hz = (peak_index + peak_offset) * bin_hz
    return peak_hz, rms


def main():
    for command in ("pw-cat", "pactl"):
        if shutil.which(command) is None:
            raise SystemExit(f"Missing '{command}'. Install PipeWire tools first.")

    window_samples = int(SAMPLE_RATE * WINDOW_SECONDS)
    bytes_per_window = window_samples * CHANNELS * FORMAT_BYTES
    mic_source = choose_microphone()

    print(f"\nListening to: {mic_source}")
    print(f"Analysis window: {WINDOW_SECONDS:.2f}s | Search range: "
          f"{LOWEST_HZ:.0f}-{HIGHEST_HZ:.0f} Hz")
    print("Keep the microphone near the sound source. Press Ctrl+C to stop.\n")

    command = [
        "pw-cat", "--record", "--raw", "--target", mic_source,
        "--rate", str(SAMPLE_RATE),
        "--channels", str(CHANNELS),
        "--format", "s16", "-"
    ]

    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=None, bufsize=0
    )

    try:
        print("Waiting for microphone audio...")
        while True:
            raw = read_exact(process.stdout, bytes_per_window)
            if len(raw) != bytes_per_window:
                if process.poll() is not None:
                    raise RuntimeError("Microphone capture process stopped.")
                if not raw:
                    raise RuntimeError("No audio data received from microphone.")
                continue

            samples = np.frombuffer(raw, dtype="<i2")
            peak_hz, rms = estimate_peak(samples)

            if peak_hz is None:
                print(f"\rNo clear tone detected (RMS {rms:.1f})" + " " * 12, end="", flush=True)
            else:
                level_dbfs = 20.0 * math.log10(max(rms / 32768.0, 1e-9))
                print(
                    f"\rStrongest frequency: {peak_hz:8.1f} Hz"
                    f" | Level: {level_dbfs:6.1f} dBFS"
                    f" | RMS: {rms:7.1f}   ",
                    end="", flush=True
                )

    except KeyboardInterrupt:
        print("\nStopping microphone monitor...")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
