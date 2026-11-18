"""Run one live source and three independently scheduled Bluetooth listeners."""

import queue
import shutil
import threading
import time
import json
from pathlib import Path

import audio_source
import speaker
from config import (CHUNK_DURATION_S, QUEUE_CHUNKS, SPEAKERS, SYNC_START_DELAY_MS, CALIBRATION_FILE)
from scheduler import PeriodicOffsetScheduler


def main():
    required = ("pw-cat", "pactl", "bluetoothctl")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise SystemExit("Missing commands: " + ", ".join(missing))
    if not SPEAKERS:
        raise SystemExit("No speakers configured in config.py")

    # Use the latest acoustic PID calibration when available. The scheduler
    # needs relative speaker delays; any common microphone timestamp offset
    # cancels when it computes max_latency - each_latency.
    calibration_path = Path(CALIBRATION_FILE)
    if calibration_path.exists():
        try:
            calibration = json.loads(calibration_path.read_text())
            applied = 0
            for cfg in SPEAKERS:
                measured = calibration.get(cfg["name"], {}).get("latency_ms")
                if measured is not None and 100.0 <= float(measured) <= 2500.0:
                    cfg["measured_latency_ms"] = float(measured)
                    applied += 1
            print(f"[CALIBRATION] Loaded {applied}/{len(SPEAKERS)} speaker delays from {calibration_path}.", flush=True)
        except (OSError, ValueError, TypeError) as exc:
            print(f"[CALIBRATION] Could not read {calibration_path}: {exc}; using config.py baselines.", flush=True)
    else:
        print(f"[CALIBRATION] No {calibration_path} found; using baseline values in config.py. Run calibrate_sync.py first.", flush=True)

    scheduler = PeriodicOffsetScheduler(
        speakers=SPEAKERS,
        period_s=CHUNK_DURATION_S,
        start_delay_ms=SYNC_START_DELAY_MS,
    )
    scheduler.print_schedule()

    stop_event = threading.Event()
    playback_clock_started = threading.Event()
    speaker_queues = [
        queue.Queue(maxsize=QUEUE_CHUNKS) for _ in SPEAKERS
    ]

    # The source and every speaker worker must be ready before the common
    # transmission timeline is armed.
    start_barrier = threading.Barrier(1 + len(SPEAKERS))

    speaker_threads = []
    for cfg, audio_queue in zip(SPEAKERS, speaker_queues):
        thread = threading.Thread(
            name=f"speaker-{cfg['name']}",
            target=speaker.run,
            args=(
                audio_queue,
                stop_event,
                cfg,
                start_barrier,
                playback_clock_started,
                scheduler,
            ),
            daemon=True,
        )
        speaker_threads.append(thread)

    music_thread = threading.Thread(
        name="bluetooth-audio-source",
        target=audio_source.run,
        args=(
            speaker_queues,
            stop_event,
            start_barrier,
            playback_clock_started,
            scheduler,
        ),
        daemon=True,
    )

    music_thread.start()
    for thread in speaker_threads:
        thread.start()

    print(
        f"[MAIN] Running {1 + len(speaker_threads)} threads: "
        "one source + speaker listeners.",
        flush=True,
    )
    print(
        "[MAIN] Each speaker receives numbered chunks on a shared "
        "periodic timeline with a calculated transmission offset.",
        flush=True,
    )
    print("[MAIN] Ctrl+C stops all threads.", flush=True)

    try:
        while music_thread.is_alive() or any(
            thread.is_alive() for thread in speaker_threads
        ):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[MAIN] Stopping...", flush=True)
        stop_event.set()
        try:
            start_barrier.abort()
        except Exception:
            pass

        music_thread.join(timeout=4)
        for thread in speaker_threads:
            thread.join(timeout=4)


if __name__ == "__main__":
    main()
