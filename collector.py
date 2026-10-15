#!/usr/bin/env python3
"""
10mila System + MySQL Monitor — collector.py

Runs as a systemd service on Ubuntu 24.04.
Collects OS and MySQL/MariaDB metrics every INTERVAL seconds,
keeps a rolling history, and writes a single JSON to /dev/shm/10mila-sysmon/healthcheck.json
(atomically via a temp-file rename).

Usage:  python3 collector.py [--config /etc/10mila-sysmon/config.ini]
"""

import argparse
import configparser
import http.server
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = "/etc/10mila-sysmon/config.ini"

INTERVAL       = 5          # seconds between samples
HISTORY_SIZE   = 360        # samples kept  (360 × 5 s = 30 min)
OUTPUT_DIR     = "/dev/shm/10mila-sysmon"
OUTPUT_FILE    = "healthcheck.json"
SYMLINK_DIR    = ""         # optional second copy of the JSON (empty = disabled)
SERVICES_FILE  = "services.json"   # compact per-service snapshot
LSOF_ENABLED   = True       # set False if lsof is slow on this server
HTTP_PORT      = 8082       # built-in HTTP server port (0 = disabled)

MYSQL_DEFAULTS_FILE = "/etc/10mila-sysmon/mysql.cnf"   # [client] user/password
MYSQL_HOST     = "127.0.0.1"
MYSQL_PORT     = "3306"
# Replication heartbeat: one key:value row the source rewrites with its UTC time every 60 s.
REPL_TABLE     = "replmon.replicamonitoring"
REPL_MAX_AGE   = 120        # seconds: 60 s between heartbeats + 60 s allowed deviation

# ─── Built-in HTTP server ────────────────────────────────────────────────────

