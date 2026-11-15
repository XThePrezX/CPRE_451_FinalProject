THREE-SPEAKER RASPBERRY PI BLUETOOTH AUDIO HUB WITH PLAYBACK SCHEDULER

The hub captures the C28 Bluetooth audio stream once and fans each numbered
100 ms PCM chunk into three independent speaker queues.

THREADS
1. One source thread captures C28 audio.
2. One listener thread streams to OontZ Angle.
3. One listener thread streams to AuraTown.
4. One listener thread streams to JBL Go 3.

SCHEDULER
- Each chunk receives a sequence number.
- Each speaker starts its PipeWire playback stream and prebuffers before a
  startup barrier releases playback.
- All listeners calculate deadlines from the same monotonic playback epoch.
- Absolute deadlines avoid cumulative sleep drift.
- A listener that falls more than one chunk behind drops stale chunks rather
  than building an ever-growing delay.
- Each speaker has a configurable sync_offset_ms in config.py. Positive values
  cause that speaker's chunks to be submitted earlier to compensate for a
  slower Bluetooth path. Start at zero and tune using an audio recording.

IMPORTANT LIMITATION
This scheduler synchronizes when the Pi submits PCM to the three PipeWire
streams. It cannot directly control the Bluetooth codec, radio retransmissions,
internal speaker buffering, DAC, or acoustic output latency. Therefore exact
acoustic synchronization is not guaranteed. Use sync_offset_ms to compensate
for stable measured differences. If a speaker disconnects after startup, its
worker reconnects locally; perfect alignment may require restarting the hub. 

CONFIGURATION
Edit config.py. The C28 source is selected in audio_source.py.

RUN
  python3 run_hub.py

Stop with Ctrl+C.

Quit edit test
