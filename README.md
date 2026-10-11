# 10mila System Monitor

A lightweight OS + MySQL/MariaDB health monitoring daemon for Ubuntu 24.04 LTS and Amazon Linux 2023.
Metrics are collected every 5 seconds, stored in RAM (`/dev/shm`), and served by the collector's own built-in HTTP server on port 8082 with zero SSD writes. It does not depend on OLA, Jetty or a web server group.

## Files

| File | Purpose |
|---|---|
| `install.sh` | Automated install / uninstall script (run as root) |
| `collector.py` | Python 3 daemon — collects metrics and writes JSON |
| `10mila-sysmon.service` | systemd unit file |
| `healthcheck.html` | Chart.js live dashboard (served on port 8082) |

## Requirements

- Ubuntu 24.04 LTS
- Python 3 (stdlib only — no pip packages required)
- `lsof` (optional, recommended): `apt install lsof`
- MySQL/MariaDB (optional — OS metrics work without it)

## Install

```bash
sudo bash install.sh
```

The script will:

1. Create a `sysmon` system user (no login shell)
2. Install `collector.py` to `/opt/10mila-sysmon/`
3. Write default config to `/etc/10mila-sysmon/config.ini`
4. Write MySQL credentials template to `/etc/10mila-sysmon/mysql.cnf`
5. Create `/dev/shm/10mila-sysmon/` (tmpfs, primary JSON output)
6. Register `/dev/shm/10mila-sysmon` in `/etc/tmpfiles.d/` so it exists after a reboot
7. Install and start the systemd service

## Uninstall

```bash
sudo bash install.sh --uninstall
```

## MySQL setup

Edit `/etc/10mila-sysmon/mysql.cnf` and set the correct password, then create the monitoring user in MySQL:

```sql
CREATE USER 'sysmon'@'127.0.0.1' IDENTIFIED BY 'YOUR_PASSWORD';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'sysmon'@'127.0.0.1';
FLUSH PRIVILEGES;
```

## Built-in HTTP server

The collector embeds a lightweight HTTP server (Python `http.server`, stdlib-only) that serves all JSON files and `healthcheck.html` directly from `/dev/shm/10mila-sysmon/`.

| URL | Content |
|---|---|
| `http://192.168.1.21:8082/healthcheck.html` | Live dashboard |
| `http://192.168.1.21:8082/healthcheck.json` | Full metrics + 30 min history |
| `http://192.168.1.21:8082/services.json` | Compact per-service snapshot |

No dependency on Jetty/Java. Port is configurable in `/etc/10mila-sysmon/config.ini` (`http_port = 8082`). Set to `0` to disable.

> `/dev/shm/10mila-sysmon/healthcheck.html` is cleared on reboot, but the service automatically copies it from `/opt/10mila-sysmon/healthcheck.html` (installed by `install.sh`) on every start via `ExecStartPre`. The copy is non-fatal — if the source is missing the service still starts.

## File locations

| Path | Description |
|---|---|
| `/dev/shm/10mila-sysmon/healthcheck.json` | Primary JSON output (tmpfs, in RAM) |
| `/etc/10mila-sysmon/config.ini` | Collector configuration |
| `/etc/10mila-sysmon/mysql.cnf` | MySQL credentials (chmod 640, root:sysmon) |
| `/opt/10mila-sysmon/collector.py` | Collector script |

Dashboard URL: `http://<server>:8082/healthcheck.html`

## Metrics collected

**System**
- CPU usage % (sampled from `/proc/stat`)
- Load average 1m / 5m / 15m
- Memory and swap usage %
- Open file descriptors (kernel count via `/proc/sys/fs/file-nr`; per-process via `lsof`)
- Process count and system uptime
- Network throughput RX/TX (Mbit/s, all interfaces)
- Disk I/O read/write (MB/s, all disks)

**MySQL / MariaDB**
- Queries per second (QPS), SELECTs/s, INSERTs/s
- Active connections, max used, connection limit
- InnoDB buffer pool hit % and size
- Slow queries, open tables
- DB traffic bytes sent/received

History: 360 samples × 5 s interval = 30 minutes of rolling history.

## Useful commands

```bash
# Live logs
journalctl -u 10mila-sysmon -f

# Service status
systemctl status 10mila-sysmon

# Inspect latest JSON
cat /dev/shm/10mila-sysmon/healthcheck.json | python3 -m json.tool | head -60

# Check tmpfs mounts
mount | grep sysmon
```

## Troubleshooting

**CRLF line endings** (if transferred from macOS):
```bash
sed -i 's/\r$//' install.sh collector.py 10mila-sysmon.service
```

**"Read-only file system" warning in logs**
The service uses `ProtectSystem=strict`. Ensure all writable paths are listed in `ReadWritePaths` inside `10mila-sysmon.service`:
```ini
ReadWritePaths=/dev/shm/10mila-sysmon
```
After any service file change: `sudo systemctl daemon-reload && sudo systemctl restart 10mila-sysmon`

**MySQL offline badge shown**
Check credentials in `/etc/10mila-sysmon/mysql.cnf` and that the `sysmon` MySQL user exists with `PROCESS` and `REPLICATION CLIENT` grants.