class _SysmonHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files from output_dir.  Adds no-cache + CORS headers."""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # type: ignore[override]
        pass   # suppress per-request noise in journald


def start_http_server(directory: str, port: int) -> None:
    handler = lambda *a, **kw: _SysmonHandler(*a, directory=directory, **kw)
    # ThreadingHTTPServer handles each request in its own thread so a slow
    # fetch of the large healthcheck.json never blocks other requests.
    httpd   = http.server.ThreadingHTTPServer(("", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print(f"[sysmon] HTTP server on port {port} → {directory}", flush=True)


# ─── /proc helpers ───────────────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _read_lines(path: str) -> list[str]:
    return _read(path).splitlines()


# ─── CPU ─────────────────────────────────────────────────────────────────────

class CpuMonitor:
    """Calculates CPU usage % from /proc/stat deltas between calls."""

    def __init__(self):
        self._prev: dict[str, list[int]] = {}

    def _parse_stat(self) -> dict[str, list[int]]:
        result = {}
        for line in _read_lines("/proc/stat"):
            if not line.startswith("cpu"):
                continue
            parts = line.split()
            name = parts[0]          # "cpu", "cpu0", "cpu1", …
            result[name] = [int(x) for x in parts[1:]]
        return result

    @staticmethod
    def _pct(curr: list[int], prev: list[int]) -> float:
        # fields: user, nice, system, idle, iowait, irq, softirq, steal, …
        if not prev:
            return 0.0
        delta = [c - p for c, p in zip(curr, prev)]
        idle  = delta[3] + (delta[4] if len(delta) > 4 else 0)  # idle + iowait
        total = sum(delta)
        if total <= 0:
            return 0.0
        return round((total - idle) / total * 100, 1)

    def sample(self) -> dict:
        curr = self._parse_stat()
        overall = self._pct(curr.get("cpu", []), self._prev.get("cpu", []))
        cores   = [
            self._pct(curr.get(f"cpu{i}", []), self._prev.get(f"cpu{i}", []))
            for i in range(max(0, len(curr) - 1))
        ]
        # Context switches (ctxt line in /proc/stat)
        ctx_s = 0
        for line in _read_lines("/proc/stat"):
            if line.startswith("ctxt "):
                ctx_s = int(line.split()[1])
                break
        self._prev = curr
        return {
            "usage_pct":   overall,
            "cores":       cores,
            "core_count":  len(cores),
            "ctx_total":   ctx_s,
        }


# ─── Memory ──────────────────────────────────────────────────────────────────

def sample_memory() -> dict:
    fields: dict[str, int] = {}
    for line in _read_lines("/proc/meminfo"):
        m = re.match(r"(\w+):\s+(\d+)", line)
        if m:
            fields[m.group(1)] = int(m.group(2))  # kB

    def kb_to_mb(k: str) -> float:
        return round(fields.get(k, 0) / 1024, 1)

    total    = kb_to_mb("MemTotal")
    free     = kb_to_mb("MemFree")
    avail    = kb_to_mb("MemAvailable")
    cached   = kb_to_mb("Cached")
    buffers  = kb_to_mb("Buffers")
    used     = round(total - avail, 1)
    pct      = round(used / total * 100, 1) if total else 0.0

    s_total  = kb_to_mb("SwapTotal")
    s_free   = kb_to_mb("SwapFree")
    s_used   = round(s_total - s_free, 1)
    s_pct    = round(s_used / s_total * 100, 1) if s_total else 0.0

    return {
        "total_mb":    total,
        "used_mb":     used,
        "free_mb":     free,
        "available_mb": avail,
        "cached_mb":   cached,
        "buffers_mb":  buffers,
        "usage_pct":   pct,
        "swap_total_mb": s_total,
        "swap_used_mb":  s_used,
        "swap_pct":    s_pct,
    }


# ─── Load average ─────────────────────────────────────────────────────────────

def sample_load() -> dict:
    parts = _read("/proc/loadavg").split()
    if len(parts) < 3:
        return {"avg_1m": 0.0, "avg_5m": 0.0, "avg_15m": 0.0}
    return {
        "avg_1m":  float(parts[0]),
        "avg_5m":  float(parts[1]),
        "avg_15m": float(parts[2]),
    }


# ─── Uptime ───────────────────────────────────────────────────────────────────

def sample_uptime() -> float:
    parts = _read("/proc/uptime").split()
    return float(parts[0]) if parts else 0.0


# ─── Process count ────────────────────────────────────────────────────────────

def sample_processes() -> int:
    try:
        return len([p for p in Path("/proc").iterdir()
                    if p.name.isdigit()])
    except OSError:
        return 0


# ─── Disk space (df -h) ──────────────────────────────────────────────────────

def sample_disk_space() -> list[dict]:
    """
    Parses `df -h --total` output and returns a list of
    {filesystem, size, used, avail, use_pct, mounted_on}
    entries for real filesystems (skip tmpfs, devtmpfs, overlay, etc.).
    """
    try:
        proc = subprocess.run(
            ["df", "-h", "--total"],
            capture_output=True, text=True, timeout=5
        )
        lines = proc.stdout.splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    result = []
    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 6:
            continue
        fs, size, used, avail, use_pct, *rest = parts
        mount = " ".join(rest) if rest else ""
        # Skip pseudo filesystems
        if fs.startswith(("tmpfs", "devtmpfs", "overlay", "squashfs",
                           "udev", "none", "proc", "sysfs", "cgroup")):
            continue
        result.append({
            "filesystem": fs,
            "size": size,
            "used": used,
            "available": avail,
            "use_pct": use_pct.rstrip("%"),
            "mounted_on": mount,
        })
    return result


# ─── Open file descriptors ───────────────────────────────────────────────────

def sample_open_files(lsof_enabled: bool) -> dict:
    # /proc/sys/fs/file-nr: allocated  0  max
    parts = _read("/proc/sys/fs/file-nr").split()
    kernel_open = int(parts[0]) if parts else 0
    kernel_max  = int(parts[2]) if len(parts) > 2 else 0

    lsof_count = None
    if lsof_enabled:
        try:
            proc = subprocess.run(
                ["bash", "-c", "lsof 2>/dev/null | wc -l"],
                capture_output=True, text=True, timeout=5
            )
            val = proc.stdout.strip()
            if val.isdigit():
                lsof_count = int(val)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    result = {
        "kernel_open": kernel_open,
        "kernel_max":  kernel_max,
        "usage_pct":   round(kernel_open / kernel_max * 100, 2) if kernel_max else 0.0,
    }
    if lsof_count is not None:
        result["lsof_count"] = lsof_count
    return result


# ─── Disk I/O ─────────────────────────────────────────────────────────────────

class DiskMonitor:
    """Reads /proc/diskstats; returns delta MB/s and IOPS per device."""

    _SECTOR = 512   # bytes per sector (standard)

    def __init__(self):
        self._prev: dict[str, list[int]] = {}
        self._prev_time: float = time.monotonic()

    @staticmethod
    def _interesting(name: str) -> bool:
        # Keep whole disks (sda, nvme0n1, vda, xvda, …) — not partitions
        return bool(re.match(r"^(sd[a-z]|vd[a-z]|nvme\d+n\d+|xvd[a-z]|hd[a-z])$", name))

    def _parse(self) -> dict[str, list[int]]:
        result = {}
        for line in _read_lines("/proc/diskstats"):
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            if self._interesting(name):
                result[name] = [int(x) for x in parts[3:]]
        return result

    def sample(self) -> dict[str, dict]:
        now  = time.monotonic()
        curr = self._parse()
        dt   = max(now - self._prev_time, 1e-9)
        out: dict[str, dict] = {}

        for name, c in curr.items():
            p = self._prev.get(name, [0] * len(c))
            # fields[0]=reads, [2]=read sectors, [4]=writes, [6]=write sectors
            # fields[8]=IOs in progress, [9]=ms spent on IO
            reads  = (c[0]  - p[0])  / dt
            rsect  = (c[2]  - p[2])  / dt
            writes = (c[4]  - p[4])  / dt
            wsect  = (c[6]  - p[6])  / dt
            # util: ms in IO / (dt * 1000) * 100
            util = 0.0
            if len(c) > 9 and len(p) > 9:
                util = (c[9] - p[9]) / (dt * 1000) * 100
                util = min(util, 100.0)
            out[name] = {
                "read_mb_s":  round(rsect * self._SECTOR / 1024 / 1024, 2),
                "write_mb_s": round(wsect * self._SECTOR / 1024 / 1024, 2),
                "reads_s":    round(reads, 1),
                "writes_s":   round(writes, 1),
                "util_pct":   round(util, 1),
            }

        self._prev = curr
        self._prev_time = now
        return out


# ─── Network I/O ─────────────────────────────────────────────────────────────

class NetMonitor:
    """Reads /proc/net/dev; returns delta MB/s per interface."""

    def __init__(self):
        self._prev: dict[str, list[int]] = {}
        self._prev_time: float = time.monotonic()

    @staticmethod
    def _interesting(name: str) -> bool:
        return not name.startswith("lo")

    def _parse(self) -> dict[str, list[int]]:
        result = {}
        for line in _read_lines("/proc/net/dev"):
            m = re.match(r"\s*(\S+):\s+(.*)", line)
            if not m:
                continue
            name = m.group(1)
            if self._interesting(name):
                result[name] = [int(x) for x in m.group(2).split()]
        return result

    def sample(self) -> dict[str, dict]:
        now  = time.monotonic()
        curr = self._parse()
        dt   = max(now - self._prev_time, 1e-9)
        out: dict[str, dict] = {}

        for name, c in curr.items():
            p = self._prev.get(name, [0] * len(c))
            # columns: rx_bytes rx_packets rx_errs rx_drop … tx_bytes tx_packets tx_errs tx_drop
            rx_bytes = (c[0] - p[0]) / dt
            tx_bytes = (c[8] - p[8]) / dt if len(c) > 8 else 0.0
            rx_err   = c[2]  if len(c) > 2  else 0
            tx_err   = c[10] if len(c) > 10 else 0
            out[name] = {
                "rx_mb_s":   round(rx_bytes / 1024 / 1024, 3),
                "tx_mb_s":   round(tx_bytes / 1024 / 1024, 3),
                "rx_errors": rx_err,
                "tx_errors": tx_err,
            }

        self._prev = curr
        self._prev_time = now
        return out


# ─── Per-process CPU + memory ────────────────────────────────────────────────

class ProcessMonitor:
    """
    Tracks CPU % and RSS memory for named system processes.

    Processes are matched by substrings in /proc/<pid>/cmdline so no
    external tools (ps, top) are needed.
    """

    CLK_TCK: int = os.sysconf("SC_CLK_TCK")   # usually 100 Hz

    # (label, list of cmdline substrings — first match wins)
    WATCHED: list[tuple[str, list[str]]] = [
        ("mysql",    ["mysqld", "mariadbd"]),
        ("ola_java", ["org.soft.ola.Server"]),
    ]

    def __init__(self) -> None:
        # label → {pid, prev_jiffies, prev_time}
        self._prev: dict[str, dict] = {}

    def _find_pid(self, patterns: list[str]) -> "int | None":
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    cmdline = (entry / "cmdline").read_text().replace("\x00", " ")
                    if any(p in cmdline for p in patterns):
                        return int(entry.name)
                except OSError:
                    continue
        except OSError:
            pass
        return None

    def _jiffies(self, pid: int) -> "int | None":
        """Return utime + stime jiffies for pid, or None on error."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # format: pid (comm) state ... — comm may contain spaces
            rp = stat.rfind(")")
            fields = stat[rp + 2:].split()
            # after closing ')': state ppid pgrp … utime[11] stime[12]
            return int(fields[11]) + int(fields[12])
        except (OSError, IndexError, ValueError):
            return None

    def _rss_mb(self, pid: int) -> "tuple[float, float]":
        """Return (rss_mb, vm_size_mb) from /proc/<pid>/status."""
        rss_mb = vm_size_mb = 0.0
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss_mb     = round(int(line.split()[1]) / 1024, 1)
                elif line.startswith("VmSize:"):
                    vm_size_mb = round(int(line.split()[1]) / 1024, 1)
        except (OSError, ValueError, IndexError):
            pass
        return rss_mb, vm_size_mb

    def _java_extras(self, pid: int) -> dict:
        """
        Java-relevant /proc metrics — no JMX, no agent, no extra tools.

        Reads:
          /proc/<pid>/status  → thread count  (Threads:)
          /proc/<pid>/fd/     → open file descriptor count
          /proc/<pid>/stat    → process start time → uptime in seconds
        """
        threads = open_fds = uptime_s = 0
        try:
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("Threads:"):
                    threads = int(line.split()[1])
                    break
        except (OSError, ValueError, IndexError):
            pass
        try:
            # count entries in the fd directory — each is an open descriptor
            open_fds = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        except OSError:
            pass
        try:
            stat_txt = Path(f"/proc/{pid}/stat").read_text()
            rp       = stat_txt.rfind(")")
            fields   = stat_txt[rp + 2:].split()
            starttime_jiffies = int(fields[19])   # field #22 in /proc/pid/stat
            sys_uptime = float(Path("/proc/uptime").read_text().split()[0])
            uptime_s   = max(0, int(sys_uptime - starttime_jiffies / self.CLK_TCK))
        except (OSError, ValueError, IndexError):
            pass
        return {"threads": threads, "open_fds": open_fds, "uptime_s": uptime_s}

    def sample(self) -> dict[str, dict]:
        now = time.monotonic()
        out: dict[str, dict] = {}

        for label, patterns in self.WATCHED:
            pid = self._find_pid(patterns)
            if pid is None:
                self._prev.pop(label, None)
                out[label] = {"available": False}
                continue

            jiffies              = self._jiffies(pid)
            rss_mb, vm_size_mb   = self._rss_mb(pid)
            prev                 = self._prev.get(label, {})

            cpu_pct = 0.0
            if prev.get("pid") == pid and jiffies is not None:
                dt = max(now - prev["prev_time"], 1e-9)
                cpu_pct = max(0.0, round(
                    (jiffies - prev["prev_jiffies"]) / (dt * self.CLK_TCK) * 100, 1
                ))

            self._prev[label] = {
                "pid":          pid,
                "prev_jiffies": jiffies or 0,
                "prev_time":    now,
            }
            entry = {
                "available":   True,
                "pid":         pid,
                "cpu_pct":     cpu_pct,
                "mem_mb":      rss_mb,
                "vm_size_mb":  vm_size_mb,
            }
            if label == "ola_java":
                entry.update(self._java_extras(pid))
            out[label] = entry

        return out


