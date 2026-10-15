#!/usr/bin/env bash
# install.sh — Deploy 10mila System + MySQL Monitor on Ubuntu 24.04
#
# Run as root:   sudo bash install.sh
# Uninstall:     sudo bash install.sh --uninstall
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/10mila-sysmon"
CONFIG_DIR="/etc/10mila-sysmon"
OUTPUT_DIR="/dev/shm/10mila-sysmon"
SERVICE_NAME="10mila-sysmon"
SERVICE_USER="sysmon"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[install]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}   $*"; }
error() { echo -e "${RED}[error]${NC}  $*"; exit 1; }

# ─── Root check ──────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || error "Run as root: sudo bash install.sh"

# ─── Uninstall ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    info "Stopping and disabling service..."
    systemctl stop  "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    info "Removing files..."
    rm -rf "$INSTALL_DIR"
    rm -rf "$CONFIG_DIR"
    userdel "$SERVICE_USER" 2>/dev/null || true
    info "Uninstall complete."
    exit 0
fi

# ─── Dependency check ────────────────────────────────────────────────────────
info "Checking dependencies..."
command -v python3 >/dev/null 2>&1 || error "python3 not found. Install with: apt install python3"
python3 -c "import json, subprocess, pathlib" 2>/dev/null || error "python3 stdlib missing"

# lsof is optional but recommended
if ! command -v lsof >/dev/null 2>&1; then
    warn "lsof not found — install with: apt install lsof"
    warn "The collector will fall back to /proc/sys/fs/file-nr (kernel count only)."
fi

# ─── Create system user ───────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    info "Creating system user '$SERVICE_USER'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi


# ─── Install files ────────────────────────────────────────────────────────────
info "Installing collector to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/collector.py" "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR/collector.py"
for page in healthcheck.html statistics.html; do
    if [[ -f "$SCRIPT_DIR/$page" ]]; then cp "$SCRIPT_DIR/$page" "$INSTALL_DIR/"; fi
done
chown -R root:root "$INSTALL_DIR"

# ─── Config ───────────────────────────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
chmod 750 "$CONFIG_DIR"

CONFIG_FILE="$CONFIG_DIR/config.ini"
if [[ ! -f "$CONFIG_FILE" ]]; then
    info "Writing default config to $CONFIG_FILE ..."
    cat > "$CONFIG_FILE" <<'EOF'
[monitor]
# Seconds between samples (lower = finer resolution, higher CPU cost)
interval      = 5

# How many samples to keep in history (5s × 360 = 30 min)
history_size  = 360

# Where to write the JSON (should be on tmpfs for low latency)
output_dir    = /dev/shm/10mila-sysmon
output_file   = healthcheck.json
# Compact per-service snapshot (no history) — used by monitoring_js / statistics_js
services_file = services.json

# Built-in lightweight HTTP server (0 = disabled).
# Serves all JSON files directly from output_dir — no Jetty/Java required.
# Access: http://<host>:8082/healthcheck.json  and  /services.json
http_port     = 8082

# Optional second copy of the JSON for another web server (blank = disabled).
publish_dir   =

# Set to false if lsof is too slow on this system
lsof_enabled  = true

[mysql]
# Path to MySQL option file with [client] user/password
defaults_file = /etc/10mila-sysmon/mysql.cnf
host          = 127.0.0.1
port          = 3306
# MySQL is monitored only where it is installed: one of these paths exists or mysqld runs.
# installed_paths = /usr/sbin/mysqld /usr/libexec/mysqld /usr/sbin/mariadbd /usr/libexec/mariadbd

[replication]
# Heartbeat: a table (k, v) whose row k='heartbeat' the source rewrites with UTC_TIMESTAMP every 60 s.
# Replication shows ALARM when the row is older than max_age_s, or when a replica thread has stopped.
# heartbeat_table = replmon.replicamonitoring
# max_age_s       = 120

[ola]
# The OLA server is monitored only where it is installed: one of these paths exists or it runs.
# installed_paths = /opt/OLA_6 /etc/systemd/system/ola10mila.service

[tunnel]
# autossh monitor port (-M flag in ssh_reverse_tunnel.sh). Set to 0 to disable.
# When the tunnel is active, autossh creates a LISTEN socket on this port via -L.
# The collector checks /proc/net/tcp for that socket — no external tools needed.
monitor_port  = 20000
service_name  = ssh_reverse_tunnel
# TCP probe of the tunnel, shown as the "SSH tunnel" box and round-trip graph.
# 0 = off. Point it at a local port that ssh forwards to the tunnel's remote
# listening port, e.g. -L 127.0.0.1:33316:127.0.0.1:33306 -> probe_port = 33316.
probe_host    = 127.0.0.1
probe_port    = 0
probe_timeout_s = 3

