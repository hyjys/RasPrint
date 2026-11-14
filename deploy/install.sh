#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
    echo 'Run with sudo: sudo bash deploy/install.sh' >&2
    exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
app_dir=/opt/rasprint
config_dir=/etc/rasprint
unit_dir=/etc/systemd/system

test -f "$source_dir/rasprint/main.py"
test -f "$source_dir/deploy/ap_helper.py"
test -d "$source_dir/themes"
test -f "$source_dir/requirements.txt"

apt-get update
apt-get install -y --no-install-recommends \
    cups cups-filters cups-ipp-utils python3-cups python3-venv \
    network-manager wpasupplicant dnsmasq-base nftables rsync usbutils \
    mitmproxy avahi-daemon python3-dbus

if ! id rasprint >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/rasprint --shell /usr/sbin/nologin rasprint
fi
if ! id rasprint-proxy >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/rasprint-proxy --shell /usr/sbin/nologin rasprint-proxy
fi

install -d -m 0755 "$app_dir" "$app_dir/rasprint" "$app_dir/themes" "$app_dir/deploy"
install -d -m 0750 "$config_dir"
install -d -m 0755 "$unit_dir/rasprint.socket.d"
if [[ -f "$unit_dir/rasprint.socket.d/50-rasprint-ap.conf" ]]; then
    chmod 0644 "$unit_dir/rasprint.socket.d/50-rasprint-ap.conf"
fi
if [[ ! -e "$config_dir/rasprint.env" ]]; then
    install -m 0600 "$source_dir/deploy/rasprint.env.example" "$config_dir/rasprint.env"
fi

if [[ "$source_dir" != "$app_dir" ]]; then
    rsync -rltp --chmod=D755,F644 --delete --exclude=__pycache__/ \
        "$source_dir/rasprint/" "$app_dir/rasprint/"
    rsync -rltp --ignore-existing --chmod=D755,F644 "$source_dir/themes/" "$app_dir/themes/"
    install -m 0644 "$source_dir/deploy/ap_helper.py" "$source_dir/deploy/network.py" \
        "$source_dir/deploy/mdns.py" "$source_dir/deploy/proxy.py" "$source_dir/deploy/proxy_headers.py" \
        "$app_dir/deploy/"
    install -m 0644 "$source_dir/requirements.txt" "$app_dir/requirements.txt"
fi

/usr/bin/python3 -m venv --system-site-packages "$app_dir/.venv"
"$app_dir/.venv/bin/python" -m pip install --only-binary=:all: -r "$app_dir/requirements.txt"
"$app_dir/.venv/bin/python" -c 'import cups'

# The apt build uses distro libraries. On Trixie, restore the compatibility
# imports needed by mitmproxy 8 in its own virtual environment.
/usr/bin/python3 -m venv --system-site-packages "$app_dir/.proxy-venv"
proxy_python="$app_dir/.proxy-venv/bin/python"
if ! "$proxy_python" /usr/bin/mitmdump --version >/dev/null 2>&1; then
    "$proxy_python" -m pip install --only-binary=:all: \
        blinker==1.7.0 Flask==3.0.3 standard-imghdr==3.13.0
    "$proxy_python" /usr/bin/mitmdump --version >/dev/null
fi

install -m 0644 "$source_dir/deploy/rasprint.service" "$source_dir/deploy/rasprint.socket" \
    "$source_dir/deploy/rasprint-ap.service" "$source_dir/deploy/rasprint-ap.socket" \
    "$source_dir/deploy/rasprint-proxy.service" "$source_dir/deploy/rasprint-mdns.service" "$unit_dir/"
systemctl daemon-reload
systemctl enable --now cups.service
systemctl enable rasprint.socket rasprint-ap.socket rasprint-ap.service rasprint-proxy.service rasprint-mdns.service
systemctl restart rasprint-ap.socket rasprint.socket
systemctl start rasprint-ap.service
systemctl restart rasprint-proxy.service rasprint-mdns.service

printf '\n%s\n' 'RasPrint installed at /opt/rasprint.' \
    'Edit /etc/rasprint/rasprint.env, configure the CUPS queue, then set up the access point as described in README.md.'
