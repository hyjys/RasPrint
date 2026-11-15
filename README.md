# RasPrint

[한국어](README.ko.md)

RasPrint is a FastAPI server for printing to a USB printer attached to a Raspberry Pi Zero W. Connect a phone or computer to the Pi's Wi-Fi network, then upload a file from your browser. The interface and its fonts are served locally and work without internet access.

- Upload PDF, JPEG, PNG, or UTF-8 TXT files. Use a PDF with embedded fonts to preserve the original layout.
- View queued jobs, print progress, ink levels, and printer errors such as paper jams or an empty tray. The available readings depend on the printer and its CUPS driver.
- The admin menu lets you register USB printers and change the access point, HTTP port, upload limit, CUPS queue, admin password, and themes. Anyone joined to the Wi-Fi network can submit jobs and see their names and status.
- The interface supports English, Korean, Japanese, Simplified Chinese, Spanish, French, German, and Russian. Choose a language in the sidebar.

## Install on a Raspberry Pi Zero W

### 1. Prepare the Pi

Use Raspberry Pi Imager to install **Raspberry Pi OS Lite (32-bit)** with NetworkManager (Bookworm or Trixie). Plug power into `PWR IN` and connect the printer to the `USB` port through a micro-USB OTG adapter. Set the correct Wi-Fi country in Imager or with `sudo raspi-config`. [Raspberry Pi OS](https://www.raspberrypi.com/documentation/computers/os.html) · [Pi Zero W hardware](https://www.raspberrypi.com/documentation/computers/zero.html)

While the Pi has internet access, download or clone this repository and run the following from the project directory. The installer sets up CUPS, the system `python3-cups` package, mitmproxy, Avahi, and an unprivileged `rasprint` service under `/opt/rasprint`. Rerunning the installer preserves existing settings. Set the initial admin password before opening the access point. Its default is `rasprint`.

```bash
git clone https://github.com/hyjys/RasPrint.git
cd RasPrint
sudo bash deploy/install.sh
sudoedit /etc/rasprint/rasprint.env
```

If you already installed RasPrint, rerun `sudo bash deploy/install.sh` from the updated source while the Pi has internet access. The updated web service and AP helper are installed together.

For a Pi already running RasPrint on its own Wi-Fi, transfer the updated source from a computer and run `sudo bash deploy/install.sh --offline`. This reuses the existing Python environments. A missing CUPS driver or `overlayroot` still needs to be installed while the Pi has internet access. If Overlay FS is active, disable it and reboot before installing a permanent update.

### 2. Add a CUPS printer

After starting the access point in step 3, connect the powered-on printer to the Pi's USB data port using an OTG adapter. In RasPrint, sign in and open **Admin → Print settings → Register USB printer**. Choose **Find USB printers**, select the device and a driver, then register the queue. The main screen lists the new queue. If you previously restricted RasPrint to one queue, select the new queue or choose Automatic in Print settings.

If the scan finds a device with no compatible driver, install its CUPS driver on the Pi and scan again. The installer includes the HP hpcups driver for models such as the OfficeJet Pro 8710. If the scan finds no device, run `lsusb` to confirm the USB connection and `sudo lpinfo -v` to inspect device discovery. The CLI procedures below also let you add a queue before switching to the access point. [CUPS administration](https://openprinting.github.io/cups/doc/admin.html)

#### HP OfficeJet Pro 8710

HP lists USB support with HPLIP 3.16.5 and later. This model can print without an additional HP plug-in. Replace the example USB address `001:004` with the bus and device numbers shown by `lsusb`. Check the address again if you reconnect the cable. [HP supported models](https://developers.hp.com/hp-linux-imaging-and-printing/supported_devices/index) · [HP setup tool](https://developers.hp.com/hp-linux-imaging-and-printing/tech_docs/man_pages/setup)

```bash
sudo apt install --no-install-recommends hplip printer-driver-hpcups
lsusb
sudo hp-setup -i -a -x -tprint -php8710 001:004
sudo lpadmin -p hp8710 -o printer-is-shared=false
sudo lpadmin -d hp8710
```

If `ipp-usb` already manages the printer, you can use its IPP-over-USB queue. To use HPLIP instead, add the printer's hexadecimal USB ID from `lsusb` as a section (for example, `[xxxx:yyyy]`) in `/etc/ipp-usb/quirks/8710.conf`, set `blacklist = true` under it, and reconnect the printer. [ipp-usb device configuration](https://github.com/OpenPrinting/ipp-usb/blob/master/ipp-usb.8.scdoc)

#### IPP-over-USB printers

Run the following to check whether the printer supports IPP-over-USB. Reconnect it and use the port from `ipp-usb status` when creating the queue. The command uses `60000` as an example. [OpenPrinting ipp-usb](https://github.com/OpenPrinting/ipp-usb) · [CUPS lpadmin](https://openprinting.github.io/cups/cups-local/lpadmin.html)

```bash
sudo apt install ipp-usb avahi-daemon
sudo ipp-usb check
sudo ipp-usb status
sudo lpadmin -p usbprinter -E -v ipp://localhost:60000/ipp/print -m everywhere -o printer-is-shared=false
sudo lpadmin -d usbprinter
```

#### Verify the queue

Select a queue in the web admin menu or let users choose among registered queues. The web interface displays jobs submitted by the RasPrint service account. To test a queue from the Pi console, submit a PDF as that account.

```bash
sudo -u rasprint lp -d hp8710 /tmp/test.pdf
lpstat -W all -o hp8710
```

On a dedicated print server, you can retain completed job history while letting CUPS discard the source files after printing. This setting applies to all queues on the CUPS server. [CUPS job history and file retention](https://openprinting.github.io/cups/doc/man-cupsd.conf.html)

```bash
sudo cupsctl PreserveJobHistory=Yes PreserveJobFiles=No
```

### 3. Start the Wi-Fi access point

Run the hotspot setup script to save a WPA2 configuration for the built-in 2.4 GHz `wlan0` interface. Choose a Wi-Fi passphrase of 8–63 printable ASCII characters. The profile disables PMF for compatibility with the Zero W's Trixie driver. [Raspberry Pi hotspot guide](https://www.raspberrypi.com/tutorials/host-a-hotel-wifi-hotspot/) · [Zero W Trixie AP issue](https://github.com/raspberrypi/linux/issues/7247)

```bash
sudo bash deploy/hotspot.sh
```

**Activate the profile from a local console or a separate wired or serial connection.** This replaces the Pi's current Wi-Fi connection and disconnects any SSH session using it.

```bash
sudo nmcli connection up id RasPrint-AP
ip -4 addr show wlan0
```

Join the **RasPrint** Wi-Fi network and open [http://rasprint.local/](http://rasprint.local/). Avahi advertises the name on the access point. mitmproxy forwards HTTP on port 80 to RasPrint. If your client cannot resolve the name, open [http://10.42.0.1:8000/](http://10.42.0.1:8000/) or use the direct port you configured. If prompted, keep the Wi-Fi connection despite the lack of internet. After verifying access, enable the AP at boot. [mitmproxy reverse mode](https://docs.mitmproxy.org/stable/concepts/modes/#reverse-proxy)

```bash
sudo nmcli connection modify id RasPrint-AP connection.autoconnect yes
```

To return to your previous Wi-Fi network, use the Pi console and replace the example profile name. Run `nmcli connection show` to list profile names.

```bash
sudo nmcli connection modify id RasPrint-AP connection.autoconnect no
sudo nmcli connection down id RasPrint-AP
sudo nmcli connection up id 'Previous Wi-Fi profile'
```

## Admin settings

Sign in from the **Admin** menu to change:

- **Print settings.** Set a per-file upload limit of 1–100 MiB and choose a CUPS queue or allow users to choose from all configured queues.
- **Wi-Fi and HTTP.** Set a network name, enable or disable WPA2, and set the Wi-Fi password. Enter a password when enabling WPA2 or renaming a protected Wi-Fi network. The direct HTTP port can be any available port from 1 to 65535 except 80, which serves the local web address. An open AP allows nearby users to join and submit print jobs.
- **Local web address.** Enter a name such as `office-print` to use `http://office-print.local/` on the RasPrint Wi-Fi. Use 1–63 ASCII letters, digits, or hyphens. The first and last characters must be letters or digits. Changing just this name keeps the Wi-Fi connection up. Open the new address shown after saving and sign in again. The direct IP address and configured port also work. With Raspberry Pi OS's RAM-backed Overlay FS, the change lasts until reboot. [Overlay FS behavior](https://www.raspberrypi.com/documentation/computers/configuration.html#enable-or-disable-overlay-file-system)
- **Overlay file system.** Change the root file system setting for the next boot. After saving, choose whether to restart now or later. Finish any print jobs before restarting. While Raspberry Pi OS uses its RAM-backed overlay, changes to printer queues, passwords, AP settings, and themes are lost at reboot. Configure these before turning Overlay FS on. [Raspberry Pi Overlay FS](https://www.raspberrypi.com/documentation/computers/configuration.html#enable-or-disable-overlay-file-system)
- **Admin password.** Enter the current password and a new password of 8–1024 UTF-8 bytes. Saving revokes every admin session. RasPrint stores a salted password hash in its state directory.

Use `sudoedit /etc/rasprint/rasprint.env` and `sudo systemctl restart rasprint.service` to configure defaults. Saved print settings and the saved admin password take precedence over the environment. To recover the admin account from the Pi console, set a new value for `RASPRINT_ADMIN_PASSWORD` in the environment file, stop `rasprint.service`, remove `/var/lib/rasprint/admin-password`, and start the service again.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `RASPRINT_ADMIN_PASSWORD` | `rasprint` | Initial password for the web admin menu |
| `RASPRINT_PRINTER` | Unset | Initial CUPS queue restriction |
| `RASPRINT_MAX_UPLOAD_MB` | `25` | Initial per-file upload limit, from 1 to 100 MiB |
| `RASPRINT_THEME` | `raspberry` | Initial theme ID, matching the JSON filename without its extension |
| `RASPRINT_THEMES_DIR` | `/opt/rasprint/themes` | Absolute path to the theme directory |
| `RASPRINT_STATE_DIR` | `/var/lib/rasprint` | State directory created by systemd, or `.rasprint` during local runs |
| `RASPRINT_DEMO` | `false` | Set to `true` to run with a simulated print queue |
| `RASPRINT_SECURE_COOKIE` | `false` | Set to `true` when serving over HTTPS |

### Custom themes and logos

Use the **Theme editor** in the admin menu to create a theme, preview its name, logo and colors, or edit or remove an existing custom theme. You can also import a JSON file with a separate logo. Removing the active theme switches back to Raspberry.

To install a theme from the Pi console, add `my-theme.json` and the logo file to `/opt/rasprint/themes` and select it in the admin menu. To specify an initial theme, set `RASPRINT_THEME=my-theme`.

```json
{
  "name": "나의 테마",
  "name_en": "My theme",
  "brand": {"name": "RasPrint", "logo": "my-logo.png"},
  "colors": {
    "primary": "#A51C50",
    "primary_hover": "#83183F",
    "primary_light": "#FBE9F0",
    "primary_text": "#FFFFFF"
  }
}
```

`name_en` is the optional English theme label. Other localized labels use `name_ja`, `name_zh-CN`, `name_es`, `name_fr`, `name_de`, `name_ru`, or `name_ko`. Use `#RRGGBB` colors and a PNG, JPEG, or WebP logo. In a theme installed from the console, the logo path is relative to the theme directory. Omitted colors use `DEFAULT_COLORS` from [rasprint/themes.py](rasprint/themes.py).

## Try locally without a printer

Use Python 3.11–3.13 and open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) after starting the demo server. Demo mode simulates printing and access point changes.

Windows PowerShell

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:RASPRINT_DEMO = "true"
.\.venv\Scripts\python.exe -m uvicorn rasprint.main:app --host 127.0.0.1 --port 8000
```

Linux or macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
RASPRINT_DEMO=true .venv/bin/python -m uvicorn rasprint.main:app --host 127.0.0.1 --port 8000
```

## Troubleshooting

```bash
systemctl status rasprint.socket rasprint.service cups.service
journalctl -u rasprint-ap.service -u rasprint-proxy.service -u rasprint-mdns.service -u rasprint-cups.service -u rasprint-overlay.service -n 50 --no-pager
journalctl -u rasprint.service -n 50 --no-pager
journalctl -u cups.service -n 50 --no-pager
lpstat -t
nmcli device status
```

- `rasprint.socket` starts the web server on the first connection.
- If the local web address does not resolve, use the direct IP and configured port. Check the mDNS service log and try a unique name if another device on the network already advertises it.
- If the AP is missing, check the Wi-Fi country, `nmcli radio wifi`, and `nmcli -g WIFI-PROPERTIES.AP device show wlan0`.
- If a queue remains stopped after you clear a jam or refill paper, inspect it with `lpstat -p` and use `sudo cupsenable hp8710` with the appropriate queue name.
- RasPrint marks a job complete when CUPS reports completion. Some USB drivers report completion before the last sheet leaves the printer.