# ─── What is installed here ──────────────────────────────────────────────────
# A server without MySQL or without the OLA server (e.g. the online web server) gets no
# local monitoring of it: nothing is polled and the page hides the section.
MYSQL_PATHS = ["/usr/sbin/mysqld", "/usr/libexec/mysqld", "/usr/sbin/mariadbd", "/usr/libexec/mariadbd"]
OLA_PATHS   = ["/opt/OLA_6", "/etc/systemd/system/ola10mila.service"]


def installed(paths: list[str]) -> bool:
    return any(os.path.exists(p) for p in paths)


# ─── SSH tunnel health ───────────────────────────────────────────────────────

class TunnelMonitor:
    """
    Checks the SSH reverse tunnel managed by autossh.

    Three checks:

    1. Process — is autossh (or the ssh_reverse_tunnel script) running?
       Scanned from /proc/*/cmdline, no external tools needed.

    2. Port LISTEN — is the autossh monitor port listening locally?
       When autossh establishes the tunnel it adds
         -L <monitor_port>:localhost:<monitor_port>
       to the SSH command, which creates a TCP LISTEN socket on that port.
       If the tunnel drops, SSH exits and the socket disappears.
       We read /proc/net/tcp[6] to check — no socket(), no subprocess.

    3. TCP probe (optional) — open a TCP connection to probe_host:probe_port and
       wait for the first byte the far end sends (MySQL and sshd both greet
       first). The time from connect() to that byte is the round trip.
       Pointed at a local port that ssh forwards to the tunnel's remote
       listening port (-L probe_port:127.0.0.1:<remote_port>), the connection
       travels to the remote host, into the reverse tunnel and back to this
       machine's MySQL — a real end-to-end test of the tunnel.
    """

    def __init__(self, monitor_port: int = 20000,
                 service_name: str = "ssh_reverse_tunnel",
                 probe_host: str = "127.0.0.1", probe_port: int = 0,
                 probe_timeout: float = 3.0) -> None:
        self._port         = monitor_port
        self._service_name = service_name
        self._probe_host   = probe_host
        self._probe_port   = probe_port      # 0 = probe disabled
        self._probe_timeout = probe_timeout

    def _probe(self) -> "tuple[bool, float | None, str]":
        """TCP round trip: connect, then wait for the far end's first byte."""
        t0 = time.monotonic()
        try:
            with socket.create_connection((self._probe_host, self._probe_port),
                                          timeout=self._probe_timeout) as sock:
                sock.settimeout(self._probe_timeout)
                if not sock.recv(1):
                    return False, None, "closed without greeting"
            return True, round((time.monotonic() - t0) * 1000, 1), ""
        except socket.timeout:
            return False, None, "timeout"
        except OSError as e:
            return False, None, e.strerror or str(e)

    def _find_pid(self) -> "int | None":
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    cmdline = (entry / "cmdline").read_text().replace("\x00", " ")
                    if "autossh" in cmdline or self._service_name in cmdline:
                        return int(entry.name)
                except OSError:
                    continue
        except OSError:
            pass
        return None

    def _port_listening(self) -> bool:
        """Return True if the monitor port has a LISTEN socket in /proc/net/tcp."""
        if self._port <= 0:
            return True   # monitoring disabled — assume ok
        hex_port = f"{self._port:04X}"
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                for line in Path(path).read_text().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    local_port = parts[1].split(":")[1].upper()
                    state      = parts[3]
                    # state 0A = LISTEN
                    if state == "0A" and local_port == hex_port:
                        return True
            except OSError:
                pass
        return False

    def sample(self) -> dict:
        pid        = self._find_pid()
        port_up    = self._port_listening()
        # Tunnel is considered UP only when the process is running AND
        # the monitor port LISTEN socket exists.
        tunnel_up  = pid is not None and port_up
        out = {
            "process_running": pid is not None,
            "pid":             pid,
            "monitor_port":    self._port,
            "monitor_port_ok": port_up,
            "probe_enabled":   self._probe_port > 0,
        }
        if self._probe_port > 0:
            ok, rtt, err = self._probe()
            out.update({"probe_target": f"{self._probe_host}:{self._probe_port}",
                        "probe_ok": ok, "rtt_ms": rtt, "probe_error": err})
            # With a probe configured, the probe decides: it tests the whole path.
            tunnel_up = ok
        out["available"] = tunnel_up
        return out


# ─── NGINX + PHP-FPM ─────────────────────────────────────────────────────────

def parse_nginx_status(text: str) -> dict:
    """Parse the nginx stub_status page."""
    out: dict = {}
    m = re.search(r"Active connections:\s+(\d+)", text)
    if m:
        out["active_connections"] = int(m.group(1))
    m = re.search(r"server accepts handled requests\s+(\d+)\s+(\d+)\s+(\d+)", text)
    if m:
        out["accepts"], out["handled"], out["requests"] = (int(g) for g in m.groups())
    m = re.search(r"Reading:\s+(\d+)\s+Writing:\s+(\d+)\s+Waiting:\s+(\d+)", text)
    if m:
        out["reading"], out["writing"], out["waiting"] = (int(g) for g in m.groups())
    return out


def parse_fpm_status(text: str) -> dict:
    """Parse the PHP-FPM status page ("key name:   value" lines); keys get underscores."""
    out: dict = {}
    for line in text.splitlines():
        m = re.match(r"^([^:]+):\s+(.+)$", line)
        if not m:
            continue
        raw = m.group(2).strip()
        out[re.sub(r"\s+", "_", m.group(1).strip())] = int(raw) if raw.isdigit() else raw
    return out


