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
| `statistics.html` | Unique devices on the online results site (web server only, no external hosts) |

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
-- only for the replication box: replica threads and delay, and the heartbeat row
GRANT SELECT ON performance_schema.* TO 'sysmon'@'127.0.0.1';
GRANT SELECT ON replmon.* TO 'sysmon'@'127.0.0.1';
FLUSH PRIVILEGES;
```

## Replication monitoring

The source server rewrites one row with its own UTC time every 60 seconds:

```sql
CREATE TABLE replmon.replicamonitoring (k VARCHAR(32) PRIMARY KEY, v DATETIME(3) NOT NULL);
REPLACE INTO replmon.replicamonitoring VALUES ('heartbeat', UTC_TIMESTAMP(3));   -- every 60 s
```

Every collector that can read the row (source and replicas) reports under `mysql.replication` in
`healthcheck.json` and under `services.replication` in `services.json`:

| Field | Meaning |
|---|---|
| `ok` | binary health: the heartbeat is at most `max_age_s` old (default 120 = 60 s interval + 60 s deviation) and, on a replica, both replica threads run. `null` where there is no heartbeat and no replica |
| `heartbeat_age_s` | age of the heartbeat row by this server's clock |
| `delay_ms` | replica only: commit on the source to applied here, for the last replicated transaction (MySQL's own timestamps in `performance_schema`) |
| `jitter_ms` | replica only: standard deviation of `delay_ms` over the last 30 heartbeats |
| `reason` | why `ok` is false |

Delay and jitter include any clock difference between the servers, so keep them time-synchronised.
Table name and limit are set in `[replication]` in `config.ini`.

## Built-in HTTP server

The collector embeds a lightweight HTTP server (Python `http.server`, stdlib-only) that serves all JSON files and `healthcheck.html` directly from `/dev/shm/10mila-sysmon/`.

| URL | Content |
|---|---|
| `http://192.168.1.21:8082/healthcheck.html` | Live dashboard |
| `http://192.168.1.21:8082/healthcheck.json` | Full metrics + 30 min history |
| `http://192.168.1.21:8082/services.json` | Compact per-service snapshot |
| `http://<online>:8082/statistics.html` | Unique devices on the online results (only where `[devices]` is on) |
| `http://<online>:8082/statistics.json` | The same as JSON |

No dependency on Jetty/Java. Port is configurable in `/etc/10mila-sysmon/config.ini` (`http_port = 8082`). Set to `0` to disable.

> `/dev/shm/10mila-sysmon/healthcheck.html` is cleared on reboot, but the service automatically copies it from `/opt/10mila-sysmon/healthcheck.html` (installed by `install.sh`) on every start via `ExecStartPre`. The copy is non-fatal — if the source is missing the service still starts.

## Unique devices (online results server)

Off by default; switched on with `[devices] socket = /dev/shm/10mila-sysmon/devices.sock` in `config.ini`
(the deploy does this on the online server only). NGINX gives every browser a cookie `tmdev`
(`ngx_http_userid_module`) and sends one syslog datagram per HTML page load to that unix socket:

```nginx
log_format devices escape=json '$uid_got$uid_set\t$msec\t$http_user_agent';
access_log syslog:server=unix:/dev/shm/10mila-sysmon/devices.sock,nohostname,tag=tmdev devices;
```

The collector binds the socket (mode 0666, so the nginx workers can send), counts devices in memory
(at most 50 000) and writes `statistics.json` every `interval` seconds:

```json
{"generated": 1760551990.1, "since": 1760550000.0, "devices": 2,
 "by_browser": {"Safari": 1, "Chrome": 1}, "by_os": {"iOS": 1, "Windows": 1},
 "new_per_hour": {"2026-10-15T18:00:00Z": 2},
 "list": {"<32 hex uid>": {"ua": "Mozilla/5.0 ...", "first": 1760551980.123, "last": 1760551990.0, "hits": 3}}}
```

Times are Unix seconds; `new_per_hour` counts devices by the UTC hour they were first seen. A service
restart keeps the count (it reads the old `statistics.json`); a reboot clears it. Malformed datagrams are
ignored. `python3 collector.py --selftest` checks the datagram parser and the browser/OS families.

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
- Replication: OK / ALARM box from a heartbeat row, heartbeat age, replica delay and jitter

MySQL and the OLA server are monitored only where they are installed. On a server without
one of them (for example the online web server) the collector does not poll it and the page
hides its boxes and graphs. Installed means: its process runs, or one of `installed_paths`
in `[mysql]` / `[ola]` in `config.ini` exists (defaults: the `mysqld` / `mariadbd` binary,
`/opt/OLA_6` or `ola10mila.service`).

**SSH tunnel** (when `probe_port` is set in `[tunnel]`)
- Status box in the System row: UP / DOWN from a TCP probe through the tunnel
- Round trip of the probe in ms, as a graph; failed probes show as a red band

The probe connects to `probe_host:probe_port` and waits for the far end's first byte.
Point it at a local port that ssh forwards to the tunnel's remote listening port
(`-L 127.0.0.1:33316:127.0.0.1:33306`), so the test goes out to the remote host and back
through the reverse tunnel.

**NGINX / PHP-FPM** (only while an nginx process is running on the server)
- NGINX: active connections, reading / writing / waiting, requests/s, accepts/s, dropped connections
- PHP-FPM: active / idle / total processes, listen queue, requests/s, slow requests, max children reached

The collector polls two local status pages, set in `[web]` in `config.ini`
(defaults `http://127.0.0.1:8080/nginx_status` and `http://127.0.0.1:8080/fpm_status`).
They must exist on the web server, on a loopback-only listener:

```nginx
server {
    listen 127.0.0.1:8080;
    location = /nginx_status { stub_status; }
    location = /fpm_status   { include fastcgi_params; fastcgi_param SCRIPT_NAME /fpm_status;
                               fastcgi_param SCRIPT_FILENAME /fpm_status; fastcgi_pass php-fpm; }
}
```

and in the PHP-FPM pool: `pm.status_path = /fpm_status`.

Check the parsers: `python3 collector.py --selftest`.

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
