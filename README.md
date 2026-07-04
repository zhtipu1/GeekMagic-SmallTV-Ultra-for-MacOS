# GeekMagic SmallTV-Ultra — macOS Port

macOS port of the GeekMagic SmallTV-Ultra Desktop Controller (pywebview).

**Status:** Ported — pending real-hardware testing on macOS

## Run from source

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Build a .app bundle

```bash
python setup.py py2app
```
The bundle is written to `dist/GeekMagic Controller.app`.

## Notes on the macOS port

- App data (settings, cached firmware) is stored in `~/Library/Application Support/GeekMagic Controller` instead of `%APPDATA%`.
- PC Wi-Fi scan/connect/disconnect and Ethernet enable/disable now shell out to `networksetup` (and the legacy `airport` utility if present) instead of `netsh`. `networksetup -setairportnetwork` may prompt for the system password when changing Wi-Fi on some macOS versions — this is an OS-level permission prompt, not an app bug.
- The Wi-Fi network scan falls back to the OS's preferred-networks list (without live signal strength) on macOS 14+ where Apple removed the `airport` utility.
- The dock icon comes from `Icons/Icon_Mac.icns` via the `py2app` bundle `Info.plist`, not from `webview.start()`.

**App Developer:** Zahidul Haque Tipu
