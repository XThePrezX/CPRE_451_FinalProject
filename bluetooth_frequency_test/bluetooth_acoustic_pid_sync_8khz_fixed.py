#!/usr/bin/env python3
"""
Closed-loop Bluetooth speaker timing experiment.

Each 2-second cycle:
  0.000-0.333 s: AuraTown, 9 kHz
  0.333-0.666 s: OontZ Angle, 8 kHz
  0.666-1.000 s: JBL Go 3, 15 kHz
  1.000-2.000 s: silence

Three persistent PipeWire playback streams are scheduled against a shared
monotonic clock. A USB microphone listens continuously. When it detects each
speaker's unique tone, a conservative per-speaker PID controller adjusts that
speaker's estimated acoustic lead time for subsequent cycles.

This is an experimental timing controller, not a calibrated measurement
instrument. Use low speaker volume.
"""

import math
import shutil
import subprocess
import sys
import threading
import time

import numpy as np


# ------------------------- Audio configuration -------------------------

PLAY_RATE = 44100
MIC_RATE = 48000
CHANNELS = 1
SAMPLE_FORMAT = "s16"
PLAY_BLOCK_SECONDS = 0.010       # 10 ms output blocks
MIC_BLOCK_SECONDS = 0.010        # 10 ms microphone analysis frames
AMPLITUDE = 0.035                # Conservative digital amplitude
CYCLE_SECONDS = 2.0
SLOT_SECONDS = 1.0 / 3.0
REST_SECONDS = 1.0

MIC_SOURCE = (
    "alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-mono"
)

# MAC addresses are the Bluetooth device MACs from the project configuration.
# Initial latencies use the user's earlier measured acoustic-latency estimates.
SPEAKERS = [
    {
        "name": "AuraTown",
        "mac": "1B:2B:6E:7B:36:56",
        "frequency": 9000.0,
        "slot": 0.0,
        "initial_latency": 1.025,
    },
    {
        "name": "OontZ Angle",
        "mac": "73:39:96:C2:51:6F",
        "frequency": 8000.0,
        "slot": SLOT_SECONDS,
        "initial_latency": 0.912,
    },
    {
        "name": "JBL Go 3",
        "mac": "18:90:67:08:4B:F0",
        "frequency": 15000.0,
        "slot": 2.0 * SLOT_SECONDS,
        "initial_latency": 0.951,
    },
]

# PID gains: intentionally conservative for a noisy acoustic feedback signal.
# The controller adjusts the estimated lead time (seconds).
KP = 0.35
KI = 0.015
KD = 0.02
MAX_LATENCY = 2.5
MIN_LATENCY = 0.10
MAX_INTEGRAL = 0.50

# A tone must exceed this estimated sine amplitude for consecutive windows.
# Increase this if room noise causes false detections; decrease cautiously if
# the microphone is not detecting the speaker.
DETECTION_THRESHOLD_COUNTS = 22.0
DETECTION_CONFIRM_WINDOWS = 2


def require_commands():
    missing = [x for x in ("pw-cat", "pactl") if shutil.which(x) is None]
    if missing:
        raise RuntimeError("Missing command(s): " + ", ".join(missing))


def find_sink(mac):
    prefix = "bluez_output." + mac.replace(":", "_")
    result = subprocess.run(
        ["pactl", "list", "short", "sinks"],
        capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError("Could not list audio sinks: " + result.stderr.strip())

    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].startswith(prefix):
            return fields[1]
    raise RuntimeError(
        f"Could not find sink for {mac}. Connect the speaker and run "
        "'pactl list short sinks' to check its sink name."
    )


