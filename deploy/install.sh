#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
    echo 'Run with sudo: sudo bash deploy/install.sh [--offline]' >&2
    exit 1
fi

offline=no
if [[ ${1:-} == --offline ]]; then
    offline=yes
    shift
fi
if (( $# != 0 )); then
    echo 'Usage: sudo bash deploy/install.sh [--offline]' >&2
    exit 2
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
app_dir=/opt/rasprint
config_dir=/etc/rasprint
unit_dir=/etc/systemd/system

test -f "$source_dir/rasprint/main.py"
test -f "$source_dir/deploy/ap_helper.py"
test -f "$source_dir/deploy/cups_setup.py"
test -f "$source_dir/deploy/overlay_helper.py"
test -d "$source_dir/themes"
test -f "$source_dir/requirements.txt"

app_python="$app_dir/.venv/bin/python"
proxy_python="$app_dir/.proxy-venv/bin/python"
if [[ $offline == yes ]]; then
    if ! { [[ -f "$config_dir/rasprint.env" ]] &&
        command -v rsync >/dev/null &&
        "$app_python" -c 'import cups, fastapi, uvicorn, python_multipart' &&
        "$proxy_python" /usr/bin/mitmdump --version >/dev/null; }; then
        echo 'Offline updates require an existing RasPrint installation and working Python environments.' >&2
        exit 1
    fi
    if ! command -v qpdf >/dev/null; then
        echo 'Page selection will be unavailable until qpdf is installed. Rerun the installer with internet access.' >&2
    fi
    if ! "$app_python" -B -c 'import defusedxml, docx, openpyxl, pptx, reportlab' 2>/dev/null; then
        echo 'Python Office conversion dependencies are missing. Rerun the installer with internet access to add them.' >&2
    fi
else
    apt-get update
    apt-get install -y --no-install-recommends \
        cups cups-filters cups-ipp-utils python3-cups python3-venv printer-driver-hpcups \
        network-manager wpasupplicant dnsmasq-base nftables rsync usbutils \
        mitmproxy avahi-daemon python3-dbus
    # Prefer Raspberry Pi OS builds of the dependencies with native extensions.
    apt-get install -y --no-install-recommends \
        qpdf python3-lxml python3-pil python3-xlsxwriter fonts-nanum fonts-dejavu-core
    if command -v raspi-config >/dev/null || apt-cache show raspi-config >/dev/null 2>&1; then
        apt-get install -y --no-install-recommends raspi-config overlayroot
    fi
fi

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
        "$source_dir/deploy/cups_setup.py" "$source_dir/deploy/overlay_helper.py" \
        "$app_dir/deploy/"
    install -m 0644 "$source_dir/requirements.txt" "$app_dir/requirements.txt"
fi

if [[ $offline == no ]]; then
    /usr/bin/python3 -m venv --system-site-packages "$app_dir/.venv"
    "$app_python" -m pip install --only-binary=:all: -r "$app_dir/requirements.txt"

    # The apt build uses distro libraries. On Trixie, restore the compatibility
    # imports needed by mitmproxy 8 in its own virtual environment.
    /usr/bin/python3 -m venv --system-site-packages "$app_dir/.proxy-venv"
    if ! "$proxy_python" /usr/bin/mitmdump --version >/dev/null 2>&1; then
        "$proxy_python" -m pip install --only-binary=:all: \
            blinker==1.7.0 Flask==3.0.3 standard-imghdr==3.13.0
        "$proxy_python" /usr/bin/mitmdump --version >/dev/null
    fi
fi
(cd "$app_dir" && PYTHONDONTWRITEBYTECODE=1 "$app_python" -c 'import cups, rasprint.main')

install -m 0644 "$source_dir/deploy/rasprint.service" "$source_dir/deploy/rasprint.socket" \
    "$source_dir/deploy/rasprint-ap.service" "$source_dir/deploy/rasprint-ap.socket" \
    "$source_dir/deploy/rasprint-cups.service" "$source_dir/deploy/rasprint-cups.socket" \
    "$source_dir/deploy/rasprint-overlay.service" "$source_dir/deploy/rasprint-overlay.socket" \
    "$source_dir/deploy/rasprint-proxy.service" "$source_dir/deploy/rasprint-mdns.service" "$unit_dir/"
systemctl daemon-reload
systemctl enable --now cups.service
systemctl enable rasprint.socket rasprint-ap.socket rasprint-ap.service rasprint-proxy.service rasprint-mdns.service \
    rasprint-cups.socket rasprint-overlay.socket
systemctl restart rasprint-ap.socket rasprint.socket rasprint-cups.socket rasprint-overlay.socket
systemctl start rasprint-ap.service
systemctl restart rasprint-proxy.service rasprint-mdns.service
systemctl is-active --quiet rasprint-cups.socket
systemctl is-active --quiet rasprint-overlay.socket

printf '\n%s\n' 'RasPrint installed at /opt/rasprint.' \
    'Edit /etc/rasprint/rasprint.env, configure the CUPS queue, then set up the access point as described in README.md.'
