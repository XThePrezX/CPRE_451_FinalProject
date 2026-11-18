"""Capture one Bluetooth source and fan numbered PCM chunks to speakers."""

import queue
import subprocess
import time

from config import SAMPLE_RATE, CHANNELS, CHUNK_BYTES, PREBUFFER_CHUNKS

ACTIVE_TRANSMITTER = "C28"
TRANSMITTERS = {"C28": "bluez_input.37_13_AA_27_AB_8F.2"}


def read_exact(stream, size):
    """Read exactly size bytes, or return a shorter buffer at end-of-stream."""
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            break
        data.extend(part)
    return bytes(data)


def fan_out(queues, seq, chunk, drop_counts):
    item = (seq, chunk)
    for index, audio_queue in enumerate(queues):
        try:
            audio_queue.put_nowait(item)
        except queue.Full:
            # Preserve the live edge: discard the oldest queued chunk.
            try:
                audio_queue.get_nowait()
                drop_counts[index] += 1
            except queue.Empty:
                pass
            try:
                audio_queue.put_nowait(item)
            except queue.Full:
                drop_counts[index] += 1


def run(audio_queues, stop_event, start_barrier, clock, scheduler):
    if ACTIVE_TRANSMITTER not in TRANSMITTERS:
        print("[MUSIC] Unknown transmitter selected.", flush=True)
        return

    source = TRANSMITTERS[ACTIVE_TRANSMITTER]
    print(f"[MUSIC] Selected transmitter: {ACTIVE_TRANSMITTER}", flush=True)
    print(f"[MUSIC] Capturing from: {source}", flush=True)

    # Keep sequence numbers increasing across pw-cat restarts. Resetting to zero
    # after reconnect would give chunks deadlines in the past.
    seq = 0
    drop_counts = [0 for _ in audio_queues]
    last_report = time.monotonic()
    last_report_drops = drop_counts.copy()

    while not stop_event.is_set():
        proc = None
        try:
            proc = subprocess.Popen(
                ["pw-cat", "--record", "--raw", "--target", source,
                 "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS),
                 "--format", "s16", "-"],
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=0,
            )

            while not stop_event.is_set():
                chunk = read_exact(proc.stdout, CHUNK_BYTES)
                if len(chunk) != CHUNK_BYTES:
                    raise RuntimeError(
                        f"Incomplete input chunk: {len(chunk)} of "
                        f"{CHUNK_BYTES} bytes; restarting capture."
                    )

                fan_out(audio_queues, seq, chunk, drop_counts)
                seq += 1

                if (not clock.is_set() and all(
                    q.qsize() >= PREBUFFER_CHUNKS for q in audio_queues
                )):
                    try:
                        start_barrier.wait(timeout=90)
                    except Exception as exc:
                        raise RuntimeError(f"Startup synchronization failed: {exc}")
                    scheduler.start()
                    clock.set()
                    print("[SCHEDULER] All speakers ready; shared timeline armed.", flush=True)

                now = time.monotonic()
                if now - last_report >= 10:
                    changes = [drop_counts[i] - last_report_drops[i]
                               for i in range(len(drop_counts))]
                    names = ", ".join(
                        f"{scheduler_names}: {count}"
                        for scheduler_names, count in zip(scheduler.latencies_ms, changes)
                    )
                    print(f"[SOURCE] Queue-overflow drops in last 10s: {names}", flush=True)
                    last_report_drops = drop_counts.copy()
                    last_report = now

        except FileNotFoundError:
            print("[MUSIC] pw-cat not found. Check PipeWire installation.", flush=True)
            return
        except Exception as exc:
            if not stop_event.is_set():
                print(f"[MUSIC] Capture error: {exc}", flush=True)
                stop_event.wait(2)
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