def verify_mic_source():
    result = subprocess.run(
        ["pactl", "list", "short", "sources"],
        capture_output=True, text=True, check=False, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError("Could not list microphone sources: " + result.stderr.strip())
    names = [line.split()[1] for line in result.stdout.splitlines() if len(line.split()) >= 2]
    if MIC_SOURCE not in names:
        raise RuntimeError(
            "Configured USB microphone source was not found.\n"
            f"Expected: {MIC_SOURCE}\n"
            "Available sources:\n  " + "\n  ".join(names)
        )


def make_tone_block(frequency, start_phase_seconds, sample_count):
    # Absolute-time phase makes successive blocks phase-continuous.
    t = start_phase_seconds + np.arange(sample_count, dtype=np.float64) / PLAY_RATE
    samples = AMPLITUDE * np.sin(2.0 * math.pi * frequency * t)
    return np.clip(samples * 32767.0, -32768, 32767).astype("<i2").tobytes()


def speaker_worker(speaker, sink, epoch, stop_event, latency_state, latency_lock):
    block_samples = int(round(PLAY_RATE * PLAY_BLOCK_SECONDS))
    block_duration = block_samples / PLAY_RATE
    command = [
        "pw-cat", "--playback", "--raw", "--target", sink,
        "--rate", str(PLAY_RATE), "--channels", str(CHANNELS),
        "--format", SAMPLE_FORMAT, "-"
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0)
    print(f"[PLAYBACK] {speaker['name']} -> {sink}")

    next_deadline = time.monotonic()
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if now < next_deadline:
                time.sleep(next_deadline - now)
            now = time.monotonic()

            with latency_lock:
                lead = latency_state["latency"]

            # Estimate when this block will be heard acoustically.
            acoustic_time = now + lead
            # Prevent modulo arithmetic from scheduling a tone before epoch.
            if acoustic_time < epoch:
                cycle_phase = None
            else:
                cycle_phase = (acoustic_time - epoch) % CYCLE_SECONDS

            slot_start = speaker["slot"]
            slot_end = slot_start + SLOT_SECONDS

            if cycle_phase is not None and slot_start <= cycle_phase < slot_end:
                tone_elapsed = cycle_phase - slot_start
                block = make_tone_block(
                    speaker["frequency"], tone_elapsed, block_samples
                )
            else:
                block = b"\x00\x00" * block_samples

            try:
                process.stdin.write(block)
            except (BrokenPipeError, OSError):
                if not stop_event.is_set():
                    print(f"\n[ERROR] Playback pipe closed for {speaker['name']}")
                break

            next_deadline += block_duration
            # If the process was delayed, skip missed wall-clock deadlines
            # rather than trying to catch up with a burst of audio writes.
            if next_deadline < time.monotonic() - block_duration:
                next_deadline = time.monotonic() + block_duration
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def estimate_tone_amplitude(samples, frequency):
    """Estimate amplitude at one known frequency using sine/cosine correlation."""
    x = samples.astype(np.float64)
    x -= np.mean(x)
    n = len(x)
    indices = np.arange(n, dtype=np.float64)
    phase = 2.0 * math.pi * frequency * indices / MIC_RATE
    window = np.hanning(n)
    weighted = x * window
    scale = 2.0 / np.sum(window)
    real = np.sum(weighted * np.cos(phase))
    imag = np.sum(weighted * np.sin(phase))
    return scale * math.hypot(real, imag)


def update_pid(state, measured_error):
    """Update estimated acoustic lead time; late detection increases lead."""
    now = time.monotonic()
    dt = max(now - state["last_update"], 0.05) if state["last_update"] else 1.0

    state["integral"] += measured_error * dt
    state["integral"] = max(-MAX_INTEGRAL, min(MAX_INTEGRAL, state["integral"]))

    derivative = (
        (measured_error - state["last_error"]) / dt
        if state["last_update"] else 0.0
    )
    adjustment = (
        KP * measured_error
        + KI * state["integral"]
        + KD * derivative
    )

    with state["lock"]:
        old_latency = state["latency"]
        state["latency"] = max(
            MIN_LATENCY, min(MAX_LATENCY, old_latency + adjustment)
        )
        new_latency = state["latency"]

    state["last_error"] = measured_error
    state["last_update"] = now
    return old_latency, new_latency


def microphone_worker(epoch, stop_event, states, states_by_name):
    # 10 ms frames provide finer onset timing than the previous 20 ms blocks.
    frame_samples = int(round(MIC_RATE * 0.010))
    bytes_per_frame = frame_samples * 2
    command = [
        "pw-cat", "--record", "--raw", "--target", MIC_SOURCE,
        "--rate", str(MIC_RATE), "--channels", "1", "--format", "s16", "-"
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=0)
    print(f"[MIC] Listening to {MIC_SOURCE} at {MIC_RATE} Hz")

    above_counts = {s["name"]: 0 for s in states}
    first_above_time = {s["name"]: None for s in states}
    accepted_cycles = {s["name"]: set() for s in states}

    # Anchor sample zero once, then derive all frame timestamps from the
    # cumulative sample count. This avoids using Python read-completion time
    # for every detection. The initial PipeWire/driver buffering offset remains
    # an uncertainty, so this is improved timing, not calibrated instrumentation.
    stream_time_zero = None
    sample_index = 0

    try:
        while not stop_event.is_set():
            raw = process.stdout.read(bytes_per_frame)
            if len(raw) != bytes_per_frame:
                if stop_event.is_set():
                    break
                if not raw:
                    print("\\n[MIC ERROR] Microphone stream ended.")
                    break
                continue

            read_time = time.monotonic()
            if stream_time_zero is None:
                stream_time_zero = read_time - frame_samples / MIC_RATE

            frame_start_time = stream_time_zero + sample_index / MIC_RATE
            sample_index += frame_samples
            audio = np.frombuffer(raw, dtype="<i2")

            for speaker in states:
                name = speaker["name"]
                amplitude = estimate_tone_amplitude(audio, speaker["frequency"])

                if amplitude >= DETECTION_THRESHOLD_COUNTS:
                    if above_counts[name] == 0:
                        first_above_time[name] = frame_start_time
                    above_counts[name] += 1

                    if above_counts[name] >= DETECTION_CONFIRM_WINDOWS:
                        detected_time = first_above_time[name]
                        expected_slot = speaker["slot"]
                        cycle_number = round(
                            (detected_time - epoch - expected_slot) / CYCLE_SECONDS
                        )
                        target_time = (
                            epoch + cycle_number * CYCLE_SECONDS + expected_slot
                        )
                        error = detected_time - target_time

                        # Only accept detections belonging to a real cycle and
                        # reasonably close to that speaker's expected slot.
                        valid_cycle = (
                            cycle_number >= 0
                            and target_time >= epoch
                            and abs(error) <= SLOT_SECONDS * 0.5
                        )

                        if (
                            valid_cycle
                            and cycle_number not in accepted_cycles[name]
                        ):
                            old, new = update_pid(states_by_name[name], error)
                            accepted_cycles[name].add(cycle_number)
                            print(
                                f"\\n[DETECT] {name}: {speaker['frequency']:.0f} Hz"
                                f" | detected={detected_time - epoch:+.3f}s"
                                f" | target={target_time - epoch:+.3f}s"
                                f" | timing error={error * 1000:+.1f} ms"
                                f" | lead {old * 1000:.1f} -> {new * 1000:.1f} ms"
                                f" | mic amplitude={amplitude:.1f}"
                            )

                        # Require the tone to disappear before re-arming.
                        above_counts[name] = 0
                        first_above_time[name] = None
                else:
                    above_counts[name] = 0
                    first_above_time[name] = None
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    require_commands()
    verify_mic_source()

    print("Bluetooth acoustic timing experiment")
    print("------------------------------------")
    print("The speakers will run continuously until Ctrl+C.")
    print("Keep volume low. Use a quiet room and keep the USB mic stationary.")
    print("This is a timing experiment, not a calibrated SPL/frequency-response test.\n")

    for speaker in SPEAKERS:
        speaker["sink"] = find_sink(speaker["mac"])
        print(
            f"{speaker['name']}: {speaker['frequency']:.0f} Hz, "
            f"slot={speaker['slot']:.3f}s, "
            f"initial lead={speaker['initial_latency'] * 1000:.0f} ms"
        )

    states = []
    states_by_name = {}
    for speaker in SPEAKERS:
        state = {
            "latency": speaker["initial_latency"],
            "integral": 0.0,
            "last_error": 0.0,
            "last_update": None,
            "lock": threading.Lock(),
        }
        states.append(speaker)
        states_by_name[speaker["name"]] = state

    stop_event = threading.Event()
    threads = []

    # Give all persistent playback streams time to start before the first
    # intended acoustic event. The lead values are around one second.
    epoch = time.monotonic() + 3.0

    mic_thread = threading.Thread(
        target=microphone_worker,
        args=(epoch, stop_event, states, states_by_name),
        daemon=True,
    )
    mic_thread.start()

    for speaker in SPEAKERS:
        thread = threading.Thread(
            target=speaker_worker,
            args=(
                speaker, speaker["sink"], epoch, stop_event,
                states_by_name[speaker["name"]], states_by_name[speaker["name"]]["lock"]
            ),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    print(f"\n[START] First intended acoustic cycle begins in about 3 seconds.")
    print("[START] Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1.0)
            status = " | ".join(
                f"{s['name']} lead={states_by_name[s['name']]['latency'] * 1000:.0f}ms"
                for s in SPEAKERS
            )
            print(f"\r[PID] {status}   ", end="", flush=True)
    except KeyboardInterrupt:
        print("\n\nStopping experiment...")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3)
        mic_thread.join(timeout=3)
        print("Stopped.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
