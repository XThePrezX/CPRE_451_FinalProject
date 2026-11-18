# Bluetooth speaker frequency sweep test

Run this independently of the live audio hub, from the folder containing `config.py`:

```bash
sudo apt update
sudo apt install python3-numpy
python3 frequency_test.py
```

Connect the target Bluetooth speaker first. The script prompts you to select one
speaker from `config.py`, then plays a 500 Hz to 18 kHz rising logarithmic sine
sweep for 30 seconds while recording the Raspberry Pi's default PipeWire
microphone input. It writes a CSV analysis to `frequency_test_results/`.

It uses `pw-cat` and `pactl`, which should already be installed for the hub.

## Important limitations

- The microphone must be connected and exposed as the PipeWire default input.
- The script tests whether the microphone can detect acoustic energy; it does
  not establish the upper limit of human hearing.
- Bluetooth speaker latency means recorded peaks will trail the sweep.
- Microphone frequency response, speaker frequency response, room acoustics,
  and automatic gain control affect results.
- Keep volume low. High-frequency tones can be uncomfortable, and pets may hear
  frequencies humans cannot. Do not use this as a hearing test.
