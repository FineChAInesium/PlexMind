#!/usr/bin/env python3
"""
Hold a job while the server is too hot or short of memory.

Each sensor has its own limit, and a blank limit turns it off:
  THROTTLE_GPU_TEMP   NVIDIA GPU, in degrees C (through the Docker broker)
  THROTTLE_CPU_TEMP   hottest CPU sensor, in degrees C
  THROTTLE_NVME_TEMP  hottest NVMe drive, in degrees C
  THROTTLE_DISK_TEMP  hottest SATA drive, in degrees C (Unraid's disks.ini)
  THROTTLE_RAM_PCT    memory in use, in percent

`wait` returns at once when everything is under its limit. Otherwise it prints
why, and blocks until every enabled sensor is THROTTLE_RESUME_MARGIN below its
limit. A sensor that cannot be read is skipped: a missing reading must not
stop the job.

Usage: resource_guard.py wait
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HWMON_DIR = Path(os.getenv("THROTTLE_HWMON_DIR", "/sys/class/hwmon"))
DISKS_INI = Path(os.getenv("THROTTLE_DISKS_INI", "/run/unraid-emhttp/disks.ini"))
MEMINFO = Path("/proc/meminfo")
POLL_SECONDS = float(os.getenv("THROTTLE_POLL_SECONDS", "30"))
NOTE_SECONDS = 300
CPU_DRIVERS = {"k10temp", "coretemp", "zenpower"}


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def hwmon_max(names, root=None):
    """Hottest temperature, in degrees C, of the hwmon drivers in NAMES."""
    readings = []
    for device in (root or HWMON_DIR).glob("hwmon*"):
        try:
            if (device / "name").read_text().strip() not in names:
                continue
            readings += [int(path.read_text()) / 1000 for path in device.glob("temp*_input")]
        except (OSError, ValueError):
            continue
    return max(readings) if readings else None


def cpu_temp():
    return hwmon_max(CPU_DRIVERS)


def nvme_temp():
    return hwmon_max({"nvme"})


def gpu_temp():
    """GPU temperature from the first probe container that answers."""
    broker = os.getenv("DOCKER_BROKER_URL", "").rstrip("/")
    token = os.getenv("PLEXMIND_BROKER_TOKEN", "")
    if not broker or not token:
        return None
    containers = [c.strip() for c in os.getenv("GPU_PROBE_CONTAINERS", "llama-cpp").split(",") if c.strip()]
    for container in containers:
        request = urllib.request.Request(
            f"{broker}/containers/{urllib.parse.quote(container)}/gpu", headers={"X-Broker-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                temperature = _number(json.loads(response.read()).get("temperature_c"))
        except (OSError, ValueError):
            continue  # that container is stopped (an idle on-demand llama.cpp, say)
        if temperature is not None:
            return temperature
    return None


def disk_temp(path=None):
    """Hottest SATA drive in Unraid's disks.ini. Spun-down drives report no
    temperature and are skipped; NVMe drives have their own limit."""
    try:
        text = (path or DISKS_INI).read_text(errors="replace")
    except OSError:
        return None
    readings = []
    for section in re.split(r'(?m)^\[', text):
        device = re.search(r'(?m)^device="([^"]*)"', section)
        temperature = re.search(r'(?m)^temp="([^"]*)"', section)
        if device and temperature and device.group(1).startswith("sd"):
            value = _number(temperature.group(1))
            if value is not None:
                readings.append(value)
    return max(readings) if readings else None


def ram_pct(path=None):
    try:
        fields = dict(re.findall(r"(?m)^(\w+):\s+(\d+)", (path or MEMINFO).read_text()))
        return 100 * (1 - int(fields["MemAvailable"]) / int(fields["MemTotal"]))
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


SENSORS = (
    ("THROTTLE_GPU_TEMP", "GPU", "C", gpu_temp),
    ("THROTTLE_CPU_TEMP", "CPU", "C", cpu_temp),
    ("THROTTLE_NVME_TEMP", "NVMe", "C", nvme_temp),
    ("THROTTLE_DISK_TEMP", "hard drive", "C", disk_temp),
    ("THROTTLE_RAM_PCT", "RAM", "%", ram_pct),
)


def configured(environ=os.environ):
    """(limit, label, unit, reader) for each sensor with a usable limit."""
    limits = []
    for name, label, unit, reader in SENSORS:
        limit = _number(environ.get(name))
        if limit is not None and limit > 0:
            limits.append((limit, label, unit, reader))
    return limits


def over(limits, margin=0.0):
    """Descriptions of the sensors at or above their limit less MARGIN."""
    hot = []
    for limit, label, unit, reader in limits:
        value = reader()
        if value is not None and value >= limit - margin:
            hot.append(f"{label} {value:.0f}{unit} (limit {limit:.0f}{unit})")
    return hot


def wait(limits, margin, poll=POLL_SECONDS, sleep=time.sleep, clock=time.monotonic):
    hot = over(limits)
    if not hot:
        return
    print(f"THROTTLE: holding: {', '.join(hot)}", flush=True)
    started = next_note = clock()
    next_note += NOTE_SECONDS
    while True:
        sleep(poll)
        hot = over(limits, margin)
        if not hot:
            print(f"THROTTLE: resuming after {(clock() - started) / 60:.0f} min", flush=True)
            return
        if clock() >= next_note:
            next_note = clock() + NOTE_SECONDS
            print(f"THROTTLE: still holding: {', '.join(hot)}", flush=True)


def main(argv):
    if argv[1:] != ["wait"]:
        print("usage: resource_guard.py wait", file=sys.stderr)
        return 2
    limits = configured()
    if limits:
        wait(limits, _number(os.getenv("THROTTLE_RESUME_MARGIN")) or 5.0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
