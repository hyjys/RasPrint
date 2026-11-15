# RasPrint

[English](README.md)

RasPrint는 Raspberry Pi Zero W에 USB 프린터를 연결해 브라우저에서 인쇄하는 FastAPI 서버입니다. 휴대폰이나 PC를 Pi의 Wi-Fi에 연결하면 문서를 보내고 인쇄 대기열과 프린터 상태를 확인할 수 있습니다.

- PDF, Word, PowerPoint, Excel, JPEG, PNG, UTF-8 TXT를 인쇄할 수 있습니다. PDF와 Office 문서는 인쇄할 페이지를 지정할 수 있습니다.
- 진행률, 잉크 잔량, 용지 부족과 걸림은 프린터 및 CUPS 드라이버가 보고하는 값을 표시합니다. 장치에서 값을 제공하지 않는 항목은 확인할 수 없음으로 표시합니다.
- 관리자 메뉴는 비밀번호로 보호됩니다. AP에 접속한 사용자는 파일을 인쇄하고 작업 이름과 상태를 볼 수 있으므로 Wi-Fi 비밀번호는 사용할 사람에게만 공유하세요.
- 영어, 한국어, 일본어, 중국어(간체), 스페인어, 프랑스어, 독일어, 러시아어를 지원합니다. 왼쪽 메뉴에서 표시 언어를 바꿀 수 있습니다.

## Raspberry Pi Zero W에 설치

### 1. Pi 준비

