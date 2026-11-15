"""Supervise the local AI Defense daemon independently of its desktop window."""
import argparse
import contextlib
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
CHECK_INTERVAL = 2
FAILURE_WINDOW = 16
STARTUP_WINDOW = 25


@contextlib.contextmanager
def exclusive(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock()
        except OSError:
            yield False
        else:
            yield True


def health(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status == 200:
            value = json.loads(response.read(2048))
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return {}


def _stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    if child.stdin:
        child.stdin.close()


def supervise(command, port, stop, log):
    restarts, delay = 0, 1
    while not stop.is_set():
        env = {**os.environ, "AI_DEFENSE_GUARDIAN_PID": str(os.getpid()),
               "AI_DEFENSE_RESTARTS": str(restarts)}
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=log,
                                 stderr=subprocess.STDOUT, env=env)
        try:
            started = last_ok = last_check = time.monotonic()
            ready = False
            while not stop.is_set() and child.poll() is None:
                now = time.monotonic()
                if now - last_check > FAILURE_WINDOW:
                    last_ok = now  # Allow fresh samples after system sleep.
                last_check = now
                status = health(port)
                sample_age = status.get("claude_sample_in_progress_s")
                healthy = (status.get("pid") == child.pid and status.get("guardian_pid") == os.getpid()
                           and type(status.get("patrol_age_s")) in (int, float)
                           and 0 <= status["patrol_age_s"] <= 8
                           and (sample_age is None or type(sample_age) in (int, float) and 0 <= sample_age <= 8))
                if healthy:
                    if not ready:
                        print(f"guardian ready pid={child.pid} restarts={restarts}", file=log, flush=True)
                    ready, last_ok = True, now
                    if now - started > 60:
                        delay = 1
                elif now - last_ok >= (FAILURE_WINDOW if ready else STARTUP_WINDOW):
                    print(f"guardian heartbeat lost pid={child.pid}", file=log, flush=True)
                    break
                stop.wait(CHECK_INTERVAL)
        finally:
            _stop(child)
        if not stop.is_set():
            restarts += 1
            print(f"guardian scheduling restart in {delay}s", file=log, flush=True)
            stop.wait(delay)
            delay = min(30, delay * 2)


def parent_watch():
    """A supervised daemon exits if its owning watchdog disappears."""
    if os.environ.get("AI_DEFENSE_GUARDIAN_PID"):
        def wait():
            try:
                os.read(sys.stdin.fileno(), 1)
            finally:
                os._exit(75)
        threading.Thread(target=wait, daemon=True, name="guardian-parent").start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()
    config = args.config.resolve()
    port = json.loads(config.read_text(encoding="utf-8")).get("port", 8765)
    if type(port) is not int or not 1024 <= port <= 65535:
        parser.error("invalid local daemon port")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run = ROOT / "run"
    with exclusive(run / f"guardian-{port}.lock") as locked:
        if not locked:
            return
        log_path = run / "guardian.log"
        if log_path.exists() and log_path.stat().st_size > 512 * 1024:
            log_path.replace(run / "guardian.log.1")
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as log:
            supervise([sys.executable, "-u", "-m", "clinic.daemon", "--config", str(config)], port, stop, log)


if __name__ == "__main__":
    main()
