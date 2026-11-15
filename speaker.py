"""One Bluetooth playback listener synchronized to the shared chunk clock."""
import queue
import subprocess
import time

from config import (
    SAMPLE_RATE, CHANNELS, PREBUFFER_CHUNKS,
    CHUNK_DURATION_S, SYNC_START_DELAY_MS,
)

CHECK_INTERVAL = 5.0


def bluetooth_command(speaker_config, commands, timeout=15):
    script = (f"select {speaker_config['controller_mac']}\n" +
              "\n".join(commands) + "\nquit\n")
    return subprocess.run(["bluetoothctl"], input=script, capture_output=True,
                          text=True, timeout=timeout)


def is_connected(cfg):
    result = bluetooth_command(cfg, [f"info {cfg['device_mac']}"], timeout=12)
    return "Connected: yes" in ((result.stdout or "") + (result.stderr or ""))


def connect(cfg):
    return bluetooth_command(cfg, [f"connect {cfg['device_mac']}"], timeout=25)


def find_audio_sink(cfg):
    prefix = "bluez_output." + cfg["device_mac"].replace(":", "_")
    result = subprocess.run(["pactl", "list", "short", "sinks"],
                            capture_output=True, text=True, timeout=10)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].startswith(prefix):
            return fields[1]
    return None


def stop_player(player):
    if player is None:
        return
    try:
        player.stdin.close()
    except Exception:
        pass
    try:
        player.terminate()
        player.wait(timeout=2)
    except Exception:
        try:
            player.kill()
        except Exception:
            pass


def wait_until(deadline, stop_event):
    """Sleep against an absolute monotonic deadline to avoid cumulative drift."""
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        stop_event.wait(min(remaining, 0.01))
    return False


def run(audio_queue, stop_event, speaker_config, start_barrier, clock, shared_epoch):
    name = speaker_config["name"]
    offset_s = float(speaker_config.get("sync_offset_ms", 0)) / 1000.0
    print(f"[{name}] Speaker worker started (sync offset {offset_s * 1000:.0f} ms).", flush=True)

    player = None
    sink = None
    connected = False
    last_check = 0.0
    last_connect = 0.0
    barrier_joined = False
    next_seq = None
    playback_epoch = None

    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_check >= CHECK_INTERVAL:
            last_check = now
            try:
                connected = is_connected(speaker_config)
                if not connected:
                    stop_player(player)
                    player = None
                    sink = None
                    if now - last_connect >= 12:
                        last_connect = now
                        print(f"[{name}] Connecting...", flush=True)
                        connect(speaker_config)
                else:
                    new_sink = find_audio_sink(speaker_config)
                    if new_sink != sink:
                        stop_player(player)
                        player = None
                        sink = new_sink
                        if sink:
                            print(f"[{name}] Found sink: {sink}", flush=True)
            except Exception as exc:
                print(f"[{name}] Bluetooth check error: {exc}", flush=True)

        if not connected or not sink:
            time.sleep(0.1)
            continue

        if player is None or player.poll() is not None:
            if audio_queue.qsize() < PREBUFFER_CHUNKS:
                time.sleep(0.03)
                continue
            player = subprocess.Popen(
                ["pw-cat", "--playback", "--raw", "--target", sink,
                 "--media-role", "Music", "--latency", "100ms",
                 "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS),
                 "--format", "s16", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=None, bufsize=0,
            )
            time.sleep(0.15)
            if player.poll() is not None:
                print(f"[{name}] pw-cat exited: {player.returncode}", flush=True)
                player = None
                continue
            print(f"[{name}] Playback stream ready: {sink}", flush=True)

        # All three players are started and have prebuffered audio before the
        # source releases this barrier. A disconnect after startup is handled
        # as a local restart; it cannot silently reset the other speakers.
        if not barrier_joined:
            try:
                start_barrier.wait(timeout=90)
                barrier_joined = True
                print(f"[{name}] Synchronized start armed.", flush=True)
            except Exception as exc:
                print(f"[{name}] Startup synchronization failed: {exc}", flush=True)
                stop_event.set()
                break

        if not clock.is_set():
            time.sleep(0.01)
            continue

        try:
            seq, chunk = audio_queue.get(timeout=0.2)
            if next_seq is None:
                next_seq = seq

            # The sequence number determines a common absolute deadline for
            # every speaker. Late chunks are skipped rather than adding drift.
            if playback_epoch is None:
                playback_epoch = shared_epoch["value"]
            deadline = playback_epoch + (seq * CHUNK_DURATION_S) - offset_s
            if not wait_until(deadline, stop_event):
                break

            lateness_ms = max(0.0, (time.monotonic() - deadline) * 1000.0)
            if lateness_ms > CHUNK_DURATION_S * 1000:
                #print(f"[{name}] Late by {lateness_ms:.1f} ms; dropping chunk {seq} to catch up.", flush=True)
                continue

            if player.poll() is None:
                player.stdin.write(chunk)
            next_seq = seq + 1
        except queue.Empty:
            continue
        except (BrokenPipeError, OSError) as exc:
            print(f"[{name}] Playback error: {exc}", flush=True)
            stop_player(player)
            player = None

    stop_player(player)
    print(f"[{name}] Worker stopped.", flush=True)