class WebMonitor:
    """
    NGINX and PHP-FPM statistics, polled on this machine.

    Nothing is polled unless an nginx process is running, so servers without a
    web server pay nothing and show no web section. With nginx running:

    - nginx_url — the stub_status page (connections, accepts, requests)
    - fpm_url   — the PHP-FPM status page (pm.status_path: processes, queue)

    Both are expected on a loopback-only listener. Per-second rates are the
    difference of the cumulative counters between two samples.
    """

    def __init__(self, nginx_url: str, fpm_url: str, timeout: float = 2.0) -> None:
        self._nginx_url = nginx_url
        self._fpm_url   = fpm_url
        self._timeout   = timeout
        self._prev: dict[str, tuple[int, float]] = {}   # counter → (value, time)
        # Never through a proxy: the status pages are local.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _nginx_running() -> bool:
        try:
            for entry in Path("/proc").iterdir():
                if entry.name.isdigit():
                    try:
                        if (entry / "comm").read_text().strip() == "nginx":
                            return True
                    except OSError:
                        continue
        except OSError:
            pass
        return False

    def _fetch(self, url: str) -> "str | None":
        if not url:
            return None
        try:
            with self._opener.open(url, timeout=self._timeout) as r:
                return r.read(65536).decode("utf-8", "replace")
        except (OSError, ValueError):
            return None

    def _rate(self, name: str, value: "int | None", now: float) -> "float | None":
        """Per-second rate of a cumulative counter; None on the first sample or after a restart."""
        if value is None:
            self._prev.pop(name, None)
            return None
        prev = self._prev.get(name)
        self._prev[name] = (value, now)
        if prev is None or value < prev[0]:
            return None
        return round((value - prev[0]) / max(now - prev[1], 1e-9), 1)

    def sample(self) -> dict:
        # Both URLs empty = switched off (e.g. a server whose nginx is not in use).
        if not (self._nginx_url or self._fpm_url) or not self._nginx_running():
            self._prev.clear()
            return {"nginx_running": False}

        now = time.monotonic()
        out: dict = {"nginx_running": True, "nginx": {"available": False}, "fpm": {"available": False}}

        text = self._fetch(self._nginx_url)
        ng = parse_nginx_status(text) if text else {}
        if "active_connections" in ng:
            ng["available"]  = True
            ng["requests_s"] = self._rate("nginx_requests", ng.get("requests"), now)
            ng["accepts_s"]  = self._rate("nginx_accepts",  ng.get("accepts"),  now)
            ng["dropped"]    = ng.get("accepts", 0) - ng.get("handled", 0)
            out["nginx"] = ng

        text = self._fetch(self._fpm_url)
        fpm = parse_fpm_status(text) if text else {}
        if "active_processes" in fpm:
            fpm["available"]  = True
            fpm["requests_s"] = self._rate("fpm_accepted", fpm.get("accepted_conn"), now)
            out["fpm"] = fpm

        return out


# ─── MySQL / MariaDB ─────────────────────────────────────────────────────────

class MySQLMonitor:
    """Reads SHOW GLOBAL STATUS + SHOW GLOBAL VARIABLES via mysql CLI."""

    def __init__(self, defaults_file: str, host: str, port: str,
                 heartbeat_table: str = REPL_TABLE, max_age_s: float = REPL_MAX_AGE):
        self._defaults = defaults_file
        self._host     = host
        self._port     = port
        # The name goes into SQL: accept schema.table only, anything else switches the heartbeat off.
        self._hb_table = heartbeat_table if re.fullmatch(r"\w+\.\w+", heartbeat_table) else ""
        self._max_age  = max_age_s
        self._last_hb  = None                              # heartbeat value seen last
        self._delays: deque = deque(maxlen=30)             # one apply delay per heartbeat (about 30 min)
        self._prev_status: dict[str, int] = {}
        self._prev_time: float = time.monotonic()

    def _query(self, sql: str) -> dict[str, str]:
        cmd = ["mysql", f"--defaults-file={self._defaults}",
               f"--host={self._host}", f"--port={self._port}",
               "--batch", "--skip-column-names",
               "-e", sql]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                return {}
            result = {}
            for line in proc.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    result[parts[0]] = parts[1]
            return result
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {}

    def _replication(self) -> dict:
        """
        Replication health, the same on the source and on a replica.

        - Heartbeat: the age of the row the source rewrites every 60 s. On the source it shows
          that the injecting service works; on a replica that changes really arrive.
        - Replica threads (I/O and SQL) from performance_schema; none on a source.
        - Delay: commit on the source to applied here, for the last replicated transaction
          (MySQL's own microsecond timestamps; the heartbeat keeps it fresh on a quiet system).
          Jitter is the standard deviation of the delay over the last 30 heartbeats.
          Both include any clock difference between the two servers.
        ok is True / False, or None where there is nothing to judge (no heartbeat, no replica).
        """
        hb = self._query(f"SELECT v, TIMESTAMPDIFF(MICROSECOND, v, UTC_TIMESTAMP(6)) "
                         f"FROM {self._hb_table} WHERE k = 'heartbeat'") if self._hb_table else {}
        rep = self._query(
            "SELECT 'io', SERVICE_STATE FROM performance_schema.replication_connection_status "
            "UNION ALL SELECT 'sql', SERVICE_STATE FROM performance_schema.replication_applier_status "
            "UNION ALL (SELECT 'delay_us', TIMESTAMPDIFF(MICROSECOND, "
            "LAST_APPLIED_TRANSACTION_ORIGINAL_COMMIT_TIMESTAMP, LAST_APPLIED_TRANSACTION_END_APPLY_TIMESTAMP) "
            "FROM performance_schema.replication_applier_status_by_worker "
            "ORDER BY LAST_APPLIED_TRANSACTION_END_APPLY_TIMESTAMP DESC LIMIT 1)")
        return self._judge(hb, rep)

    def _judge(self, hb: dict, rep: dict) -> dict:
        def num(text: "str | None") -> "float | None":
            try:
                return float(text)
            except (TypeError, ValueError):
                return None                                # NULL: nothing applied yet

        hb_at, age_s = None, None
        if hb:
            hb_at, raw = next(iter(hb.items()))
            age_s = num(raw)
            age_s = None if age_s is None else round(age_s / 1e6, 1)
        replica  = "io" in rep
        io_ok    = rep.get("io") == "ON"
        sql_ok   = rep.get("sql") == "ON"
        delay_us = num(rep.get("delay_us")) if replica else None
        delay_ms = None if delay_us is None else round(delay_us / 1000, 1)
        if replica and hb_at != self._last_hb and delay_ms is not None:
            self._delays.append(delay_ms)
        self._last_hb = hb_at

        reasons = []
        if replica and not (io_ok and sql_ok):
            reasons.append(f"replica threads: I/O {rep.get('io', '?')}, SQL {rep.get('sql', '?')}")
        if age_s is not None and age_s > self._max_age:
            reasons.append(f"heartbeat {age_s:.0f} s old (limit {self._max_age:.0f} s)")
        return {
            "role":            "replica" if replica else "source",
            "io_running":      io_ok if replica else None,
            "sql_running":     sql_ok if replica else None,
            "heartbeat_at":    hb_at,
            "heartbeat_age_s": age_s,
            "max_age_s":       self._max_age,
            "delay_ms":        delay_ms,
            "jitter_ms":       round(statistics.pstdev(self._delays), 1) if len(self._delays) > 1 else None,
            "ok":              None if not (replica or hb) else not reasons,
            "reason":          "; ".join(reasons),
        }

    def sample(self) -> dict:
        status = self._query("SHOW GLOBAL STATUS")
        if not status:
            return {"available": False}

        variables = self._query("SHOW GLOBAL VARIABLES")

        now = time.monotonic()
        dt  = max(now - self._prev_time, 1e-9)

        def s(key: str, default: int = 0) -> int:
            return int(status.get(key, default))

        def v(key: str, default: str = "0") -> str:
            return variables.get(key, default)

        def rate(key: str) -> float:
            curr = s(key)
            prev = self._prev_status.get(key, curr)
            return round(max(0.0, (curr - prev) / dt), 2)

        qps         = rate("Questions")
        selects_s   = rate("Com_select")
        inserts_s   = rate("Com_insert")
        updates_s   = rate("Com_update")
        deletes_s   = rate("Com_delete")
        rx_s        = rate("Bytes_received")
        tx_s        = rate("Bytes_sent")

        # InnoDB buffer pool hit rate
        bp_reads    = s("Innodb_buffer_pool_reads")
        bp_req      = s("Innodb_buffer_pool_read_requests")
        prev_reads  = self._prev_status.get("Innodb_buffer_pool_reads", bp_reads)
        prev_req    = self._prev_status.get("Innodb_buffer_pool_read_requests", bp_req)
        d_reads     = max(0, bp_reads - prev_reads)
        d_req       = max(0, bp_req   - prev_req)
        hit_pct     = round((1 - d_reads / d_req) * 100, 2) if d_req > 0 else 100.0

        bp_total    = int(v("innodb_buffer_pool_size", "0"))
        bp_used     = s("Innodb_buffer_pool_bytes_data")
        bp_total_mb = round(bp_total / 1024 / 1024, 1)
        bp_used_pct = round(bp_used / bp_total * 100, 1) if bp_total else 0.0

        # InnoDB fsync rate — measures write fsync() latency from innodb to disk
        fsyncs      = rate("Innodb_data_fsyncs")
        log_fsyncs  = rate("Innodb_os_log_fsyncs")

        self._prev_status = {k: int(val) for k, val in status.items()
                             if val.lstrip("-").isdigit()}
        self._prev_time = now

        return {
            "available": True,
            "version":   v("version", "unknown"),
            "uptime_s":  s("Uptime"),
            "connections": {
                "current":    s("Threads_connected"),
                "running":    s("Threads_running"),
                "max_used":   s("Max_used_connections"),
                "limit":      int(v("max_connections", "0")),
                "errors_total": s("Connection_errors_total"),
                "aborted":    s("Aborted_connects"),
            },
            "queries": {
                "qps":        qps,
                "selects_s":  selects_s,
                "inserts_s":  inserts_s,
                "updates_s":  updates_s,
                "deletes_s":  deletes_s,
                "slow_queries": s("Slow_queries"),
                "open_tables":  s("Open_tables"),
            },
            "innodb": {
                "buffer_pool_mb":       bp_total_mb,
                "buffer_pool_used_pct": bp_used_pct,
                "buffer_pool_hit_pct":  hit_pct,
                "reads_s":   rate("Innodb_data_reads"),
                "writes_s":  rate("Innodb_data_writes"),
                "fsyncs_s":  fsyncs,
                "log_fsyncs_s": log_fsyncs,
                "dirty_pages": s("Innodb_buffer_pool_pages_dirty"),
            },
            "bytes": {
                "received_mb_s": round(rx_s / 1024 / 1024, 3),
                "sent_mb_s":     round(tx_s / 1024 / 1024, 3),
            },
            "replication": {
                "slave_running": status.get("Slave_running",
                                 status.get("Replica_running", "OFF")),
                **self._replication(),
            },
        }


