"""Bluetooth playback worker with non-blocking connection monitoring and stats."""

import queue
import subprocess
import threading
import time

from config import SAMPLE_RATE, CHANNELS, PREBUFFER_CHUNKS, CHUNK_DURATION_S

CHECK_INTERVAL = 5.0
REPORT_INTERVAL = 10.0
SLOW_WRITE_FACTOR = 0.75  # Flag writes taking >75% of one chunk period.


def write_all(pipe, data):
    """Write every byte to the pw-cat pipe, handling short writes.

    FileIO.write() is allowed to write fewer bytes than requested. Audio
    chunks must be delivered in full and in order; otherwise a partial chunk
    can corrupt the PCM byte stream.
    """
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


def bluetooth_command(cfg, commands, timeout=15):
    script = (f"select {cfg['controller_mac']}\n" + "\n".join(commands) + "\nquit\n")
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
        if player.stdin:
            player.stdin.close()
    except Exception:
        pass
    try:
        player.terminate()
        player.wait(timeout=2)
    except Exception:
        try:
            player.kill()
            player.wait(timeout=1)
        except Exception:
            pass


def wait_until(deadline, stop_event):
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        stop_event.wait(min(remaining, 0.01))
    return False


def monitor_connection(cfg, state, state_lock, stop_event):
    """Do slow bluetoothctl/pactl work away from the real-time-ish audio loop."""
    name = cfg["name"]
    last_connect = 0.0
    while not stop_event.is_set():
        try:
            connected = is_connected(cfg)
            sink = find_audio_sink(cfg) if connected else None
            if not connected and time.monotonic() - last_connect >= 12:
                last_connect = time.monotonic()
                print(f"[{name}] Bluetooth disconnected; attempting reconnect.", flush=True)
                connect(cfg)
            with state_lock:
                old_connected, old_sink = state["connected"], state["sink"]
                state["connected"] = connected
                state["sink"] = sink
            if old_connected and not connected:
                print(f"[{name}] Bluetooth connection lost.", flush=True)
            if sink and sink != old_sink:
                print(f"[{name}] Sink available: {sink}", flush=True)
        except Exception as exc:
            print(f"[{name}] Connection monitor error: {exc}", flush=True)
        stop_event.wait(CHECK_INTERVAL)


def run(audio_queue, stop_event, speaker_config, start_barrier, clock, scheduler):
    name = speaker_config["name"]
    offset_ms = scheduler.offsets_s[name] * 1000.0
    print(f"[{name}] Worker started: measured latency="
          f"{scheduler.latencies_ms[name]:.1f} ms, offset={offset_ms:.1f} ms.", flush=True)

    state = {"connected": False, "sink": None}
    state_lock = threading.Lock()
    monitor = threading.Thread(target=monitor_connection,
                               args=(speaker_config, state, state_lock, stop_event),
                               name=f"monitor-{name}", daemon=True)
    monitor.start()

    player = None
    active_sink = None
    barrier_joined = False
    stats = {"late_drops": 0, "queue_empty": 0, "slow_writes": 0,
             "write_errors": 0, "written": 0, "max_write_ms": 0.0}
    last_report = time.monotonic()

    def report_stats(now):
        nonlocal stats, last_report
        if now - last_report >= REPORT_INTERVAL:
            print(
                f"[{name}] 10s stats: written={stats['written']}, "
                f"late_drops={stats['late_drops']}, "
                f"queue_empty={stats['queue_empty']}, "
                f"slow_writes={stats['slow_writes']}, "
                f"write_errors={stats['write_errors']}, "
                f"max_write={stats['max_write_ms']:.1f} ms, "
                f"queue_depth={audio_queue.qsize()}", flush=True)
            stats = {"late_drops": 0, "queue_empty": 0, "slow_writes": 0,
                     "write_errors": 0, "written": 0, "max_write_ms": 0.0}
            last_report = now

    try:
        while not stop_event.is_set():
            with state_lock:
                connected, sink = state["connected"], state["sink"]

            # Bluetooth status checks run in monitor thread, never here.
            if not connected or not sink:
                if player is not None:
                    stop_player(player)
                    player = None
                    active_sink = None
                stop_event.wait(0.05)
                continue

            if sink != active_sink:
                if player is not None:
                    stop_player(player)
                    player = None
                active_sink = sink

            if player is None or player.poll() is not None:
                if audio_queue.qsize() < PREBUFFER_CHUNKS:
                    stop_event.wait(0.02)
                    continue
                try:
                    player = subprocess.Popen(
                        ["pw-cat", "--playback", "--raw", "--target", sink,
                         "--media-role", "Music", "--latency", "250ms",
                         "--rate", str(SAMPLE_RATE), "--channels", str(CHANNELS),
                         "--format", "s16", "-"],
                        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=None, bufsize=0,
                    )
                    time.sleep(0.10)
                    if player.poll() is not None:
                        print(f"[{name}] pw-cat exited: {player.returncode}", flush=True)
                        player = None
                        continue
                    print(f"[{name}] Playback stream ready.", flush=True)
                except OSError as exc:
                    stats["write_errors"] += 1
                    print(f"[{name}] Could not start pw-cat: {exc}", flush=True)
                    player = None
                    stop_event.wait(0.5)
                    continue

            if not barrier_joined:
                try:
                    start_barrier.wait(timeout=90)
                    barrier_joined = True
                    print(f"[{name}] Ready; waiting for scheduler start.", flush=True)
                except Exception as exc:
                    print(f"[{name}] Startup synchronization failed: {exc}", flush=True)
                    stop_event.set()
                    break

            if not clock.is_set():
                stop_event.wait(0.01)
                continue

            try:
                seq, chunk = audio_queue.get(timeout=0.15)
            except queue.Empty:
                stats["queue_empty"] += 1
                report_stats(time.monotonic())
                continue

            deadline = scheduler.transmission_deadline(name, seq)
            if not wait_until(deadline, stop_event):
                break

            lateness_ms = max(0.0, (time.monotonic() - deadline) * 1000.0)
            if lateness_ms > CHUNK_DURATION_S * 1000.0:
                stats["late_drops"] += 1
                continue

            if player is None or player.poll() is not None:
                continue
            try:
                write_start = time.monotonic()
                bytes_written = write_all(player.stdin, chunk)
                write_ms = (time.monotonic() - write_start) * 1000.0
                stats["written"] += 1
                if bytes_written != len(chunk):
                    raise OSError(f"Short audio chunk: {bytes_written}/{len(chunk)} bytes")
                stats["max_write_ms"] = max(stats["max_write_ms"], write_ms)
                if write_ms > CHUNK_DURATION_S * 1000.0 * SLOW_WRITE_FACTOR:
                    stats["slow_writes"] += 1
                    print(f"[{name}] Slow PipeWire write: {write_ms:.1f} ms", flush=True)
            except (BrokenPipeError, OSError, ValueError) as exc:
                stats["write_errors"] += 1
                print(f"[{name}] Playback write failed: {exc}", flush=True)
                stop_player(player)
                player = None
                active_sink = None

            report_stats(time.monotonic())
    finally:
        stop_player(player)
        print(f"[{name}] Worker stopped.", flush=True)
