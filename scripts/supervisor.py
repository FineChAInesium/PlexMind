#!/usr/bin/env python3
"""Run PlexMind's four processes in one container.

The API (:8000) and the recommendation worker run as the unprivileged
plexmind user. The scripts controller and the Docker broker run as root and
listen on 127.0.0.1 only, so nothing outside the container can reach them.
A process that exits is restarted; one that keeps exiting stops the
container, so Docker's restart policy takes over.
"""
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_UID = int(os.getenv("PLEXMIND_UID", "1000"))
APP_GID = int(os.getenv("PLEXMIND_GID", "1000"))
# Must exceed control_server's 20 s child-job drain, and the container's
# stop timeout must exceed this plus BROKER_STOP_SECONDS.
DRAIN_SECONDS = 25
BROKER_STOP_SECONDS = 5
CRASH_LIMIT = 5
CRASH_WINDOW_SECONDS = 300

# The processes talk over loopback; these always override inherited values.
INTERNAL_URLS = {
    "DOCKER_BROKER_URL": "http://127.0.0.1:9020",
    "SCRIPTS_API_URL": "http://127.0.0.1:9010",
}
INTERNAL_TOKENS = ("PLEXMIND_CONTROL_TOKEN", "PLEXMIND_BROKER_TOKEN")
PERSISTED_SECRETS = ("PLEXMIND_API_KEY", "PLEXMIND_WEBHOOK_SECRET")
# Processes that read PLEX_URL/PLEX_TOKEN; restarted after a new Plex sign-in.
PLEX_USERS = ("scripts", "worker", "api")
# Let the API answer the sign-in request before it is restarted.
PLEX_RELOAD_DELAY_SECONDS = 2

# (name, command, runs as root). The broker stops last: the scripts
# controller's drain stops Whisper and llama.cpp through it.
SERVICES = [
    ("broker", ["python3", "/app/docker_broker.py"], True),
    ("scripts", ["python3", "/app/control_server.py"], True),
    ("worker", ["python3", "-m", "app.recommendation_worker"], False),
    ("api", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"], False),
]


def log(message):
    print(f"[supervisor] {message}", flush=True)


def data_dir(environ):
    return Path(environ.get("DATA_DIR", "/app/data"))


def persistent_secrets(environ):
    """API key and webhook secret: from the environment, else generated once
    and kept in DATA_DIR/secrets.json (root-only) so they survive restarts.
    Nobody has to make one up; people sign in with Plex."""
    path = data_dir(environ) / "secrets.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored = stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        stored = {}
    values, changed = {}, False
    for name in PERSISTED_SECRETS:
        if environ.get(name):
            values[name] = environ[name]
            continue
        if not stored.get(name):
            stored[name] = secrets.token_urlsafe(32)
            changed = True
        values[name] = stored[name]
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        log(f"generated {', '.join(n for n in PERSISTED_SECRETS if not environ.get(n))} in {path}")
    return values


def plex_auth_file(environ):
    return data_dir(environ) / "plex_auth.json"


def plex_overlay(environ):
    """PLEX_URL and PLEX_TOKEN from a dashboard or setup.sh sign-in, unless
    the environment sets a token itself (that always wins)."""
    if environ.get("PLEX_TOKEN"):
        return {}
    try:
        saved = json.loads(plex_auth_file(environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict) or not saved.get("token") or not saved.get("url"):
        return {}
    return {"PLEX_URL": saved["url"], "PLEX_TOKEN": saved["token"]}


def prepare_env(environ):
    """The environment every process gets: loopback URLs, the internal
    tokens generated for this run unless they are set, the persisted API
    key and webhook secret, and a saved Plex sign-in."""
    env = dict(environ)
    env.update(INTERNAL_URLS)
    for name in INTERNAL_TOKENS:
        if not env.get(name):
            env[name] = secrets.token_urlsafe(32)
    env.update(persistent_secrets(environ))
    env.update(plex_overlay(environ))
    return env


def file_stamp(path):
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def start(service, env):
    name, command, as_root = service
    kwargs = {} if as_root else {"user": APP_UID, "group": APP_GID, "extra_groups": []}
    proc = subprocess.Popen(command, env=env, cwd="/app", start_new_session=True, **kwargs)
    log(f"started {name} (pid {proc.pid})")
    return proc


def stop(procs, seconds):
    """SIGTERM PROCS, then SIGKILL whatever is still running after SECONDS."""
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + seconds
    for proc in procs:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def shutdown(procs):
    log("stopping")
    stop([proc for name, proc in procs.items() if name != "broker"], DRAIN_SECONDS)
    stop([procs["broker"]], BROKER_STOP_SECONDS)


def record_exit(history, now):
    """Exit times within the crash window, including this one."""
    return [t for t in history if now - t < CRASH_WINDOW_SECONDS] + [now]


def main():
    env = prepare_env(os.environ)
    stopping = False

    def on_signal(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    procs = {service[0]: start(service, env) for service in SERVICES}
    exits = {service[0]: [] for service in SERVICES}
    status = 0
    auth_path = plex_auth_file(os.environ)
    auth_stamp = file_stamp(auth_path)
    while not stopping:
        time.sleep(1)
        stamp = file_stamp(auth_path)
        if stamp != auth_stamp and not os.environ.get("PLEX_TOKEN"):
            auth_stamp = stamp
            time.sleep(PLEX_RELOAD_DELAY_SECONDS)
            env.update(plex_overlay(os.environ))
            log("Plex sign-in changed; restarting " + ", ".join(PLEX_USERS))
            stop([procs[name] for name in PLEX_USERS], DRAIN_SECONDS)
            for service in SERVICES:
                if service[0] in PLEX_USERS:
                    procs[service[0]] = start(service, env)
            continue
        for service in SERVICES:
            name = service[0]
            if stopping or procs[name].poll() is None:
                continue
            exits[name] = record_exit(exits[name], time.monotonic())
            log(f"{name} exited with status {procs[name].returncode}")
            if len(exits[name]) > CRASH_LIMIT:
                log(f"{name} exited {len(exits[name])} times in {CRASH_WINDOW_SECONDS} s; stopping the container")
                stopping, status = True, 1
                break
            procs[name] = start(service, env)
    shutdown(procs)
    sys.exit(status)


if __name__ == "__main__":
    main()