# ─── History ──────────────────────────────────────────────────────────────────

HISTORY_KEYS = [
    "timestamps",
    "cpu_pct", "mem_pct", "swap_pct",
    "load_1m", "load_5m",
    "open_files_kernel",
    "processes",
    "disk_read_mb_s", "disk_write_mb_s",
    "net_rx_mb_s", "net_tx_mb_s",
    "mysql_qps", "mysql_connections", "mysql_buf_hit_pct",
    # OLA Java process metrics (from /proc — no JMX needed)
    "java_cpu_pct", "java_mem_mb", "java_threads", "java_open_fds",
    # SSH tunnel TCP probe: round trip in ms (None when the probe failed) and up/down as 1/0
    "tunnel_rtt_ms", "tunnel_up",
    # MySQL replication: heartbeat age (s), apply delay (ms), health as 1/0 (None where not in use)
    "repl_age_s", "repl_delay_ms", "repl_ok",
    # NGINX + PHP-FPM (None while nginx is not running or a status page does not answer)
    "nginx_active", "nginx_waiting", "nginx_rps",
    "fpm_active", "fpm_idle", "fpm_queue", "fpm_rps",
]


def empty_history(size: int) -> dict[str, deque]:
    return {k: deque(maxlen=size) for k in HISTORY_KEYS}


def restore_history(data: dict, size: int) -> dict[str, deque]:
    h = empty_history(size)
    src = data.get("history", {})
    n = len(src.get("timestamps", []))
    for k in HISTORY_KEYS:
        # A series that is new since the JSON was written is padded, so all series keep the same length.
        for v in src.get(k, [None] * n):
            h[k].append(v)
    return h


def push_history(h: dict[str, deque], snapshot: dict,
                 processes: "dict | None" = None) -> None:
    cur   = snapshot["current"]
    mysql = snapshot["mysql"]
    net   = cur.get("network", {})
    disk  = cur.get("disk", {})

    # Sum all interfaces / disks for single-line history
    rx = sum(i["rx_mb_s"] for i in net.values())
    tx = sum(i["tx_mb_s"] for i in net.values())
    dr = sum(d["read_mb_s"]  for d in disk.values())
    dw = sum(d["write_mb_s"] for d in disk.values())

    h["timestamps"].append(snapshot["collected_at"])
    h["cpu_pct"].append(cur["cpu"]["usage_pct"])
    h["mem_pct"].append(cur["memory"]["usage_pct"])
    h["swap_pct"].append(cur["memory"]["swap_pct"])
    h["load_1m"].append(cur["load"]["avg_1m"])
    h["load_5m"].append(cur["load"]["avg_5m"])
    h["open_files_kernel"].append(cur["open_files"]["kernel_open"])
    h["processes"].append(cur["system"]["processes"])
    h["disk_read_mb_s"].append(dr)
    h["disk_write_mb_s"].append(dw)
    h["net_rx_mb_s"].append(rx)
    h["net_tx_mb_s"].append(tx)
    h["mysql_qps"].append(mysql.get("queries", {}).get("qps", 0) if mysql.get("available") else 0)
    h["mysql_connections"].append(mysql.get("connections", {}).get("current", 0) if mysql.get("available") else 0)
    h["mysql_buf_hit_pct"].append(mysql.get("innodb", {}).get("buffer_pool_hit_pct", 0) if mysql.get("available") else 0)

    # OLA Java process metrics
    java = (processes or {}).get("ola_java", {})
    h["java_cpu_pct"].append(  java.get("cpu_pct",   0) if java.get("available") else 0)
    h["java_mem_mb"].append(   java.get("mem_mb",    0) if java.get("available") else 0)
    h["java_threads"].append(  java.get("threads",   0) if java.get("available") else 0)
    h["java_open_fds"].append( java.get("open_fds",  0) if java.get("available") else 0)

    tunnel = snapshot.get("ssh_tunnel", {})
    h["tunnel_rtt_ms"].append(tunnel.get("rtt_ms"))
    h["tunnel_up"].append(1 if tunnel.get("available") else 0)

    repl = mysql.get("replication", {})
    h["repl_age_s"].append(repl.get("heartbeat_age_s"))
    h["repl_delay_ms"].append(repl.get("delay_ms"))
    h["repl_ok"].append(None if repl.get("ok") is None else int(repl["ok"]))

    web = snapshot.get("web", {})
    ng  = web.get("nginx", {})
    fpm = web.get("fpm", {})
    h["nginx_active"].append( ng.get("active_connections"))
    h["nginx_waiting"].append(ng.get("waiting"))
    h["nginx_rps"].append(    ng.get("requests_s"))
    h["fpm_active"].append(   fpm.get("active_processes"))
    h["fpm_idle"].append(     fpm.get("idle_processes"))
    h["fpm_queue"].append(    fpm.get("listen_queue"))
    h["fpm_rps"].append(      fpm.get("requests_s"))


