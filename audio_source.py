"""Capture C28 once and fan out numbered PCM chunks to all speakers."""
import subprocess
import time

from config import SAMPLE_RATE, CHANNELS, CHUNK_BYTES, PREBUFFER_CHUNKS, SYNC_START_DELAY_MS

ACTIVE_TRANSMITTER = "C28"
TRANSMITTERS = {
    "C28": "bluez_input.37_13_AA_27_AB_8F.2",
}


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            break
        data.extend(part)
    return bytes(data)


def fan_out(queues, seq, chunk):
    item = (seq, chunk)
    for audio_queue in queues:
        try:
            audio_queue.put_nowait(item)
        except Exception:
            # Keep each listener near the live edge without blocking capture.
            try:
                audio_queue.get_nowait()
            except Exception:
                pass
            try:
                audio_queue.put_nowait(item)
            except Exception:
                pass


def run(audio_queues, stop_event, start_barrier, clock, playback_epoch):
    if ACTIVE_TRANSMITTER not in TRANSMITTERS:
        print("[MUSIC] Unknown transmitter selected.", flush=True)
        return

    source = TRANSMITTERS[ACTIVE_TRANSMITTER]
    print(f"[MUSIC] Selected transmitter: {ACTIVE_TRANSMITTER}", flush=True)
    print(f"[MUSIC] Capturing from: {source}", flush=True)

    while not stop_event.is_set():
        proc = None
        try:
            proc = subprocess.Popen(
                ["pw-cat", "--record", "--raw", "--target", source,
                 "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS),
                 "--format", "s16", "-"],
                stdout=subprocess.PIPE, stderr=None, bufsize=0,
            )
            #print(f"[MUSIC] Receiving live audio from {ACTIVE_TRANSMITTER}.", flush=True)
            seq = 0
            while not stop_event.is_set():
                chunk = read_exact(proc.stdout, CHUNK_BYTES)
                if not chunk:
                    raise RuntimeError("PipeWire input ended or disconnected.")
                fan_out(audio_queues, seq, chunk)
                seq += 1

                # Do not release the common start barrier until every speaker
                # has enough audio buffered to start together.
                if not clock.is_set() and all(q.qsize() >= PREBUFFER_CHUNKS for q in audio_queues):
                    try:
                        start_barrier.wait(timeout=90)
                    except Exception as exc:
                        raise RuntimeError(f"Could not synchronize speaker startup: {exc}")
                    playback_epoch["value"] = time.monotonic() + SYNC_START_DELAY_MS / 1000.0
                    clock.set()
                    print("[SCHEDULER] All speakers ready; common playback clock started.", flush=True)

        except FileNotFoundError:
            print("[MUSIC] pw-cat not found. Check PipeWire installation.", flush=True)
            return
        except Exception as exc:
            if not stop_event.is_set():
                print(f"[MUSIC] Capture error: {exc}", flush=True)
                time.sleep(2)
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
