"""Configuration for the Raspberry Pi Bluetooth audio hub."""

SAMPLE_RATE = 44100
CHANNELS = 2
CHUNK_BYTES = 21168  # 120 ms at 44.1 kHz, stereo, 16-bit PCM
QUEUE_CHUNKS = 5  # Maximum queued chunks per speaker (600 ms)
PREBUFFER_CHUNKS = 1  # Startup prebuffer (120 ms)


# Time between arming the scheduler and the first scheduled transmission.
SYNC_START_DELAY_MS = 0

# Measured end-to-end latency for each speaker.
# These are calibration measurements, not scheduler offsets.
# The scheduler calculates each transmission offset as:
#     offset_i = max_latency - measured_latency_i
SPEAKERS = [
    {
        "name": "OontZ Angle",
        "device_mac": "73:39:96:C2:51:6F",
        "controller_mac": "AC:A7:F1:5B:B3:67",
        "measured_latency_ms": 912,
    },
    {
        "name": "AuraTown",
        "device_mac": "1B:2B:6E:7B:36:56",
        "controller_mac": "B8:27:EB:FB:3B:B2",
        "measured_latency_ms": 1025,
    },
    {
        "name": "JBL Go 3",
        "device_mac": "18:90:67:08:4B:F0",
        "controller_mac": "AC:A7:F1:5B:B5:80",
        "measured_latency_ms": 951,
    },
]

CHUNK_DURATION_S = CHUNK_BYTES / (SAMPLE_RATE * CHANNELS * 2)