def serialise_history(h: dict[str, deque]) -> dict:
    return {k: list(v) for k, v in h.items()}


# ─── Atomic JSON write ────────────────────────────────────────────────────────

def write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(path)
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.chmod(tmp, 0o644)   # world-readable before atomic rename
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── Publish copy ────────────────────────────────────────────────────────────
# Writes a real copy of the JSON to a second directory (e.g. the web server's
# document root). A real file is used instead of a symlink so that any web
# server (including Jetty, which blocks symlinks by default) can serve it.

def publish_copy(data: dict, publish_dir: str, filename: str) -> None:
    if not publish_dir:
        return
    try:
        os.makedirs(publish_dir, exist_ok=True)
        dest = os.path.join(publish_dir, filename)
        fd, tmp = tempfile.mkstemp(dir=publish_dir, prefix=".tmp_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, separators=(",", ":"))
            os.chmod(tmp, 0o644)   # world-readable before atomic rename
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        print(f"[sysmon] WARNING: could not publish copy to {publish_dir}: {e}", flush=True)


# ─── Device counter (online results server only) ─────────────────────────────
# nginx gives every browser a cookie (ngx_http_userid_module) and sends one syslog datagram per HTML
# page load to a unix socket:  <190>Oct 15 20:13:00 tmdev: tmdev=<32 hex>\t<unix s.ms>\t<user agent>
# (user agent JSON-escaped, may be empty). Counted in memory, written as statistics.json.

DEVICES_MAX = 50000   # ponytail: ceiling on devices kept (~25 MB RAM, ~10 MB JSON); new ones beyond are not counted
UA_MAX      = 400     # longer user agents are cut

_BOT_RE = re.compile(r"bot|crawl|spider|slurp|curl|wget|python|httpclient|okhttp|go-http|java/|headless|lighthouse", re.I)


def parse_device_datagram(data: bytes):
    """One nginx syslog datagram -> (uid, unix seconds, user agent), or None if malformed."""
    try:
        msg = data.decode("utf-8", "replace").rstrip("\r\n")
        if msg.startswith("<"):                     # syslog header: <PRI>Mmm dd hh:mm:ss tag:
            cut = msg.find(": ")
            if cut < 0:
                return None
            msg = msg[cut + 2:]
        parts = msg.split("\t", 2)
        if len(parts) != 3:
            return None
        uid, ts, ua = parts
        if uid.startswith("tmdev="):
            uid = uid[6:]
        if not re.fullmatch(r"[0-9A-Fa-f]{32}", uid):
            return None
        ts = float(ts)
        if not 1e9 < ts < 1e10:                     # also rejects nan/inf
            return None
        try:
            ua = json.loads('"' + ua + '"')         # undo log_format escape=json
        except ValueError:
            pass
        return uid.upper(), round(ts, 3), ua[:UA_MAX]
    except (ValueError, UnicodeError):
        return None


def ua_family(ua: str) -> tuple[str, str]:
    """(browser, os) from a user agent, by substring; good enough for a pie, not for a fingerprint."""
    if not ua:
        return "Unknown", "Unknown"
    if _BOT_RE.search(ua):
        browser = "Bot"
    elif any(s in ua for s in ("Edg/", "EdgA/", "EdgiOS/", "Edge/")):
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "SamsungBrowser/" in ua:
        browser = "Samsung Internet"
    elif "FBAN" in ua or "FBAV" in ua:
        browser = "Facebook"
    elif "Instagram" in ua:
        browser = "Instagram"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        browser = "Firefox"
    elif "CriOS/" in ua or "Chrome/" in ua or "Chromium/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "Other"
    if any(s in ua for s in ("iPhone", "iPad", "iPod")):
        os_ = "iOS"
    elif "Android" in ua:
        os_ = "Android"
    elif "Windows" in ua:
        os_ = "Windows"
    elif "CrOS" in ua:
        os_ = "ChromeOS"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        os_ = "macOS"
    elif "Linux" in ua or "X11" in ua:
        os_ = "Linux"
    else:
        os_ = "Other"
    return browser, os_


class DeviceCounter:
    """Receives the nginx datagrams and writes statistics.json every interval."""

    def __init__(self, sock_path: str, out_path: str, interval: int) -> None:
        self.sock_path, self.out_path, self.interval = sock_path, out_path, interval
        self.lock    = threading.Lock()
        self.devices: dict = {}
        self.since   = round(time.time(), 3)
        self.dirty   = True
        # A restart keeps the count (tmpfs: gone after a reboot, like healthcheck.json's history).
        try:
            with open(out_path) as f:
                old = json.load(f)
            for uid, d in old.get("list", {}).items():
                if len(self.devices) < DEVICES_MAX and re.fullmatch(r"[0-9A-F]{32}", uid):
                    self.devices[uid] = {"ua": str(d["ua"])[:UA_MAX], "first": float(d["first"]),
                                         "last": float(d["last"]), "hits": int(d["hits"])}
            self.since = float(old.get("since", self.since))
            print(f"[sysmon] devices: restored {len(self.devices)}", flush=True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def add(self, uid: str, ts: float, ua: str) -> None:
        with self.lock:
            d = self.devices.get(uid)
            if d is None:
                if len(self.devices) >= DEVICES_MAX:
                    return
                self.devices[uid] = {"ua": ua, "first": ts, "last": ts, "hits": 1}
            else:
                d["hits"] += 1
                d["first"], d["last"] = min(d["first"], ts), max(d["last"], ts)
                if ua:
                    d["ua"] = ua
            self.dirty = True

    def snapshot(self) -> dict:
        with self.lock:
            devices = {uid: dict(d) for uid, d in self.devices.items()}
        browsers: dict = {}
        systems:  dict = {}
        per_hour: dict = {}
        for d in devices.values():
            b, o = ua_family(d["ua"])
            browsers[b] = browsers.get(b, 0) + 1
            systems[o]  = systems.get(o, 0) + 1
            h = datetime.fromtimestamp(d["first"] - d["first"] % 3600, timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
            per_hour[h] = per_hour.get(h, 0) + 1
        by_count = lambda c: dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))
        return {
            "generated":    round(time.time(), 3),
            "since":        self.since,
            "devices":      len(devices),
            "by_browser":   by_count(browsers),
            "by_os":        by_count(systems),
            "new_per_hour": dict(sorted(per_hour.items())),
            "list":         devices,
        }

    def _receive(self) -> None:
        while True:
            try:
                try:
                    os.unlink(self.sock_path)          # left over from the last run
                except FileNotFoundError:
                    pass
                s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                s.bind(self.sock_path)
                os.chmod(self.sock_path, 0o666)        # nginx workers run as another user
                print(f"[sysmon] devices: listening on {self.sock_path}", flush=True)
                break
            except OSError as e:
                print(f"[sysmon] devices: cannot bind {self.sock_path}: {e}; retrying", flush=True)
                time.sleep(30)
        while True:
            try:
                rec = parse_device_datagram(s.recv(65536))
                if rec:
                    self.add(*rec)
            except Exception as e:                     # never let one datagram stop the counter
                print(f"[sysmon] devices: {e}", flush=True)
                time.sleep(0.1)

    def _write(self) -> None:
        while True:
            if self.dirty:
                self.dirty = False
                try:
                    write_json(self.out_path, self.snapshot())
                except Exception as e:
                    self.dirty = True
                    print(f"[sysmon] ERROR writing {self.out_path}: {e}", flush=True)
            time.sleep(self.interval)

    def start(self) -> None:
        threading.Thread(target=self._receive, daemon=True).start()
        threading.Thread(target=self._write,   daemon=True).start()


# ─── Config ───────────────────────────────────────────────────────────────────

def load_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


# ─── Main loop ────────────────────────────────────────────────────────────────

def selftest() -> None:
    """python3 collector.py --selftest — the status page parsers, on real sample pages."""
    ng = parse_nginx_status("Active connections: 291 \nserver accepts handled requests\n"
                            " 16630948 16630946 31070465 \nReading: 6 Writing: 179 Waiting: 106 \n")
    assert ng == {"active_connections": 291, "accepts": 16630948, "handled": 16630946,
                  "requests": 31070465, "reading": 6, "writing": 179, "waiting": 106}, ng
    fpm = parse_fpm_status("pool:                 www\nprocess manager:      dynamic\n"
                           "start time:           11/Oct/2026:20:00:00 +0200\naccepted conn:        1234\n"
                           "listen queue:         0\nmax listen queue:     7\nidle processes:       4\n"
                           "active processes:     1\ntotal processes:      5\nmax children reached: 0\n")
    assert fpm["pool"] == "www" and fpm["accepted_conn"] == 1234 and fpm["max_listen_queue"] == 7, fpm
    assert fpm["active_processes"] == 1 and fpm["start_time"].startswith("11/Oct"), fpm
    w = WebMonitor("", "")
    assert w.sample() == {"nginx_running": False}   # no URLs: off, whatever is running
    assert w._rate("c", 100, 10.0) is None and w._rate("c", 150, 15.0) == 10.0
    assert w._rate("c", 5, 20.0) is None            # counter went backwards: service restarted
    h = restore_history({"history": {"timestamps": ["a", "b"], "cpu_pct": [1, 2]}}, 10)
    assert list(h["nginx_rps"]) == [None, None] and list(h["cpu_pct"]) == [1, 2]
    assert installed(["/nonexistent", "/"]) and not installed(["/nonexistent"]) and not installed([])
    m = MySQLMonitor("", "", "", "replmon.replicamonitoring", 120)
    assert MySQLMonitor("", "", "", "x; DROP TABLE y")._hb_table == ""
    assert m._judge({}, {})["ok"] is None                                    # no heartbeat, no replica
    src = m._judge({"2026-10-12 17:00:00.000": "31000000"}, {})              # source, 31 s old
    assert src["ok"] is True and src["role"] == "source" and src["heartbeat_age_s"] == 31.0 and src["delay_ms"] is None
    assert m._judge({"2026-10-12 17:00:00.000": "121000000"}, {})["ok"] is False   # more than 60 s late
    up = {"io": "ON", "sql": "ON", "delay_us": "4200"}
    r = m._judge({"a": "5000000"}, up)
    assert r["ok"] is True and r["role"] == "replica" and r["delay_ms"] == 4.2 and r["jitter_ms"] is None
    m._judge({"a": "9000000"}, {**up, "delay_us": "9999"})                   # same heartbeat: no new delay sample
    assert m._judge({"b": "5000000"}, {**up, "delay_us": "6200"})["jitter_ms"] == 1.0
    r = m._judge({"b": "5000000"}, {"io": "CONNECTING", "sql": "ON", "delay_us": "NULL"})
    assert r["ok"] is False and r["delay_ms"] is None and "I/O CONNECTING" in r["reason"]
    # Device counter: nginx syslog datagrams and user agent families.
    uid = "0123456789abcdef0123456789ABCDEF"
    iphone = r"Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    assert parse_device_datagram(f"<190>Oct 15 20:13:00 tmdev: tmdev={uid}\t1760551980.123\t{iphone}".encode()) \
        == (uid.upper(), 1760551980.123, iphone)
    assert parse_device_datagram(f"tmdev={uid}\t1760551980.5\t".encode()) == (uid.upper(), 1760551980.5, "")
    assert parse_device_datagram(f"<190>Oct  5 01:02:03 tmdev: tmdev={uid}\t1760551980\ta \\\"b\\\" \\u00e5".encode())[2] == 'a "b" \u00e5'
    for bad in (b"", b"garbage", b"<190>no header", f"tmdev={uid}\t1760551980".encode(),
                f"tmdev=\t1760551980\tx".encode(), f"tmdev={uid[:-1]}\t1760551980\tx".encode(),
                f"tmdev={uid}\tnan\tx".encode(), f"tmdev={uid}\t-\tx".encode(), b"\xff\xfe\x00"):
        assert parse_device_datagram(bad) is None, bad
    assert ua_family(iphone) == ("Safari", "iOS")
    assert ua_family("Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36") == ("Samsung Internet", "Android")
    assert ua_family("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0") == ("Edge", "Windows")
    assert ua_family("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/129.0.0.0 Safari/537.36") == ("Chrome", "Windows")
    assert ua_family("Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:131.0) Gecko/20100101 Firefox/131.0") == ("Firefox", "macOS")
    assert ua_family("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0") == ("Firefox", "Linux")
    assert ua_family("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")[0] == "Bot"
    assert ua_family("curl/8.5.0") == ("Bot", "Other") and ua_family("") == ("Unknown", "Unknown")
    dc = DeviceCounter("", "/nonexistent/statistics.json", 5)
    dc.add(uid.upper(), 1760551980.0, iphone)
    dc.add(uid.upper(), 1760551990.0, "")                      # empty UA keeps the known one
    dc.add("F" * 32, 1760555600.0, "curl/8.5.0")
    st = dc.snapshot()
    assert st["devices"] == 2 and st["list"][uid.upper()] == {"ua": iphone, "first": 1760551980.0,
                                                              "last": 1760551990.0, "hits": 2}
    assert st["by_browser"] == {"Bot": 1, "Safari": 1} and st["by_os"] == {"Other": 1, "iOS": 1}
    assert st["new_per_hour"] == {"2025-10-15T18:00:00Z": 1, "2025-10-15T19:00:00Z": 1}, st["new_per_hour"]
    print("selftest ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="10mila system + MySQL monitor")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--selftest", action="store_true", help="check the parsers and exit")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return

    cfg = load_config(args.config)

    interval     = cfg.getint("monitor",  "interval",     fallback=INTERVAL)
    history_size = cfg.getint("monitor",  "history_size", fallback=HISTORY_SIZE)
    output_dir   = cfg.get   ("monitor",  "output_dir",   fallback=OUTPUT_DIR)
    output_file  = cfg.get   ("monitor",  "output_file",  fallback=OUTPUT_FILE)
    publish_dir  = cfg.get   ("monitor",  "publish_dir",  fallback=
                   cfg.get("monitor",  "symlink_dir",  fallback=SYMLINK_DIR))
    services_file = cfg.get  ("monitor",  "services_file", fallback=SERVICES_FILE)
    http_port    = cfg.getint("monitor",  "http_port",     fallback=HTTP_PORT)
    lsof_enabled = cfg.getboolean("monitor", "lsof_enabled", fallback=LSOF_ENABLED)
    mysql_cfg    = cfg.get   ("mysql",    "defaults_file",fallback=MYSQL_DEFAULTS_FILE)
    mysql_host   = cfg.get   ("mysql",    "host",         fallback=MYSQL_HOST)
    mysql_port   = cfg.get   ("mysql",    "port",         fallback=MYSQL_PORT)
    repl_table   = cfg.get   ("replication", "heartbeat_table", fallback=REPL_TABLE)
    repl_max_age = cfg.getfloat("replication", "max_age_s",    fallback=REPL_MAX_AGE)
    # Space separated paths; MySQL or OLA counts as installed when one of them exists.
    mysql_paths  = cfg.get   ("mysql",    "installed_paths", fallback=" ".join(MYSQL_PATHS)).split()
    ola_paths    = cfg.get   ("ola",      "installed_paths", fallback=" ".join(OLA_PATHS)).split()
    tunnel_port  = cfg.getint("tunnel",   "monitor_port", fallback=20000)
    tunnel_svc   = cfg.get   ("tunnel",   "service_name", fallback="ssh_reverse_tunnel")
    probe_host   = cfg.get   ("tunnel",   "probe_host",   fallback="127.0.0.1")
    probe_port   = cfg.getint("tunnel",   "probe_port",   fallback=0)
    probe_timeout = cfg.getfloat("tunnel", "probe_timeout_s", fallback=3.0)
    nginx_url    = cfg.get   ("web",      "nginx_url",    fallback="http://127.0.0.1:8080/nginx_status")
    fpm_url      = cfg.get   ("web",      "fpm_url",      fallback="http://127.0.0.1:8080/fpm_status")
    devices_sock = cfg.get   ("devices",  "socket",       fallback="")   # empty = no device counter
    devices_file = cfg.get   ("devices",  "statistics_file", fallback="statistics.json")

    output_path  = os.path.join(output_dir, output_file)
    link_name    = output_file

    hostname = socket.gethostname()

    print(f"[sysmon] starting — interval={interval}s  history={history_size}  output={output_path}", flush=True)

    if http_port > 0:
        # Ensure the output dir exists before the HTTP server thread starts
        os.makedirs(output_dir, exist_ok=True)
        start_http_server(output_dir, http_port)

    if devices_sock:
        DeviceCounter(devices_sock, os.path.join(output_dir, devices_file), interval).start()

    cpu_mon  = CpuMonitor()
    disk_mon = DiskMonitor()
    net_mon  = NetMonitor()
    db_mon     = MySQLMonitor(mysql_cfg, mysql_host, mysql_port, repl_table, repl_max_age)
    proc_mon   = ProcessMonitor()
    tunnel_mon = TunnelMonitor(tunnel_port, tunnel_svc, probe_host, probe_port, probe_timeout)
    web_mon    = WebMonitor(nginx_url, fpm_url)

    # Restore history from existing JSON (keeps graphs continuous on restart)
    history = empty_history(history_size)
    try:
        with open(output_path) as f:
            old = json.load(f)
        history = restore_history(old, history_size)
        print(f"[sysmon] restored {len(history['timestamps'])} history samples", flush=True)
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    print(f"[sysmon] publish_dir: {publish_dir}", flush=True)

    # Warm-up pass so delta calculations have a baseline
    cpu_mon.sample()
    disk_mon.sample()
    net_mon.sample()
    proc_mon.sample()
    time.sleep(interval)

    while True:
        t0 = time.monotonic()

        ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Sampled first: Java metrics flow into history AND services, and a running
        # process counts as installed, wherever it was installed.
        processes = proc_mon.sample()
        has_mysql = processes["mysql"]["available"]    or installed(mysql_paths)
        has_ola   = processes["ola_java"]["available"] or installed(ola_paths)
        processes["mysql"]["installed"]    = has_mysql
        processes["ola_java"]["installed"] = has_ola

        snapshot: dict = {
            "meta": {
                "version":        "1.1",
                "hostname":       hostname,
                "collected_at":   ts_iso,
                "interval_s":     interval,
                "history_samples": history_size,
            },
            "collected_at": ts_iso,
            "current": {
                "cpu":       cpu_mon.sample(),
                "memory":    sample_memory(),
                "load":      sample_load(),
                "disk":      disk_mon.sample(),
                "network":   net_mon.sample(),
                "open_files":  sample_open_files(lsof_enabled),
                "disk_space":  sample_disk_space(),
                "system": {
                    "uptime_s":  sample_uptime(),
                    "processes": sample_processes(),
                },
            },
            "installed": {"mysql": has_mysql, "ola": has_ola},
            "mysql": db_mon.sample() if has_mysql else {"available": False},
            "ssh_tunnel": tunnel_mon.sample(),
            "web": web_mon.sample(),
        }

        push_history(history, snapshot, processes)
        snapshot["history"] = serialise_history(history)

        # ── Compact services snapshot (no history) ──────────────────────────
        mem = snapshot["current"]["memory"]
        services_snapshot: dict = {
            "collected_at": ts_iso,
            "hostname":     hostname,
            "system": {
                "cpu_pct":      snapshot["current"]["cpu"]["usage_pct"],
                "mem_pct":      mem["usage_pct"],
                "mem_used_mb":  mem["used_mb"],
                "mem_total_mb": mem["total_mb"],
                "load_1m":      snapshot["current"]["load"]["avg_1m"],
            },
            "services": {
                "mysql": {
                    **processes.get("mysql", {"available": False}),
                    "db_available": snapshot["mysql"].get("available", False),
                    "qps":          snapshot["mysql"].get("queries", {}).get("qps", 0)
                                    if snapshot["mysql"].get("available") else 0,
                    "inserts_s":    snapshot["mysql"].get("queries", {}).get("inserts_s", 0)
                                    if snapshot["mysql"].get("available") else 0,
                    "updates_s":    snapshot["mysql"].get("queries", {}).get("updates_s", 0)
                                    if snapshot["mysql"].get("available") else 0,
                    "connections":  snapshot["mysql"].get("connections", {}).get("current", 0)
                                    if snapshot["mysql"].get("available") else 0,
                    "fsyncs_s":     snapshot["mysql"].get("innodb", {}).get("fsyncs_s", 0)
                                    if snapshot["mysql"].get("available") else 0,
                },
                "disk_space": {
                    "available": True,
                    "mounts": snapshot["current"].get("disk_space", []),
                },
                "ola_java":    processes.get("ola_java", {"available": False}),
                # Binary health for the central monitoring: available = replication is healthy.
                "replication": {**snapshot["mysql"].get("replication", {}),
                                "available": snapshot["mysql"].get("replication", {}).get("ok") is True},
                "ssh_tunnel":  snapshot["ssh_tunnel"],
                "web":         snapshot["web"],
            },
        }

        try:
            write_json(output_path, snapshot)
            publish_copy(snapshot, publish_dir, output_file)
            # Write services.json to both locations
            services_path = os.path.join(output_dir, services_file)
            write_json(services_path, services_snapshot)
            publish_copy(services_snapshot, publish_dir, services_file)
        except Exception as e:
            print(f"[sysmon] ERROR writing JSON: {e}", flush=True)

        elapsed = time.monotonic() - t0
        sleep_s = max(0.0, interval - elapsed)
        time.sleep(sleep_s)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[sysmon] stopped", flush=True)
        sys.exit(0)