[web]
# NGINX and PHP-FPM statistics. Polled only while an nginx process is running.
# nginx_url: a stub_status location; fpm_url: the PHP-FPM status page (pm.status_path).
# Keep both on a loopback-only listener. Leave a value empty to skip that page;
# leave both empty to switch the web statistics off.
nginx_url     = http://127.0.0.1:8080/nginx_status
fpm_url       = http://127.0.0.1:8080/fpm_status

[devices]
# Unique devices on the online results site -> statistics.json and statistics.html. Web server only:
# nginx sends one syslog datagram per HTML page load to this unix socket. Empty = off.
socket          =
statistics_file = statistics.json
EOF
    chmod 640 "$CONFIG_FILE"
    chown root:"$SERVICE_USER" "$CONFIG_FILE"
else
    info "Config already exists — skipping (delete $CONFIG_FILE to regenerate)"
fi

# MySQL credentials file
MYSQL_CNF="$CONFIG_DIR/mysql.cnf"
if [[ ! -f "$MYSQL_CNF" ]]; then
    info "Writing MySQL credential template to $MYSQL_CNF ..."
    cat > "$MYSQL_CNF" <<'EOF'
# /etc/10mila-sysmon/mysql.cnf
# Credentials for the read-only monitoring user.
#
# Create the user in MySQL/MariaDB:
#   CREATE USER 'sysmon'@'127.0.0.1' IDENTIFIED BY 'STRONG_PASSWORD_HERE';
#   GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'sysmon'@'127.0.0.1';
#   FLUSH PRIVILEGES;
#
[client]
user     = sysmon
password = ****************
EOF
    chmod 640 "$MYSQL_CNF"
    chown root:"$SERVICE_USER" "$MYSQL_CNF"
    warn "Edit $MYSQL_CNF and set the correct MySQL password before starting."
else
    info "MySQL config already exists — skipping"
fi

# ─── Output directory (tmpfs /dev/shm) ───────────────────────────────────────
info "Creating output directory $OUTPUT_DIR ..."
mkdir -p "$OUTPUT_DIR"
chown "$SERVICE_USER":"$SERVICE_USER" "$OUTPUT_DIR"
chmod 755 "$OUTPUT_DIR"

# Note: /dev/shm is cleared on reboot — the service re-creates the dir on start.
# Persist across reboot by adding to /etc/tmpfiles.d:
TMPFILES_CONF="/etc/tmpfiles.d/10mila-sysmon.conf"
cat > "$TMPFILES_CONF" <<EOF
# Created by 10mila-sysmon install.sh
d /dev/shm/10mila-sysmon 0755 $SERVICE_USER $SERVICE_USER -
EOF
info "Registered tmpfiles.d entry: $TMPFILES_CONF"

# ─── Install HTML dashboard ───────────────────────────────────────────────────
# Copy to the built-in HTTP server root (tmpfs, re-created on each install/reboot).
# /etc/tmpfiles.d ensures the directory exists after reboot; the service
# copies the pages again on every start (ExecStartPre).
for page in healthcheck.html statistics.html; do
    [[ -f "$SCRIPT_DIR/$page" ]] || continue
    info "Copying $page to $OUTPUT_DIR ..."
    mkdir -p "$OUTPUT_DIR"
    cp "$SCRIPT_DIR/$page" "$OUTPUT_DIR/"
    chown "$SERVICE_USER":"$SERVICE_USER" "$OUTPUT_DIR/$page"
    chmod 644 "$OUTPUT_DIR/$page"
done

# ─── systemd service ─────────────────────────────────────────────────────────
info "Installing systemd service..."
cp "$SCRIPT_DIR/10mila-sysmon.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    info "Service is running."
    echo
    info "Output:   $OUTPUT_DIR/healthcheck.json"
    info "Dashboard: http://$(hostname -I | awk '{print $1}'):8082/healthcheck.html"
    echo
    info "Useful commands:"
    echo "    journalctl -u $SERVICE_NAME -f          # live logs"
    echo "    systemctl status $SERVICE_NAME           # status"
    echo "    cat /dev/shm/10mila-sysmon/healthcheck.json | python3 -m json.tool | head -60"
else
    warn "Service did not start — check logs:"
    echo "    journalctl -u $SERVICE_NAME -n 30 --no-pager"
    exit 1
fi
