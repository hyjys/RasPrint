#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

if (( EUID != 0 )); then
    echo 'Run with sudo: sudo bash deploy/hotspot.sh' >&2
    exit 1
fi

name=RasPrint-AP
ssid=RasPrint
profile_dir=/etc/NetworkManager/system-connections
profile="$profile_dir/rasprint-ap.nmconnection"

if [[ -e "$profile" ]] || nmcli connection show id "$name" >/dev/null 2>&1; then
    echo "The $name profile already exists. Use nmcli connection modify to update it." >&2
    exit 1
fi

if [[ $(nmcli -g WIFI-PROPERTIES.AP device show wlan0) != yes ]]; then
    echo 'NetworkManager did not report AP support for wlan0.' >&2
    exit 1
fi

IFS= read -r -s -p 'RasPrint Wi-Fi password (8-63 printable ASCII characters): ' passphrase
printf '\n'
if [[ ! "$passphrase" =~ ^[[:print:]]{8,63}$ ]]; then
    echo 'Enter 8-63 printable ASCII characters.' >&2
    exit 1
fi

# Store the PSK in keyfile-safe hexadecimal form without passing the password as an argument.
psk=$(printf '%s' "$passphrase" | /usr/bin/python3 -c '
import hashlib, sys
print(hashlib.pbkdf2_hmac("sha1", sys.stdin.buffer.read(), sys.argv[1].encode(), 4096, 32).hex())
' "$ssid")
unset passphrase

umask 077
temp_profile=$(mktemp "$profile_dir/.rasprint-ap.XXXXXXXX")
trap 'rm -f -- "$temp_profile"' EXIT
cat >"$temp_profile" <<EOF
[connection]
id=$name
uuid=$(< /proc/sys/kernel/random/uuid)
type=wifi
interface-name=wlan0
autoconnect=false
autoconnect-priority=100

[wifi]
band=bg
channel=6
mode=ap
powersave=2
ssid=$ssid

[wifi-security]
key-mgmt=wpa-psk
proto=rsn;
pairwise=ccmp;
group=ccmp;
# brcmfmac on Pi Zero W can fail to set the AP management key on Trixie.
pmf=1
psk=$psk

[ipv4]
method=shared
address1=10.42.0.1/24

[ipv6]
method=disabled
EOF

mv -- "$temp_profile" "$profile"
if ! nmcli connection load "$profile"; then
    rm -f -- "$profile"
    exit 1
fi
printf '\n%s\n' "Saved $name. The current Wi-Fi connection is still active." \
    "From a local console, run: sudo nmcli connection up id $name" \
    'Connect to RasPrint and open http://rasprint.local/ (or http://10.42.0.1:8000/).'
