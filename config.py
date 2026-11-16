"""Raspberry Pi Bluetooth audio hub configuration."""

SAMPLE_RATE = 48000
CHANNELS = 2
CHUNK_BYTES = 19200       # 100 ms, stereo, signed 16-bit PCM
QUEUE_CHUNKS = 30
PREBUFFER_CHUNKS = 4

# Give every Bluetooth playback stream time to become ready before the common
# playback clock starts. Increase if one speaker takes longer to initialize.
SYNC_START_DELAY_MS = 500

# Per-speaker compensation. If a speaker is heard 120 ms later than the
# others, set its sync_offset_ms to 120. Positive values make that stream
# receive audio earlier. Start at 0 and tune by listening/recording.
SPEAKERS = [
    {
        "name": "OontZ Angle",
        "device_mac": "73:39:96:C2:51:6F",
        "controller_mac": "AC:A7:F1:5B:B3:67",
        "sync_offset_ms": 113,
    },
    {
        "name": "AuraTown",
        "device_mac": "1B:2B:6E:7B:36:56",
        "controller_mac": "B8:27:EB:FB:3B:B2",
        "sync_offset_ms": 0,
    },
    {
        "name": "JBL Go 3",
        "device_mac": "18:90:67:08:4B:F0",
        "controller_mac": "AC:A7:F1:5B:B5:80",
        "sync_offset_ms": 74,
    },
]

CHUNK_DURATION_S = CHUNK_BYTES / (SAMPLE_RATE * CHANNELS * 2)
