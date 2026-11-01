#!/usr/bin/python3
"""waybar custom module: network name + down/up speed, CPU, RAM and GPU usage.
Long-running; prints one JSON line every INTERVAL seconds (waybar "return-type": "json")."""
import glob
import json
import shutil
import subprocess
import sys
import time

INTERVAL = 2
DIM = "<span alpha='55%'>{}</span>"
LABEL = "<span alpha='55%' size='small'>{}</span>"
FIG = " "  # figure space: as wide as a digit, so numbers don't shift the bar


def pad(text, width):
    return FIG * (width - len(text)) + text


def cpu_times():
    with open("/proc/stat") as f:
        vals = list(map(int, f.readline().split()[1:]))
    idle = vals[3] + vals[4]  # idle + iowait
    return sum(vals), idle


def ram_percent():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            info[key] = int(val.split()[0])
    return round(100 * (info["MemTotal"] - info["MemAvailable"]) / info["MemTotal"]), info


def gpu_percent():
    for path in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):  # AMD
        try:
            with open(path) as f:
                return int(f.read())
        except (OSError, ValueError):
            pass
    if shutil.which("nvidia-smi"):
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout.split()
        return int(out[0]) if out else None
    return None


def default_iface():
    with open("/proc/net/route") as f:
        for line in f.readlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000":
                return fields[0]
    return None


def iface_bytes(iface):
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(iface + ":"):
                vals = line.split(":", 1)[1].split()
                return int(vals[0]), int(vals[8])  # rx, tx
    return 0, 0


def connection_name(iface):
    out = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        name, kind, dev = line.rsplit(":", 2)
        if dev == iface:
            if "ethernet" in kind:
                return "Wired"
            if "wireless" in kind:
                return name if len(name) <= 14 else name[:13] + "…"
            return name[:14]
    return iface


def speed(bps):
    if bps >= 1e6:
        num, unit = bps / 1e6, "MB/s"
    else:
        num, unit = bps / 1e3, "KB/s"
    text = f"{num:.1f}" if num < 10 else f"{num:.0f}"
    return pad(text, 3) + " " + unit


def main():
    total, idle = cpu_times()
    iface = default_iface()
    rx, tx = iface_bytes(iface) if iface else (0, 0)
    name, name_at = "", 0
    last = time.monotonic()
    while True:
        time.sleep(INTERVAL)
        now = time.monotonic()
        dt, last = now - last, now

        t2, i2 = cpu_times()
        cpu = round(100 * (1 - (i2 - idle) / max(1, t2 - total)))
        total, idle = t2, i2
        ram, mem = ram_percent()
        gpu = gpu_percent()

        cur = default_iface()
        if cur != iface:
            iface, name_at = cur, 0
            rx, tx = iface_bytes(iface) if iface else (0, 0)
        r2, t2b = iface_bytes(iface) if iface else (0, 0)
        down, up = max(0, r2 - rx) / dt, max(0, t2b - tx) / dt
        rx, tx = r2, t2b
        if iface and now - name_at > 30:
            name, name_at = connection_name(iface), now

        parts = []
        if iface:
            parts.append(f"{DIM.format(name)}  {DIM.format('󰇚')} {speed(down)}"
                         f"  {DIM.format('󰕒')} {speed(up)}")
        else:
            parts.append(DIM.format("󰤭 Offline"))
        parts.append(f"{LABEL.format('CPU')} {pad(str(cpu), 2)}%")
        parts.append(f"{LABEL.format('RAM')} {pad(str(ram), 2)}%")
        if gpu is not None:
            parts.append(f"{LABEL.format('GPU')} {pad(str(gpu), 2)}%")
        used = (mem["MemTotal"] - mem["MemAvailable"]) / 1048576
        tooltip = (f"{iface or 'offline'}\nRAM {used:.1f} / {mem['MemTotal'] / 1048576:.1f} GiB"
                   "\nClick for system monitor")
        print(json.dumps({"text": "   ".join(parts), "tooltip": tooltip}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
