# Synchronized Bluetooth Audio Hub

## What this version does

- Captures the selected Bluetooth receiver source once (`C28` in `audio_source.py`).
- Fans numbered 44.1 kHz stereo, signed 16-bit PCM chunks to three speaker queues.
- Runs an acoustic calibration first using the USB microphone and distinct tones:
  AuraTown 9 kHz, OontZ 8 kHz, JBL 15 kHz.
- Uses bounded, conservative PID adjustments (maximum 5 ms per update) and saves
  the resulting per-speaker acoustic lead estimates to `calibration.json`.
- Loads those estimates into the shared-clock offset scheduler before streaming
  receiver audio.
- Uses 12 chunks per speaker queue (1.44 s) and a 4-chunk (480 ms) startup
  prebuffer, plus 250 ms PipeWire playback buffering.
- Keeps connection monitoring outside the audio-writing loop and reports queue
  drops, late chunks, queue-empty events, slow writes, and write errors.

## Run

Connect all three speakers and make sure the Bluetooth receiver is connected.
From this directory:

```bash
chmod +x start_system.sh
./start_system.sh
```

The calibration runs for about 60 seconds, then the live receiver audio starts.
To run only the live hub after a calibration has been saved:

```bash
python3 run_hub.py
```

To recalibrate:

```bash
python3 calibrate_sync.py
```

## Important limitations

- Calibration uses the acoustic microphone timing experiment. Its absolute timing
  includes the USB microphone/PipeWire capture-path offset. A common offset is
  mostly canceled by the scheduler because it uses relative speaker delays.
- The calibration PID is deliberately slow and bounded. Do not interpret the
  saved numbers as laboratory-grade absolute Bluetooth latency.
- The hub cannot guarantee zero audible interruptions. Bluetooth A2DP packet
  handling and radio retransmission are below this application's PCM layer.
  The larger queues and PipeWire buffering absorb short Pi scheduling and source
  jitter; they cannot fix a prolonged RF outage, disconnect, or a speaker that
  stops consuming audio.
- `Queue-overflow drops` are application queue drops, not raw Bluetooth packet
  loss. BlueZ/PipeWire do not expose A2DP RF packet loss through these scripts.
- If `queue_empty`, `late_drops`, `write_errors`, or overflow drops rise
  continuously, save the terminal log; those indicate where to tune next.

## Current receiver

`audio_source.py` selects transmitter `C28` and source
`bluez_input.37_13_AA_27_AB_8F.2`. Change `ACTIVE_TRANSMITTER` and `TRANSMITTERS`
there if your receiver/source changes.
