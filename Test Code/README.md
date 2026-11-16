# Raspberry Pi Bluetooth Audio Synchronization Scheduler

## Files
- `config.py`: audio format, queue settings, speaker identities, measured latencies.
- `scheduler.py`: calculates per-speaker offsets and absolute transmission deadlines.
- `run_hub.py`: creates the shared scheduler and starts the source/speaker threads.
- `audio_source.py`: captures audio once and fans numbered chunks to all listeners.
- `speaker.py`: waits until each chunk's scheduled transmission time and writes it to PipeWire.

## Scheduling model
For each speaker i:

    offset_i = max(measured_latency) - measured_latency_i
    transmit_time(i, chunk_k) = T0 + k * chunk_period + offset_i

The expected playback time is approximately:

    transmit_time(i, k) + measured_latency_i
      = T0 + k * chunk_period + max(measured_latency)

With the current example measurements:
- OontZ: 912 ms latency -> 113 ms transmission offset
- JBL: 951 ms latency -> 74 ms transmission offset
- AuraTown: 1025 ms latency -> 0 ms transmission offset

## Important
The measured latency values must be measured under the same audio format,
chunk size, PipeWire configuration, and speaker operating conditions used for
playback. Re-measure them if those conditions change.

This is an offset-based periodic scheduler, not Rate Monotonic Scheduling.
It schedules when each chunk is written to each PipeWire playback stream.
It cannot guarantee exact acoustic synchronization because Bluetooth, PipeWire,
speaker buffering, and operating-system scheduling introduce variable delay.
Use microphone/loopback measurements to evaluate actual playback skew.

Run with:

    python3 run_hub.py
