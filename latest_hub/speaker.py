
import os
import subprocess
import threading
import time
from collections import deque

import config


def write_all(pipe, data):
    """Write every byte to the pw-cat pipe, handling short writes."""
    view = memoryview(data)
    total = 0

    while view:
        written = pipe.write(view)

        if written is None or written <= 0:
            raise BrokenPipeError(
                f"Incomplete audio write ({total}/{len(data)} bytes)"
            )

        total += written
        view = view[written:]

    return total


class SpeakerPlayer:
    def __init__(self, speaker, audio_queue, scheduler):
        self.speaker = speaker
        self.audio_queue = audio_queue
        self.scheduler = scheduler

        self.process = None
        self.stop_event = threading.Event()

        self.stats = {
            "chunks_written": 0,
            "write_errors": 0,
            "slow_writes": 0,
            "slow_write_max_ms": 0.0,
            "last_write_ms": 0.0,
        }

        self.stats_lock = threading.Lock()

    def start_player(self):
        command = [
            "pw-cat",
            "--playback",
            "--raw",
            "--target", self.speaker["controller"],
            "--rate", str(config.SAMPLE_RATE),
            "--channels", str(config.CHANNELS),
            "--format", "s16",
            "--latency", "250ms",
            "--verbose",
        ]

        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
            bufsize=0,
        )

        print(
            f"[{self.speaker['name']}] Started pw-cat "
            f"(PID {self.process.pid})"
        )

    def stop_player(self):
        if self.process is not None:
            try:
                if self.process.stdin:
                    self.process.stdin.close()

                self.process.terminate()
                self.process.wait(timeout=3)

            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

            self.process = None

    def get_stats(self):
        with self.stats_lock:
            return dict(self.stats)

    def run(self):
        print(f"[{self.speaker['name']}] Waiting for scheduled playback")

        try:
            self.start_player()

            while not self.stop_event.is_set():
                try:
                    chunk = self.audio_queue.get(timeout=0.5)
                except Exception:
                    continue

                if self.stop_event.is_set():
                    break

                target_time = self.scheduler.get_play_time(
                    self.speaker["name"],
                    chunk.timestamp
                )

                delay = target_time - time.monotonic()

                if delay > 0:
                    self.stop_event.wait(delay)

                if self.stop_event.is_set():
                    break

                if self.process is None or self.process.poll() is not None:
                    print(
                        f"[{self.speaker['name']}] "
                        "Playback process stopped; restarting"
                    )
                    self.stop_player()
                    self.start_player()

                if self.process.stdin is None:
                    raise BrokenPipeError("pw-cat stdin is unavailable")

                write_start = time.monotonic()

                try:
                    bytes_written = write_all(
                        self.process.stdin,
                        chunk.data
                    )

                    write_duration = time.monotonic() - write_start
                    write_ms = write_duration * 1000

                    with self.stats_lock:
                        self.stats["chunks_written"] += 1
                        self.stats["last_write_ms"] = write_ms

                        if write_ms > 100:
                            self.stats["slow_writes"] += 1

                        self.stats["slow_write_max_ms"] = max(
                            self.stats["slow_write_max_ms"],
                            write_ms
                        )

                    if bytes_written != len(chunk.data):
                        raise IOError(
                            f"Expected {len(chunk.data)} bytes, "
                            f"wrote {bytes_written}"
                        )

                except (BrokenPipeError, OSError, IOError) as error:
                    with self.stats_lock:
                        self.stats["write_errors"] += 1

                    print(
                        f"[{self.speaker['name']}] "
                        f"Playback write error: {error}"
                    )

                    self.stop_player()
                    time.sleep(0.25)

                    if not self.stop_event.is_set():
                        self.start_player()

        except Exception as error:
            print(f"[{self.speaker['name']}] Player error: {error}")

        finally:
            self.stop_player()
            print(f"[{self.speaker['name']}] Player stopped")

    def stop(self):
        self.stop_event.set()
        self.stop_player()
