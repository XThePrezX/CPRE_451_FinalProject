"""Periodic offset-based scheduler for synchronized Bluetooth playback.

Each audio chunk is transmitted at:
    T_transmit(i, k) = T0 + k * period + offset(i)

where:
    offset(i) = maximum_measured_latency - measured_latency(i)

This delays faster speakers so their audio is expected to play alongside
audio from the slowest speaker.
"""

import time


class PeriodicOffsetScheduler:
    def __init__(self, speakers, period_s, start_delay_ms=500):
        if not speakers:
            raise ValueError("At least one speaker must be configured.")
        if period_s <= 0:
            raise ValueError("The audio chunk period must be positive.")

        self.period_s = float(period_s)
        self.start_delay_s = float(start_delay_ms) / 1000.0

        latencies = {}
        for speaker in speakers:
            name = speaker["name"]
            if "measured_latency_ms" not in speaker:
                raise ValueError(
                    f"{name} is missing measured_latency_ms in config.py"
                )
            latency = float(speaker["measured_latency_ms"])
            if latency < 0:
                raise ValueError(f"{name} has a negative measured latency.")
            latencies[name] = latency

        self.max_latency_ms = max(latencies.values())
        self.latencies_ms = latencies
        self.offsets_s = {
            name: (self.max_latency_ms - latency) / 1000.0
            for name, latency in latencies.items()
        }
        self.transmission_epoch = None

    def start(self):
        """Arm one shared monotonic timeline for all speaker workers."""
        if self.transmission_epoch is not None:
            raise RuntimeError("The scheduler has already been started.")

        self.transmission_epoch = time.monotonic() + self.start_delay_s

    def transmission_deadline(self, speaker_name, sequence_number):
        """Return the absolute time to write a numbered chunk to a speaker."""
        if self.transmission_epoch is None:
            raise RuntimeError("The scheduler has not been started.")
        if speaker_name not in self.offsets_s:
            raise KeyError(f"No scheduling offset for speaker: {speaker_name}")

        return (
            self.transmission_epoch
            + int(sequence_number) * self.period_s
            + self.offsets_s[speaker_name]
        )

    def print_schedule(self):
        print(
            f"[SCHEDULER] Periodic offset scheduler: "
            f"period={self.period_s * 1000:.1f} ms, "
            f"maximum measured latency={self.max_latency_ms:.1f} ms",
            flush=True,
        )
        for name in self.latencies_ms:
            print(
                f"[SCHEDULER] {name}: "
                f"latency={self.latencies_ms[name]:.1f} ms, "
                f"transmission offset={self.offsets_s[name] * 1000:.1f} ms",
                flush=True,
            )
