"""Run one live source and three independently scheduled Bluetooth listeners."""
import queue
import shutil
import threading
import time

import audio_source
import speaker
from config import QUEUE_CHUNKS, SPEAKERS


def main():
    required = ("pw-cat", "pactl", "bluetoothctl")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise SystemExit("Missing commands: " + ", ".join(missing))
    if not SPEAKERS:
        raise SystemExit("No speakers configured in config.py")

    stop_event = threading.Event()
    playback_clock_started = threading.Event()
    shared_playback_epoch = {"value": None}
    speaker_queues = [queue.Queue(maxsize=QUEUE_CHUNKS) for _ in SPEAKERS]
    # One source + one participant for each speaker. The barrier opens only
    # after every playback process is running and each queue has prebuffered.
    start_barrier = threading.Barrier(1 + len(SPEAKERS))

    speaker_threads = []
    for cfg, audio_queue in zip(SPEAKERS, speaker_queues):
        thread = threading.Thread(
            name=f"speaker-{cfg['name']}", target=speaker.run,
            args=(audio_queue, stop_event, cfg, start_barrier, playback_clock_started, shared_playback_epoch),
            daemon=True,
        )
        speaker_threads.append(thread)

    music_thread = threading.Thread(
        name="bluetooth-audio-source", target=audio_source.run,
        args=(speaker_queues, stop_event, start_barrier, playback_clock_started, shared_playback_epoch),
        daemon=True,
    )

    music_thread.start()
    for thread in speaker_threads:
        thread.start()

    print(f"[MAIN] Running {1 + len(speaker_threads)} threads: source + speaker listeners.", flush=True)
    print("[MAIN] Scheduler uses numbered 100 ms chunks and a shared monotonic playback timeline.", flush=True)
    print("[MAIN] All speaker streams must be ready before synchronized playback begins.", flush=True)
    print("[MAIN] Ctrl+C stops all threads.", flush=True)

    try:
        while music_thread.is_alive() or any(t.is_alive() for t in speaker_threads):
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
