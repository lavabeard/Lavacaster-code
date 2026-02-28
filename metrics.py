"""
shared/metrics.py — System metrics via /proc filesystem.

Avoids psutil.cpu_percent(interval=N) which breaks under eventlet monkey-patch.
Called by the metrics background thread in app.py.
"""
import time

# Capture the real OS-level sleep before eventlet can monkey-patch time.sleep.
# metrics.py is intentionally imported before SocketIO(..., async_mode='eventlet')
# is instantiated in app.py, so this reference stays unpatched.
_real_sleep = time.sleep


def read_cpu_stat():
    with open("/proc/stat") as f:
        parts = f.readline().split()
    return tuple(int(x) for x in parts[1:8])


def cpu_percent(s1, s2):
    idle  = (s2[3] + s2[4]) - (s1[3] + s1[4])
    total = sum(s2) - sum(s1)
    if total == 0:
        return 0.0
    return round((1.0 - idle / total) * 100.0, 1)


def read_net_dev():
    result = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            parts = line.split()
            if len(parts) < 10:
                continue
            iface = parts[0].rstrip(":")
            result[iface] = (int(parts[1]), int(parts[9]))
    return result


def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.split()[0])
    total  = info.get("MemTotal",     1)
    free   = info.get("MemFree",      0)
    bufs   = info.get("Buffers",      0)
    cached = info.get("Cached",       0)
    sreclm = info.get("SReclaimable", 0)
    used   = total - free - bufs - cached - sreclm
    return (
        round(used / total * 100, 1),
        round(used  / (1024 * 1024), 2),
        round(total / (1024 * 1024), 2),
    )


def collect(interval=1.0):
    """
    Sample CPU, RAM, and per-NIC bandwidth over `interval` seconds.
    Returns a dict suitable for direct Socket.IO emit.
    """
    cpu1 = read_cpu_stat()
    net1 = read_net_dev()
    t1   = time.monotonic()
    _real_sleep(interval)
    cpu2    = read_cpu_stat()
    net2    = read_net_dev()
    t2      = time.monotonic()
    elapsed = max(t2 - t1, 0.001)

    mem_pct, mem_used, mem_total = read_mem()

    nics = {}
    for iface in net2:
        if iface not in net1:
            continue
        rx1, tx1 = net1[iface]
        rx2, tx2 = net2[iface]
        nics[iface] = {
            "tx_mbps": round((tx2 - tx1) * 8 / 1_000_000 / elapsed, 3),
            "rx_mbps": round((rx2 - rx1) * 8 / 1_000_000 / elapsed, 3),
        }

    return {
        "cpu":          cpu_percent(cpu1, cpu2),
        "mem":          mem_pct,
        "mem_used_gb":  mem_used,
        "mem_total_gb": mem_total,
        "nics":         nics,
    }
