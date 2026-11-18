# Bluetooth audio hub – dropout diagnostics update

This version preserves the current 44.1 kHz, stereo, signed 16-bit PCM format,
120 ms chunks, 5-chunk queues, 1-chunk prebuffer, speaker latency values, and
offset-based scheduler.

## Changes

- Bluetooth connection checks and reconnect attempts now run in a separate
  monitor thread per speaker. Slow `bluetoothctl`/`pactl` calls no longer block
  the thread that writes audio.
- Each speaker reports late chunk drops, queue-empty events, slow PipeWire
  writes, write errors, maximum write time, and queue depth every 10 seconds.
- The source reports queue-overflow drops per speaker every 10 seconds.
- Capture rejects partial PCM chunks rather than forwarding malformed data.
- Source sequence numbers continue across `pw-cat` capture restarts, instead
  of resetting to zero against an already-running scheduler timeline.
- Queue overflow catches `queue.Full` specifically and counts discarded chunks.

## Install

Back up the current project first. Copy these six Python files into the same
folder as your existing project files, replacing the old versions:

- `config.py`
- `audio_source.py`
- `speaker.py`
- `scheduler.py`
- `run_hub.py`

Then run:

```bash
python3 run_hub.py
```

Let it play for several minutes. Save the terminal output, especially any
`late_drops`, `queue_empty`, `slow_writes`, `write_errors`, and
`Queue-overflow drops` lines.

## Interpreting results

- `queue_empty` increasing: speaker worker is not receiving chunks quickly
  enough, or source/fan-out is interrupted.
- `late_drops` increasing: chunks reached the worker after their deadline.
- `slow_writes` or a high `max_write`: PipeWire is applying backpressure.
- `write_errors` or repeated playback stream restarts: playback process/stream
  is failing.
- `Queue-overflow drops` increasing: a speaker queue is filling faster than
  that speaker can consume it.

These are application-level diagnostics. They do not expose raw Bluetooth
radio packet loss. BlueZ/PipeWire handle the A2DP transport below this layer.