Raspberry Pi Imager에서 **Raspberry Pi OS Lite (32-bit)**를 설치합니다. Bookworm 또는 Trixie의 NetworkManager 구성을 사용합니다. Pi의 `PWR IN` 단자에 전원을 연결하고, `USB` 단자에 OTG 어댑터와 프린터 USB 케이블을 연결합니다. AP로 전환하기 전 Pi의 Wi-Fi 국가를 Imager 또는 `sudo raspi-config`에서 실제 사용 국가로 설정하세요. [Raspberry Pi OS](https://www.raspberrypi.com/documentation/computers/os.html) · [Zero W 단자 안내](https://www.raspberrypi.com/documentation/computers/zero.html)

인터넷이 연결된 상태에서 프로젝트 전체를 Pi로 복사합니다. Pi에서 프로젝트 폴더로 이동한 뒤 다음을 실행합니다.

```bash
sudo bash deploy/install.sh
sudoedit /etc/rasprint/rasprint.env
```

설치 스크립트가 CUPS와 Raspberry Pi OS용 `python3-cups`를 설치하고, `/opt/rasprint`에 시스템 패키지를 볼 수 있는 가상환경을 만듭니다. 시스템 서비스는 `rasprint` 계정으로 실행됩니다. 기본 관리자 비밀번호는 `rasprint`입니다. 사용을 시작하기 전에 설정 파일의 `RASPRINT_ADMIN_PASSWORD` 값을 바꿉니다. 설치를 다시 실행해도 설정 파일과 인쇄 서비스에 등록된 프린터는 유지됩니다.

기존 설치를 업데이트하려면 Pi를 인터넷에 연결한 상태에서 최신 소스의 `sudo bash deploy/install.sh`를 실행하세요. 웹 서비스와 AP 설정 서비스가 함께 업데이트됩니다.

Pi가 자체 AP로 동작 중이면 PC에서 최신 소스를 복사한 뒤 `sudo bash deploy/install.sh --offline`로 업데이트할 수 있습니다. 기존 Python 환경을 사용합니다. 프린터 드라이버나 `overlayroot`가 빠져 있다면 Pi가 인터넷에 연결된 상태에서 설치해야 합니다. Overlay FS가 켜져 있다면 업데이트를 유지하려면 먼저 끄고 재부팅하세요.

기존 설치에 Office 변환과 페이지 선택을 추가하려면 인터넷에 연결된 상태에서 설치 스크립트를 한 번 실행해 Python 패키지와 `qpdf`를 설치하세요.

### 2. CUPS 프린터 연결

3단계에서 AP를 시작한 뒤 전원을 켠 프린터를 Pi의 USB 데이터 단자에 연결하세요. **관리자 → 인쇄 설정 → USB 프린터 등록**에서 **USB 프린터 찾기**를 누르고, 장치와 드라이버를 선택해 큐를 등록합니다. 기존에 특정 큐로 제한했다면 인쇄 설정에서 새 큐를 선택하거나 자동으로 바꾸세요.

설치 스크립트에는 HP OfficeJet Pro 8710을 지원하는 hpcups 드라이버가 포함됩니다. 장치는 보이지만 드라이버가 없으면 Pi에 해당 CUPS 드라이버를 설치한 뒤 다시 검색하세요. 장치가 아예 보이지 않거나 AP로 전환하기 전에 큐를 등록하려면 아래 명령을 사용합니다.

```bash
lsusb
sudo lpinfo -v
lpstat -p -d
```

`lsusb`에서 프린터가 보이면 아래의 설치 방법 중 장치에 맞는 것을 선택합니다. 다른 제조사 프린터도 CUPS에 등록한 뒤 같은 방법으로 큐를 지정할 수 있습니다. [CUPS 프린터 관리](https://openprinting.github.io/cups/doc/admin.html)

#### HP OfficeJet Pro 8710 USB

HP는 이 모델의 USB 출력을 HPLIP 3.16.5부터 지원하며 별도 플러그인을 요구하지 않습니다. 인터넷이 연결된 동안 Raspberry Pi OS 저장소에서 드라이버를 설치합니다. [HP 지원 모델](https://developers.hp.com/hp-linux-imaging-and-printing/supported_devices/index)

```bash
sudo apt install --no-install-recommends hplip printer-driver-hpcups
lsusb
# 예를 들어 Bus 001 Device 004라면 아래에 001:004를 입력합니다.
sudo hp-setup -i -a -x -tprint -php8710 001:004
sudo lpadmin -p hp8710 -o printer-is-shared=false
sudo lpadmin -d hp8710
```

기존 `ipp-usb`가 동일한 프린터의 USB 인터페이스를 점유하고 있다면 아래의 IPP-over-USB 큐를 그대로 사용할 수 있습니다. HPLIP을 사용하려면 `/etc/ipp-usb/quirks/8710.conf`에 `lsusb`의 장치 ID를 섹션 이름으로 적고 그 아래에 `blacklist = true`를 추가한 뒤 프린터를 다시 연결합니다. 장치를 다시 연결하면 `hp-setup`의 USB 번호가 바뀔 수 있습니다. [HP 설정 명령](https://developers.hp.com/hp-linux-imaging-and-printing/tech_docs/man_pages/setup) · [ipp-usb 장치별 설정](https://github.com/OpenPrinting/ipp-usb/blob/master/ipp-usb.8.scdoc)

#### IPP-over-USB를 지원하는 프린터

```bash
sudo apt install ipp-usb avahi-daemon
sudo ipp-usb check
sudo ipp-usb status
sudo lpinfo -v
```

IPP-over-USB를 지원하는 장치는 `ipp-usb check`에 나타납니다. 재연결한 뒤 `ipp-usb status`에 표시된 포트를 사용해 큐를 등록합니다. 다음 명령의 `60000`은 예시 포트입니다. [OpenPrinting ipp-usb](https://github.com/OpenPrinting/ipp-usb) · [CUPS lpadmin](https://openprinting.github.io/cups/cups-local/lpadmin.html)

```bash
sudo lpadmin -p usbprinter -E -v ipp://localhost:60000/ipp/print -m everywhere -o printer-is-shared=false
sudo lpadmin -d usbprinter
```

#### 큐 확인

설정 파일에서 `RASPRINT_PRINTER`를 비워 두면 웹에서 등록된 큐를 선택할 수 있습니다. 단일 큐로 제한하려면 `RASPRINT_PRINTER=hp8710`과 같이 지정합니다. 아래 명령의 큐 이름과 파일 경로를 실제 값으로 바꾸면 서비스 계정으로 시험 인쇄하고 결과를 확인할 수 있습니다.

```bash
sudo -u rasprint lp -d hp8710 /tmp/test.pdf
lpstat -p -d
lpstat -W all -o hp8710
```

웹 대기열은 서비스 계정 `rasprint`로 제출한 작업을 표시합니다. 완료한 작업 목록은 CUPS 이력 보존 설정의 영향을 받습니다. 전용 프린트 서버에서는 다음과 같이 이력을 남기고 인쇄한 문서 파일은 CUPS가 작업 처리 후 지우게 할 수 있습니다. 이 설정은 해당 CUPS 서버의 모든 큐에 적용됩니다. [CUPS 이력 보존 설정](https://openprinting.github.io/cups/doc/man-cupsd.conf.html)

```bash
sudo cupsctl PreserveJobHistory=Yes PreserveJobFiles=No
```

### 3. 독립 Wi-Fi AP 시작

Pi의 기본 무선 인터페이스 `wlan0`에서 2.4 GHz WPA2 네트워크를 만듭니다. `hotspot.sh`는 Wi-Fi 비밀번호를 물어보고 연결 프로필을 저장합니다. 비밀번호는 8~63자의 출력 가능한 ASCII 문자로 설정하세요. Pi Zero W의 Trixie 무선 드라이버 호환성을 위해 PMF를 비활성화한 WPA2 프로필을 사용합니다. [Raspberry Pi 핫스팟 안내](https://www.raspberrypi.com/tutorials/host-a-hotel-wifi-hotspot/) · [Zero W의 Trixie AP 이슈](https://github.com/raspberrypi/linux/issues/7247)

```bash
sudo bash deploy/hotspot.sh
```

아래 AP 전환 명령을 실행하면 Pi가 기존 Wi-Fi에서 나갑니다. **이 단계는 Pi에 연결한 키보드·모니터 또는 별도 유선·시리얼 접속에서 실행하세요.**

```bash
sudo nmcli connection up id RasPrint-AP
ip -4 addr show wlan0
```

휴대폰 또는 PC를 **RasPrint** Wi-Fi에 연결하고 [http://rasprint.local/](http://rasprint.local/)을 엽니다. Avahi가 AP에 이름을 알리고, mitmproxy가 80번 포트의 HTTP 요청을 RasPrint에 전달합니다. 이름이 열리지 않으면 [http://10.42.0.1:8000/](http://10.42.0.1:8000/)이나 설정한 직접 포트로 접속하세요. 인터넷 없음 알림이 떠도 연결을 유지하면 인쇄 화면과 폰트를 쓸 수 있습니다. [mitmproxy 리버스 프록시](https://docs.mitmproxy.org/stable/concepts/modes/#reverse-proxy)

페이지에 접근할 수 있는지 확인한 뒤 아래와 같이 부팅 시 자동 연결을 켭니다.

```bash
sudo nmcli connection modify id RasPrint-AP connection.autoconnect yes
```

기존 Wi-Fi로 돌아가려면 Pi 콘솔에서 AP 자동 연결을 끄고 원래 연결 이름을 지정해 다시 활성화합니다. 프로필 이름은 `nmcli connection show`에서 확인할 수 있습니다.

```bash
sudo nmcli connection modify id RasPrint-AP connection.autoconnect no
sudo nmcli connection down id RasPrint-AP
sudo nmcli connection up id '기존 Wi-Fi 프로필 이름'
```

## Office 문서와 특정 페이지 인쇄

RasPrint가 변환기를 자동으로 선택합니다. LibreOffice의 Writer, Calc, Impress가 설치되어 있으면 `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx`를 변환할 때 LibreOffice를 사용합니다. 기본 설치에 포함된 Python 변환기는 LibreOffice가 없을 때 python-docx, python-pptx, openpyxl, ReportLab으로 `.docx`, `.pptx`, `.xlsx`를 처리합니다. Python 모드에서는 문서 배치나 페이지 나눔이 원본과 다를 수 있으며, Excel 수식은 파일에 저장된 계산 결과를 사용합니다. 원본 배치를 그대로 인쇄하려면 PDF로 내보낸 뒤 올리세요.

LibreOffice를 사용하려면 인터넷에 연결된 Pi에서 다음을 실행하세요.

```bash
sudo apt install --no-install-recommends libreoffice-writer libreoffice-calc libreoffice-impress
```

문서를 고른 다음 **인쇄할 페이지**에 `1,3-5,8`처럼 입력합니다. Office 문서의 페이지 번호는 변환된 PDF 기준이고, PowerPoint는 변환된 슬라이드 한 장이 한 페이지입니다. 비워 두면 전체를 인쇄합니다. CUPS가 현재 페이지 번호만 알려줄 때는 PDF와 Office 문서의 인쇄 페이지 수와 매수를 바탕으로 진행률을 추정해 표시합니다.

## 관리자 설정

관리자 메뉴에서 파일당 최대 업로드 크기(1~100 MiB)와 사용할 CUPS 대기열을 설정할 수 있습니다. AP 이름, WPA2 사용 여부, Wi-Fi 비밀번호와 직접 HTTP 포트도 바꿀 수 있습니다. 80번은 `.local` 웹 주소에 사용됩니다. WPA2를 켜거나 암호를 쓰는 AP의 이름을 바꿀 때는 비밀번호를 입력하세요.

**로컬 웹 주소**에 `office-print`를 입력하면 RasPrint Wi-Fi에서 `http://office-print.local/`로 접속할 수 있습니다. 영문, 숫자, 하이픈을 1~63자 쓸 수 있으며 첫 글자와 마지막 글자는 영문이나 숫자여야 합니다. 웹 주소만 바꾸면 Wi-Fi 연결이 유지됩니다. 화면에 안내된 새 주소를 열고 다시 로그인하세요. 기존 IP와 직접 포트로도 계속 접속할 수 있습니다. Raspberry Pi OS의 기본 Overlay FS를 켠 동안 바꾼 주소는 재부팅하면 이전 값으로 돌아갑니다. [Overlay FS 동작](https://www.raspberrypi.com/documentation/computers/configuration.html#enable-or-disable-overlay-file-system)

**Overlay FS**에서 다음 부팅에 적용할 설정을 바꿀 수 있습니다. 저장하면 지금 재시작할지 나중에 할지 묻습니다. 인쇄 작업이 끝난 뒤 재시작하세요. RAM 기반 Overlay FS가 켜져 있는 동안 바꾼 프린터 큐, 비밀번호, AP 설정과 테마는 재부팅할 때 사라지므로 Overlay FS를 켜기 전에 설정하세요. [Raspberry Pi Overlay FS](https://www.raspberrypi.com/documentation/computers/configuration.html#enable-or-disable-overlay-file-system)

관리자 비밀번호는 현재 비밀번호를 확인한 뒤 변경합니다. 변경하면 모든 관리자 세션이 만료됩니다. 초기 설정은 `sudoedit /etc/rasprint/rasprint.env`로 바꾼 뒤 `sudo systemctl restart rasprint.service`를 실행합니다. 웹에서 저장한 인쇄 설정과 관리자 비밀번호가 초기값에 우선합니다. 관리자 암호를 잊었다면 Pi 콘솔에서 환경 파일에 새 비밀번호를 설정하고, 서비스를 중지한 뒤 `/var/lib/rasprint/admin-password`를 삭제하고 서비스를 다시 시작하세요.

| 환경 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `RASPRINT_ADMIN_PASSWORD` | `rasprint` | 웹 관리자 메뉴 비밀번호 |
| `RASPRINT_PRINTER` | 비어 있음 | 지정할 CUPS 큐 이름 |
| `RASPRINT_MAX_UPLOAD_MB` | `25` | 파일 하나의 업로드 최대 크기, 1~100 MiB |
| `RASPRINT_THEME` | `raspberry` | 초기 테마 이름, 사용자 정의 테마는 JSON 파일명에서 확장자를 뺀 값 |
| `RASPRINT_THEMES_DIR` | `/opt/rasprint/themes` | 테마 폴더, 별도 경로 사용 시 절대 경로 |
| `RASPRINT_STATE_DIR` | `/var/lib/rasprint` | systemd에서 만드는 앱 상태 디렉터리, 로컬 실행 시 프로젝트 내 `.rasprint` |
| `RASPRINT_DEMO` | `false` | 실제 CUPS 없이 테스트할 때 `true` |
| `RASPRINT_SECURE_COOKIE` | `false` | HTTPS로 제공하는 환경에서 `true` |

인쇄할 때는 브라우저에서 파일과 인쇄 옵션을 선택하고 대기열을 확인합니다. 작업 취소가 필요하면 관리자 메뉴에 로그인합니다.

### 테마와 로고 교체

관리자 메뉴의 테마 편집기로 이름, 로고와 색상을 미리 보고 테마를 만들거나 수정할 수 있습니다. JSON과 별도 로고도 가져올 수 있습니다. 적용 중인 사용자 테마를 제거하면 기본 라즈베리 테마로 돌아갑니다.

서버의 `/opt/rasprint/themes`에 `my-theme.json`을 저장합니다. `plum.json`을 복사하거나 다음 예시를 사용하세요. 로고 이미지도 같은 폴더에 둡니다. 관리자 메뉴에서 테마를 선택할 수 있으며, 선택한 테마는 재시작 후에도 유지됩니다. 초기 테마는 `RASPRINT_THEME`로 설정합니다.

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

`name_en`은 관리자 메뉴의 영어 테마 이름이며 생략할 수 있습니다. 색상은 `#RRGGBB`로 지정하며 생략한 색상에는 기본 테마 값이 적용됩니다. 전체 키는 `rasprint/themes.py`의 `DEFAULT_COLORS`에 있습니다. 로고는 테마 폴더를 기준으로 PNG, JPEG 또는 WebP 경로를 지정합니다. 메뉴 상단의 로고 영역에는 정사각형 파일이 잘 맞습니다.

## 프린터 없이 실행

로컬에서 Python 3.11~3.13으로 실행할 수 있습니다. 다음은 Windows PowerShell 명령입니다.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:RASPRINT_DEMO = "true"
.\.venv\Scripts\python.exe -m uvicorn rasprint.main:app --host 127.0.0.1 --port 8000
```

Linux/macOS에서는 다음을 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
RASPRINT_DEMO=true .venv/bin/python -m uvicorn rasprint.main:app --host 127.0.0.1 --port 8000
```

[http://127.0.0.1:8000/](http://127.0.0.1:8000/)에서 업로드, 대기열, 관리자 로그인을 시험할 수 있습니다. 데모 출력은 가상 대기열에서 처리됩니다.

## 문제 확인

```bash
systemctl status rasprint.socket rasprint.service cups.service
journalctl -u rasprint-ap.service -u rasprint-proxy.service -u rasprint-mdns.service -u rasprint-cups.service -u rasprint-overlay.service -n 50 --no-pager
journalctl -u rasprint.service -n 50 --no-pager
journalctl -u cups.service -n 50 --no-pager
lpstat -t
nmcli device status
```

- 첫 웹 요청이 들어오면 `rasprint.socket`이 서버를 시작합니다. 요청 전에는 `rasprint.service`가 대기 상태일 수 있습니다.
- AP가 보이지 않으면 Wi-Fi 국가 설정과 `nmcli radio wifi`, `nmcli -g WIFI-PROPERTIES.AP device show wlan0` 출력을 확인합니다.
- `.local` 주소가 열리지 않으면 직접 IP와 포트로 접속하세요. 다른 기기가 이미 쓰는 이름이라면 관리자 메뉴에서 로컬 웹 주소를 다른 이름으로 바꿉니다.
- 프린터에 용지를 보충하거나 걸림을 제거한 후에도 큐가 멈춰 있으면 `lpstat -p`로 원인을 확인하고 필요하면 `sudo cupsenable hp8710`로 다시 활성화합니다.
- CUPS 완료 상태를 받으면 작업이 완료로 표시됩니다. USB 드라이버에 따라 이 상태는 마지막 종이가 배출되기 전에 보고될 수 있습니다.
