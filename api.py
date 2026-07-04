import atexit
import io
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile

import requests
from PIL import Image, ImageSequence

_NO_WINDOW = {}

# The ESP8266 web server is single-threaded — it can only handle one HTTP
# connection at a time. pywebview dispatches each JS->Python call on its own
# thread, so without this lock, concurrent UI actions (e.g. loading several
# GIF thumbnails, or a Promise.all settings fetch) would fire overlapping
# requests at the device and cause dropped/garbled responses. Every request
# that hits the device (not GitHub/ip-api/etc.) must acquire this first.
_device_lock = threading.Lock()

def _app_data_dir():
    base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    d = os.path.join(base, "GeekMagic Controller")
    os.makedirs(d, exist_ok=True)
    return d

SETTINGS_FILE = os.path.join(_app_data_dir(), "settings.json")
FIRMWARE_DIR = os.path.join(_app_data_dir(), "smalltv-ultra-main", "smalltv-ultra-main")
GITHUB_REPO = "GeekMagicClock/smalltv-ultra"
GITHUB_API = "https://api.github.com/repos/" + GITHUB_REPO + "/contents/"
GITHUB_RAW = "https://raw.githubusercontent.com/" + GITHUB_REPO + "/main/"

# ESP8266 GIF playback hard limits — the device is a slow single-core decoder
# with no alpha compositing, so output GIFs must be kept structurally simple
# or frames visibly lag/stutter or fail to decode.
MAX_GIF_COLORS = 256         # GIF's own ceiling; the UI still defaults far lower (16/64)
                             # for smooth playback, this is just the user-adjustable max
MIN_FRAME_DELAY_MS = 40      # ~25 FPS floor; the renderer can't keep up faster than this

DEFAULT_SETTINGS = {
    "device_ip": "",
    "timeout": 4,
    "wifi_lock_enabled": False,
    "wifi_lock_ssid": "",
    "wifi_lock_interval": 30,
    "theme": "dark",
}


def _load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            return {**DEFAULT_SETTINGS, **data}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def _save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


class GeekMagicAPI:
    def __init__(self):
        self._settings = _load_settings()
        self._wifi_lock_thread = None
        self._wifi_lock_stop = threading.Event()
        self._disabled_adapters = []   # Ethernet adapters we disabled
        self._fw_download_progress = 0  # 0-100
        self._fw_download_status = ""
        atexit.register(self._cleanup)
        if self._settings.get("wifi_lock_enabled") and self._settings.get("wifi_lock_ssid"):
            self._start_wifi_lock()

    # ── Settings ──────────────────────────────────────────────────────────────

    def get_settings(self):
        return self._settings

    def save_settings(self, settings: dict):
        old_lock = self._settings.get("wifi_lock_enabled")
        old_ssid = self._settings.get("wifi_lock_ssid")
        self._settings.update(settings)
        _save_settings(self._settings)
        new_lock = self._settings.get("wifi_lock_enabled")
        new_ssid = self._settings.get("wifi_lock_ssid")
        if new_lock and new_ssid:
            if not old_lock or old_ssid != new_ssid:
                self._restart_wifi_lock()
        elif not new_lock and old_lock:
            self._stop_wifi_lock()
            self._reenable_ethernet()
        return {"ok": True}

    # ── Device HTTP helpers ───────────────────────────────────────────────────

    def _base(self):
        ip = self._settings.get("device_ip", "").strip()
        ip = re.sub(r'^https?://', '', ip).rstrip('/')
        if not ip:
            raise ValueError("Device IP not configured")
        return f"http://{ip}"

    def _get(self, path: str):
        url = self._base() + path
        with _device_lock:
            r = requests.get(url, timeout=self._settings.get("timeout", 4))
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        if "json" in ct or r.text.strip().startswith(("{", "[")):
            try:
                return r.json()
            except Exception:
                pass
        return r.text

    def _set(self, params: dict):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return self._get(f"/set?{qs}")

    # ── Device info ───────────────────────────────────────────────────────────

    def get_device_info(self):
        try:
            return {"ok": True, "data": self._get("/v.json")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def ping_device(self):
        try:
            ip = re.sub(r'^https?://', '', self._settings.get("device_ip", "").strip()).rstrip('/')
            if not ip:
                return {"ok": False, "error": "No IP configured"}
            s = socket.create_connection((ip, 80), timeout=2)
            s.close()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Brightness ────────────────────────────────────────────────────────────

    def get_brightness(self):
        try:
            data = self._safe_get("/brt.json")
            if data is None:
                return {"ok": False, "error": "Endpoint not available"}
            return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_brightness(self, brt: int):
        try:
            self._set({"brt": brt})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_night_mode(self):
        try:
            return {"ok": True, "data": self._get("/timebrt.json")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_night_mode(self, t1: int, t2: int, b1: int, b2: int, en: int):
        try:
            self._set({"t1": t1, "t2": t2, "b1": b1, "b2": b2, "en": en})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Theme ─────────────────────────────────────────────────────────────────

    def get_theme(self):
        try:
            return {"ok": True, "data": self._get("/app.json")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_theme(self, theme: int):
        try:
            self._set({"theme": theme})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_theme_list(self):
        try:
            return {"ok": True, "data": self._get("/theme_list.json")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_theme_list(self, theme_list: str, sw_en: int, theme_interval: int):
        try:
            self._set({"theme_list": theme_list, "sw_en": sw_en, "theme_interval": theme_interval})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Time ─────────────────────────────────────────────────────────────────

    def _safe_get(self, path: str):
        """GET endpoint, return None on any error (404, timeout, etc.)."""
        try:
            return self._get(path)
        except Exception:
            return None

    def _get_free_kb(self):
        """Free SPIFFS space on the device, or None if it can't be read."""
        try:
            space = self._get("/space.json")
            return int(space.get("free", 0)) // 1024
        except Exception:
            return None

    def _check_fits_on_device(self, size_bytes: int):
        """Guard against uploading something bigger than the device's free
        space. The ESP8266's SPIFFS write can crash/corrupt the filesystem
        when it runs out of room mid-write, so this must block the upload
        outright rather than just warn after the fact."""
        size_kb = size_bytes / 1024
        free_kb = self._get_free_kb()
        if free_kb is not None and size_kb > free_kb - 10:
            return {
                "ok": False,
                "error": f"Not enough space on device: needs ~{int(size_kb)} KB, only {free_kb} KB free",
                "insufficient_space": True,
                "needed_kb": int(size_kb),
                "free_kb": free_kb,
            }
        return None

    def get_time_settings(self):
        try:
            tz        = self._safe_get("/tz.json")
            hour12    = self._safe_get("/hour12.json")
            day       = self._safe_get("/day.json")
            ntp       = self._safe_get("/ntp.json")
            colon     = self._safe_get("/colon.json")
            font      = self._safe_get("/font.json")
            timecolor = self._safe_get("/timecolor.json")
            return {"ok": True, "data": {
                "tz": tz, "hour12": hour12, "day": day,
                "ntp": ntp, "colon": colon, "font": font, "timecolor": timecolor,
            }}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_timezone(self, tz_auto: int, tz_offset: int):
        try:
            self._set({"tz_auto": tz_auto, "tz_offset": tz_offset})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_time_format(self, hour: int):
        try:
            self._set({"hour": hour})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_date_format(self, day: int):
        try:
            self._set({"day": day})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_ntp(self, ntp: str, time_interval: int):
        try:
            self._set({"ntp": urllib.parse.quote(ntp), "time_interval": time_interval})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_colon(self, colon: int):
        try:
            self._set({"colon": colon})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_font(self, font: int):
        try:
            self._set({"font": font})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_time_colors(self, hc: str, mc: str, sc: str):
        try:
            self._set({
                "hc": urllib.parse.quote(hc),
                "mc": urllib.parse.quote(mc),
                "sc": urllib.parse.quote(sc),
            })
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Album ─────────────────────────────────────────────────────────────────

    def get_album_settings(self):
        try:
            return {"ok": True, "data": self._get("/album.json")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_album_settings(self, i_i: int, autoplay: int):
        try:
            self._set({"i_i": i_i, "autoplay": autoplay})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_file_list(self):
        try:
            raw = self._get("/filelist?dir=/image/")
            if raw in ("Empty", "Fail", "", None):
                raw = ""
            files = self._parse_filelist(raw)
            space_raw = self._safe_get("/space.json")
            free_kb = None
            if isinstance(space_raw, dict):
                free_bytes = space_raw.get("free") or space_raw.get("space")
                if free_bytes is not None:
                    free_kb = int(free_bytes) // 1024
            return {"ok": True, "data": {"files": files, "free_kb": free_kb}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _parse_filelist(self, html: str):
        """
        Parse the HTML table returned by /filelist.
        Returns list of {name, size_kb} dicts.
        Tries to extract size from the table's third <td> column.
        """
        from html.parser import HTMLParser

        class _P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows = []
                self._cur = []
                self._in_td = False
                self._td_text = ""
                self._href_captured = False  # once href is set, ignore inner text

            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    self._cur = []
                elif tag == "td":
                    self._in_td = True
                    self._td_text = ""
                    self._href_captured = False
                elif tag == "a" and self._in_td:
                    for k, v in attrs:
                        if k == "href":
                            self._td_text = v.split("/")[-1]
                            self._href_captured = True

            def handle_endtag(self, tag):
                if tag == "td":
                    self._in_td = False
                    self._href_captured = False
                    self._cur.append(self._td_text.strip())
                elif tag == "tr":
                    if self._cur:
                        self.rows.append(self._cur)
                    self._cur = []

            def handle_data(self, data):
                if self._in_td and not self._href_captured:
                    self._td_text += data

        p = _P()
        p.feed(html)
        results = []
        for row in p.rows:
            # row[0]=index, row[1]=filename, row[2]=size, ...
            if len(row) < 2:
                continue
            name = row[1].strip()
            if not name or not re.search(r'\.(jpg|jpeg|gif|png)$', name, re.IGNORECASE):
                continue
            size_kb = None
            if len(row) >= 3:
                try:
                    size_kb = int(row[2].strip())
                except Exception:
                    pass
            results.append({"name": name, "size_kb": size_kb})
        return results

    def _photo_thumb_cache_dir(self):
        d = os.path.join(_app_data_dir(), "photo_thumb_cache")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _is_complete_image(data: bytes) -> bool:
        """Magic bytes (start) AND end marker — rejects truncated transfers."""
        if len(data) < 6:
            return False
        if data[0] == 0xFF and data[1] == 0xD8:  # JPEG
            return data[-2] == 0xFF and data[-1] == 0xD9
        if data[:3] == b"GIF":
            return data[-1:] == b"\x3b"
        return False

    def _clear_photo_thumb_cache(self, filename: str = None):
        d = self._photo_thumb_cache_dir()
        try:
            if filename:
                safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
                p = os.path.join(d, safe_name)
                if os.path.exists(p):
                    os.remove(p)
            else:
                for name in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, name))
                    except Exception:
                        pass
        except Exception:
            pass

    def _cache_uploaded_photo(self, filename: str, local_path: str):
        """Write-through the exact bytes we just uploaded into the thumbnail
        cache, so viewing it afterwards never has to fetch it back from the
        (slow) device at all."""
        try:
            safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
            cache_path = os.path.join(self._photo_thumb_cache_dir(), safe_name)
            with open(local_path, "rb") as src, open(cache_path, "wb") as dst:
                dst.write(src.read())
        except Exception:
            pass

    def get_photo_thumb(self, filename: str, force: bool = False):
        """Return a base64 data URL for a device album image thumbnail, cached on disk.

        Set force=True to bypass the cache and re-fetch (manual retry)."""
        import base64
        try:
            safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
            cache_path = os.path.join(self._photo_thumb_cache_dir(), safe_name)
            data = None
            if not force and os.path.exists(cache_path):
                with open(cache_path, "rb") as f:
                    cached = f.read()
                if self._is_complete_image(cached):
                    data = cached
                else:
                    os.remove(cache_path)
            if data is None:
                url = self._base() + f"/image/{urllib.parse.quote(filename)}"
                # Thumbnails can be large (a full 240x240 GIF can run into the
                # hundreds of KB) and the ESP8266 serves files slowly, so the
                # short request timeout used for quick API calls is too tight
                # here and was causing large files to fail to load.
                thumb_timeout = max(20, self._settings.get("timeout", 4) * 5)
                with _device_lock:
                    r = requests.get(url, timeout=thumb_timeout)
                r.raise_for_status()
                data = r.content
                if not self._is_complete_image(data):
                    return {"ok": False, "error": "Incomplete image received from device"}
                with open(cache_path, "wb") as f:
                    f.write(data)
            ext = os.path.splitext(filename)[1].lower()
            mime = "image/gif" if ext == ".gif" else "image/jpeg"
            b64 = base64.b64encode(data).decode()
            return {"ok": True, "dataUrl": f"data:{mime};base64,{b64}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_file(self, filename: str):
        try:
            full = urllib.parse.quote(f"/image/{filename}")
            self._get(f"/delete?file={full}")
            self._clear_photo_thumb_cache(filename)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_display_image(self, filename: str):
        """Send /set?img= with full /image/ path as required by device firmware."""
        try:
            full = f"/image/{filename}"
            enc = urllib.parse.quote(full, safe='')
            with _device_lock:
                r = requests.get(self._base() + f"/set?img={enc}",
                                 timeout=self._settings.get("timeout", 4))
            return {"ok": True, "device_response": r.text.strip(),
                    "url_sent": f"/set?img={enc}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_images(self):
        try:
            self._set({"clear": "image"})
            self._clear_photo_thumb_cache()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Image processing ──────────────────────────────────────────────────────

    def auto_detect_location(self):
        """
        Detect city and country from public IP via ip-api.com (free, no key).
        Returns city name, country code, and recommended unit preset key.
        """
        try:
            r = requests.get("http://ip-api.com/json/?fields=city,country,countryCode,lat,lon,status,message",
                             timeout=8)
            data = r.json()
            if data.get("status") != "success":
                return {"ok": False, "error": data.get("message", "Location detection failed")}

            city        = data.get("city", "")
            country     = data.get("country", "")
            cc          = data.get("countryCode", "").upper()

            # Pick unit preset from country code
            if cc in ("US", "LR", "MM"):
                preset = "imperial"
            elif cc in ("GB", "IE"):
                preset = "uk"
            elif cc == "CA":
                preset = "canada"
            else:
                preset = "metric"

            return {"ok": True, "city": city, "country": country,
                    "country_code": cc, "preset": preset}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_url(self, url: str):
        """Open a URL in the system's default browser."""
        import webbrowser
        webbrowser.open(url)
        return {"ok": True}

    def open_gif_dialog(self):
        import webview
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("GIF files (*.gif)", "All files (*.*)")
        )
        if result:
            return {"ok": True, "files": list(result)}
        return {"ok": False, "files": []}

    def open_file_dialog(self):
        import webview
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=("Image files (*.jpg;*.jpeg;*.gif;*.png;*.bmp;*.webp)", "All files (*.*)")
        )
        if result:
            return {"ok": True, "files": list(result)}
        return {"ok": False, "files": []}

    def get_image_data_url(self, file_path: str):
        """Return base64 data URL for displaying image in crop modal."""
        import base64
        try:
            ext = os.path.splitext(file_path)[1].lower()
            with open(file_path, "rb") as f:
                data = f.read()
            mime = "image/gif" if ext == ".gif" else "image/jpeg"
            b64 = base64.b64encode(data).decode()
            return {"ok": True, "dataUrl": f"data:{mime};base64,{b64}", "ext": ext}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def process_and_upload(self, file_path: str, crop_x: float, crop_y: float,
                           crop_size: float, img_natural_w: float, img_natural_h: float,
                           brightness: float = 1.0, contrast: float = 1.0,
                           saturation: float = 1.0, sharpness: float = 1.0,
                           max_colors: int = 256, max_frames: int = 0, target_kb: int = 0):
        """
        Crop (in natural image coordinates), resize to 240×240, upload to device.
        crop_x/y/size are in natural image pixels.
        """
        try:
            ext = os.path.splitext(file_path)[1].lower()
            name = os.path.basename(file_path)
            base = os.path.splitext(name)[0]

            cx = int(round(crop_x))
            cy = int(round(crop_y))
            cs = int(round(crop_size))

            if ext == ".gif":
                out_path = os.path.join(tempfile.gettempdir(), base + "_gm.gif")
                self._process_gif(file_path, cx, cy, cs, out_path,
                                  brightness=brightness, contrast=contrast,
                                  saturation=saturation, sharpness=sharpness,
                                  max_colors=max_colors, max_frames=max_frames,
                                  target_kb=target_kb)
                upload_name = base + "_gm.gif"
            else:
                out_path = os.path.join(tempfile.gettempdir(), base + "_gm.jpg")
                self._process_image(file_path, cx, cy, cs, out_path,
                                    brightness=brightness, contrast=contrast,
                                    saturation=saturation, sharpness=sharpness)
                upload_name = base + "_gm.jpg"

            too_big = self._check_fits_on_device(os.path.getsize(out_path))
            if too_big:
                return too_big
            self._clear_photo_thumb_cache(upload_name)
            url = self._base() + "/doUpload?dir=/image/"
            with open(out_path, "rb") as f:
                files = {"image": (upload_name, f,
                                   "image/gif" if ext == ".gif" else "image/jpeg")}
                with _device_lock:
                    r = requests.post(url, files=files,
                                      timeout=self._settings.get("timeout", 4) * 10)
            r.raise_for_status()
            # We already have the exact bytes we just sent, cache them now
            # instead of waiting to fetch them back from the device later.
            self._cache_uploaded_photo(upload_name, out_path)
            return {"ok": True, "name": upload_name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _apply_adjustments(self, img, brightness=1.0, contrast=1.0, saturation=1.0, sharpness=1.0):
        from PIL import ImageEnhance
        if brightness != 1.0:
            img = ImageEnhance.Brightness(img).enhance(brightness)
        if contrast != 1.0:
            img = ImageEnhance.Contrast(img).enhance(contrast)
        if saturation != 1.0:
            img = ImageEnhance.Color(img).enhance(saturation)
        if sharpness != 1.0:
            img = ImageEnhance.Sharpness(img).enhance(sharpness)
        return img

    def _process_image(self, src: str, cx: int, cy: int, cs: int, dst: str,
                       brightness=1.0, contrast=1.0, saturation=1.0, sharpness=1.0):
        img = Image.open(src).convert("RGB")
        w, h = img.size
        if cs > 0:
            cx = max(0, min(cx, w - 1))
            cy = max(0, min(cy, h - 1))
            cs = min(cs, w - cx, h - cy)
            img = img.crop((cx, cy, cx + cs, cy + cs))
        img = img.resize((240, 240), Image.LANCZOS)
        img = self._apply_adjustments(img, brightness, contrast, saturation, sharpness)
        img.save(dst, "JPEG", quality=88)

    @staticmethod
    def _encode_gif(frames, durations) -> bytes:
        buf = io.BytesIO()
        # disposal=2 tells the decoder to clear the canvas to the background
        # color before drawing the next frame, instead of combining it with
        # whatever was there before. The ESP8266 has no real alpha
        # compositing, so leaving disposal unset (or 0/1) causes ghosting.
        # interlace is never written here: Pillow's GIF encoder does not
        # support interlaced output at all, so every frame is already
        # sequential/non-interlaced by construction.
        frames[0].save(buf, save_all=True, append_images=frames[1:],
                       loop=0, duration=durations, format="GIF", optimize=True,
                       disposal=2)
        return buf.getvalue()

    @staticmethod
    def _quantize_frames(raw_frames, raw_dur, max_colors, max_frames):
        frames = raw_frames
        dur = raw_dur
        if max_frames and len(frames) > max_frames:
            step = len(frames) / max_frames
            idx = [int(i * step) for i in range(max_frames)]
            frames = [frames[i] for i in idx]
            dur = [dur[i] for i in idx]

        # Frame-delay floor: the device can't redraw faster than this, so a
        # source GIF requesting a shorter delay would just stutter instead
        # of actually playing faster.
        dur = [max(MIN_FRAME_DELAY_MS, d) for d in dur]

        # Hard cap on the color table size regardless of what was requested,
        # a bigger palette means more bytes per scanline for the device to
        # decode per frame.
        clamp = max(2, min(MAX_GIF_COLORS, max_colors))

        # Flatten transparency onto a solid black canvas. Image.convert("RGB")
        # on an RGBA source keeps whatever RGB values sit behind a transparent
        # pixel, which can leave stray colors in the palette and haloing
        # artifacts once alpha is dropped, since the device has no alpha
        # compositing to hide it.
        flattened = []
        target_size = frames[0].size
        for f in frames:
            if f.mode != "RGBA":
                f = f.convert("RGBA")
            bg = Image.new("RGB", f.size, (0, 0, 0))
            bg.paste(f, mask=f.split()[3])
            flattened.append(bg)

        # Build one shared/global palette from a sampled strip of every frame,
        # then re-quantize each frame against that same palette. Quantizing
        # each frame independently (the old approach) gave each one its own
        # palette, which forces the GIF encoder to write a local color table
        # per frame instead of a single global one, and the ESP8266 has to
        # re-read a new palette on every frame instead of once for the file.
        sample_w = max(1, 64 // max(1, len(flattened)))
        strip = Image.new("RGB", (sample_w * len(flattened), flattened[0].height))
        for i, f in enumerate(flattened):
            strip.paste(f.resize((sample_w, f.height)), (i * sample_w, 0))
        master = strip.quantize(colors=clamp, method=Image.Quantize.MEDIANCUT, dither=0)

        quantized = [f.quantize(colors=clamp, palette=master, dither=0) for f in flattened]

        # Hard-lock every frame back to the exact requested size. If any
        # earlier resize/crop step drifted by even a pixel, the device's
        # renderer would try to rescale on the fly, which is what causes
        # severe playback lag.
        quantized = [q if q.size == target_size else q.resize(target_size, Image.LANCZOS)
                     for q in quantized]
        return quantized, dur

    def _load_gif_frames(self, src, cx, cy, cs, size, brightness, contrast, saturation, sharpness):
        gif = Image.open(src)
        raw_frames, raw_dur = [], []
        try:
            while True:
                frame = gif.copy().convert("RGBA")
                w, h = frame.size
                if cs > 0:
                    fcx = max(0, min(cx, w - 1))
                    fcy = max(0, min(cy, h - 1))
                    fcs = min(cs, w - fcx, h - fcy)
                    frame = frame.crop((fcx, fcy, fcx + fcs, fcy + fcs))
                frame = frame.resize((size, size), Image.LANCZOS)
                frame = self._apply_adjustments(frame, brightness, contrast, saturation, sharpness)
                raw_frames.append(frame)
                raw_dur.append(gif.info.get("duration", 100))
                gif.seek(gif.tell() + 1)
        except EOFError:
            pass
        return raw_frames, raw_dur

    def _process_gif(self, src: str, cx: int, cy: int, cs: int, dst: str, size: int = 240,
                     brightness=1.0, contrast=1.0, saturation=1.0, sharpness=1.0,
                     max_colors: int = 256, max_frames: int = 0, target_kb: int = 0):
        raw_frames, raw_dur = self._load_gif_frames(src, cx, cy, cs, size, brightness, contrast, saturation, sharpness)
        if not raw_frames:
            return
        if target_kb > 0:
            data = self._compress_to_target(raw_frames, raw_dur, max_colors, max_frames, target_kb)
        else:
            frames, dur = self._quantize_frames(raw_frames, raw_dur, max_colors, max_frames)
            data = self._encode_gif(frames, dur)
        with open(dst, "wb") as f:
            f.write(data)

    def _compress_to_target(self, raw_frames, raw_dur, max_colors, max_frames, target_kb):
        target_bytes = target_kb * 1024
        colors = max(2, min(256, max_colors))
        frames_limit = max_frames if max_frames else len(raw_frames)

        best = None
        # Iterative squeeze: reduce colors first, then frames
        for _ in range(20):
            quantized, dur = self._quantize_frames(raw_frames, raw_dur, colors, frames_limit)
            data = self._encode_gif(quantized, dur)
            if best is None or len(data) < len(best):
                best = data
            if len(data) <= target_bytes:
                return data
            # Reduce colors by ~30%, then reduce frames if colors already at minimum
            new_colors = max(2, int(colors * 0.7))
            if new_colors == colors:
                # Colors can't go lower — cut frames instead
                frames_limit = max(2, int(frames_limit * 0.7))
                if frames_limit == max(2, int(frames_limit / 0.7)):
                    break  # nothing left to reduce
            colors = new_colors
        return best  # best effort if target unreachable

    def get_gif_compression_preview(self, file_path: str, max_colors: int = 16, max_frames: int = 20,
                                    target_kb: int = 0, output_size: int = 80,
                                    crop_x: float = 0, crop_y: float = 0, crop_size: float = 0,
                                    brightness: float = 1.0, contrast: float = 1.0,
                                    saturation: float = 1.0, sharpness: float = 1.0):
        """Return stats on what compression will produce, plus a base64 data URL
        of the exact GIF bytes the device would receive, without saving anything."""
        import base64
        try:
            gif = Image.open(file_path)
            total_frames = 0
            try:
                while True:
                    gif.seek(gif.tell() + 1)
                    total_frames += 1
            except EOFError:
                total_frames += 1

            orig_size = os.path.getsize(file_path)
            raw_frames, raw_dur = self._load_gif_frames(
                file_path, int(round(crop_x)), int(round(crop_y)), int(round(crop_size)),
                output_size, brightness, contrast, saturation, sharpness)

            if target_kb > 0:
                data = self._compress_to_target(raw_frames, raw_dur, max_colors, max_frames, target_kb)
                compressed_size = len(data)
                # Reconstruct final frame count from data (approximate via re-open)
                try:
                    tmp = Image.open(io.BytesIO(data))
                    out_frames = 0
                    try:
                        while True:
                            tmp.seek(tmp.tell() + 1)
                            out_frames += 1
                    except EOFError:
                        out_frames += 1
                except Exception:
                    out_frames = len(raw_frames)
                met_target = compressed_size <= target_kb * 1024
            else:
                frames, dur = self._quantize_frames(raw_frames, raw_dur, max_colors, max_frames)
                data = self._encode_gif(frames, dur)
                compressed_size = len(data)
                out_frames = len(frames)
                met_target = None

            free_kb = None
            try:
                space = self._get("/space.json")
                free_kb = int(space.get("free", 0)) // 1024
            except Exception:
                pass

            return {
                "ok": True,
                "original_size": orig_size,
                "compressed_size": compressed_size,
                "original_frames": total_frames,
                "output_frames": out_frames,
                "free_kb": free_kb,
                "will_fit": (free_kb is None) or (compressed_size // 1024 < free_kb - 10),
                "met_target": met_target,
                "data_url": f"data:image/gif;base64,{base64.b64encode(data).decode()}",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def process_and_upload_gif_crop(self, file_path: str, crop_x: float, crop_y: float,
                                    crop_size: float, img_natural_w: float, img_natural_h: float,
                                    brightness: float = 1.0, contrast: float = 1.0,
                                    saturation: float = 1.0, sharpness: float = 1.0,
                                    max_colors: int = 16, max_frames: int = 20, target_kb: int = 0):
        """Crop, resize to 80×80, compress, upload to /gif directory."""
        try:
            ext = os.path.splitext(file_path)[1].lower()
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            cx = int(round(crop_x))
            cy = int(round(crop_y))
            cs = int(round(crop_size))
            out_path = os.path.join(tempfile.gettempdir(), base_name + "_wgif.gif")
            if ext == ".gif":
                self._process_gif(file_path, cx, cy, cs, out_path, size=80,
                                  brightness=brightness, contrast=contrast,
                                  saturation=saturation, sharpness=sharpness,
                                  max_colors=max_colors, max_frames=max_frames,
                                  target_kb=target_kb)
            else:
                img = Image.open(file_path).convert("RGBA")
                w, h = img.size
                if cs > 0:
                    cx = max(0, min(cx, w - 1))
                    cy = max(0, min(cy, h - 1))
                    cs = min(cs, w - cx, h - cy)
                    img = img.crop((cx, cy, cx + cs, cy + cs))
                img = img.resize((80, 80), Image.LANCZOS)
                img = self._apply_adjustments(img, brightness, contrast, saturation, sharpness)
                clamp = max(2, min(256, max_colors))
                img = img.convert("RGB").quantize(colors=clamp, method=Image.Quantize.MEDIANCUT, dither=0)
                img.save(out_path, "GIF", optimize=True)
            return self.upload_gif(out_path)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Network / WiFi ────────────────────────────────────────────────────────

    def get_wifi_networks(self):
        try:
            data = self._get("/wifi.json?q=1")
            return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def connect_device_wifi(self, ssid: str, password: str):
        try:
            self._get(f"/wifisave?s={urllib.parse.quote(ssid)}&p={urllib.parse.quote(password)}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _wifi_hardware_port(self):
        """Return the macOS hardware port device (e.g. 'en0') for the Wi-Fi adapter."""
        try:
            result = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True, text=True, timeout=6
            )
            lines = result.stdout.splitlines()
            for i, line in enumerate(lines):
                if line.strip().startswith("Hardware Port:") and "Wi-Fi" in line:
                    for j in range(i + 1, min(i + 3, len(lines))):
                        if lines[j].strip().startswith("Device:"):
                            return lines[j].split(":", 1)[-1].strip()
            return "en0"
        except Exception:
            return "en0"

    def get_pc_wifi_networks(self):
        try:
            dev = self._wifi_hardware_port()
            airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
            networks = []
            if os.path.exists(airport):
                result = subprocess.run(
                    [airport, "-s"], capture_output=True, text=True, timeout=10
                )
                lines = result.stdout.splitlines()
                if lines:
                    for line in lines[1:]:
                        parts = line.rstrip().rsplit(maxsplit=6)
                        if len(parts) >= 2:
                            ssid = parts[0].strip()
                            try:
                                rssi = int(parts[1])
                                signal = max(0, min(100, 2 * (rssi + 100)))
                            except Exception:
                                signal = 0
                            if ssid:
                                networks.append({"ssid": ssid, "signal": signal})
            else:
                # airport utility removed on newer macOS — fall back to preferred networks list (no live signal)
                result = subprocess.run(
                    ["networksetup", "-listpreferredwirelessnetworks", dev],
                    capture_output=True, text=True, timeout=6
                )
                for line in result.stdout.splitlines()[1:]:
                    ssid = line.strip()
                    if ssid:
                        networks.append({"ssid": ssid, "signal": 0})
            return {"ok": True, "data": networks}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_current_pc_wifi(self):
        try:
            dev = self._wifi_hardware_port()
            result = subprocess.run(
                ["networksetup", "-getairportnetwork", dev],
                capture_output=True, text=True, timeout=6
            )
            out = result.stdout.strip()
            if "Current Wi-Fi Network:" in out:
                return {"ok": True, "ssid": out.split(":", 1)[-1].strip()}
            return {"ok": True, "ssid": ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def connect_pc_wifi(self, ssid: str, password: str = ""):
        try:
            dev = self._wifi_hardware_port()
            cmd = ["networksetup", "-setairportnetwork", dev, ssid]
            if password:
                cmd.append(password)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0 and not result.stdout.strip():
                return {"ok": True}
            return {"ok": False, "error": result.stdout.strip() or result.stderr.strip() or "Failed to connect"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def disconnect_pc_wifi(self):
        try:
            dev = self._wifi_hardware_port()
            subprocess.run(["networksetup", "-setairportpower", dev, "off"], capture_output=True, text=True, timeout=10)
            result = subprocess.run(["networksetup", "-setairportpower", dev, "on"], capture_output=True, text=True, timeout=10)
            return {"ok": result.returncode == 0}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Ethernet management ───────────────────────────────────────────────────

    def _get_ethernet_adapters(self):
        """Return list of macOS network service names for enabled, non-Wi-Fi services."""
        try:
            result = subprocess.run(
                ["networksetup", "-listallnetworkservices"],
                capture_output=True, text=True, timeout=6
            )
            services = []
            for line in result.stdout.splitlines()[1:]:
                name = line.strip()
                if not name or name.startswith("*") or "wi-fi" in name.lower() or "wireless" in name.lower():
                    continue
                services.append(name)
            return services
        except Exception:
            return []

    def _disable_ethernet(self):
        adapters = self._get_ethernet_adapters()
        newly_disabled = []
        for name in adapters:
            try:
                result = subprocess.run(
                    ["networksetup", "-setnetworkserviceenabled", name, "off"],
                    capture_output=True, text=True, timeout=8
                )
                if result.returncode == 0:
                    newly_disabled.append(name)
            except Exception:
                pass
        self._disabled_adapters = list(set(self._disabled_adapters + newly_disabled))
        return newly_disabled

    def _reenable_ethernet(self):
        for name in list(self._disabled_adapters):
            try:
                subprocess.run(
                    ["networksetup", "-setnetworkserviceenabled", name, "on"],
                    capture_output=True, text=True, timeout=8
                )
            except Exception:
                pass
        self._disabled_adapters = []

    def get_ethernet_adapters(self):
        """Expose to JS — list of Ethernet adapter names."""
        return {"ok": True, "data": self._get_ethernet_adapters()}

    # ── WiFi Lock (background thread) ─────────────────────────────────────────

    def _wifi_lock_worker(self):
        # Disable ethernet on start
        self._disable_ethernet()
        while not self._wifi_lock_stop.is_set():
            ssid = self._settings.get("wifi_lock_ssid", "")
            interval = self._settings.get("wifi_lock_interval", 30)
            if ssid:
                current = self.get_current_pc_wifi()
                if current.get("ssid") != ssid:
                    self._disable_ethernet()
                    self.connect_pc_wifi(ssid)
            self._wifi_lock_stop.wait(interval)

    def _start_wifi_lock(self):
        self._wifi_lock_stop.clear()
        self._wifi_lock_thread = threading.Thread(target=self._wifi_lock_worker, daemon=True)
        self._wifi_lock_thread.start()

    def _stop_wifi_lock(self):
        self._wifi_lock_stop.set()

    def _restart_wifi_lock(self):
        self._stop_wifi_lock()
        time.sleep(0.2)
        self._start_wifi_lock()

    def _cleanup(self):
        self._stop_wifi_lock()
        self._reenable_ethernet()

    def get_wifi_lock_status(self):
        active = (self._wifi_lock_thread is not None and
                  self._wifi_lock_thread.is_alive())
        return {
            "ok": True,
            "active": active,
            "disabled_adapters": self._disabled_adapters,
        }

    # ── Device Discovery ─────────────────────────────────────────────────────

    def scan_for_device(self):
        """Scan the local subnet for a GeekMagic device by probing /v.json on each host."""
        import concurrent.futures

        # Determine local subnets — try multiple methods and collect all
        subnets = set()
        # Method 1: UDP connect trick (most reliable for the active interface)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                subnets.add(ip.rsplit(".", 1)[0])
        except Exception:
            pass
        # Method 2: all interfaces via getaddrinfo
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if ip.startswith("127.") or ":" in ip:
                    continue
                parts = ip.rsplit(".", 1)
                if len(parts) == 2:
                    subnets.add(parts[0])
        except Exception:
            pass

        if not subnets:
            return {"ok": False, "error": "Could not determine local subnet"}

        found = []

        def probe(ip):
            try:
                r = requests.get(f"http://{ip}/v.json", timeout=1.5)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        ver = data.get("v") or data.get("ver") or data.get("version")
                        if ver and isinstance(ver, str) and len(ver) > 1:
                            return {"ip": ip, "ver": ver}
            except Exception:
                pass
            return None

        # Always include the most common home router subnets
        common = {"192.168.0", "192.168.1", "192.168.2", "192.168.10",
                  "10.0.0", "10.0.1", "172.16.0"}
        subnets.update(common)

        candidates = [f"{subnet}.{i}" for subnet in subnets for i in range(1, 255)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=80) as ex:
            results = ex.map(probe, candidates)
            for r in results:
                if r:
                    found.append(r)

        return {"ok": True, "found": found, "subnets": list(subnets)}

    # ── Firmware ──────────────────────────────────────────────────────────────

    def get_firmware_info(self):
        """
        Return:
          local_versions: list of {version, path, filename, notes}
          github_versions: list of {version, download_url, filename, notes}
          device_version: str or None
        """
        local = self._scan_local_firmware()
        github = self._fetch_github_firmware()
        device_ver = None
        try:
            info = self._get("/v.json")
            device_ver = info.get("ver") or info.get("version")
        except Exception:
            pass
        return {"ok": True, "local": local, "github": github, "device_ver": device_ver}

    def _scan_local_firmware(self):
        versions = []
        if not os.path.isdir(FIRMWARE_DIR):
            return versions
        for entry in sorted(os.listdir(FIRMWARE_DIR), reverse=True):
            folder = os.path.join(FIRMWARE_DIR, entry)
            if not os.path.isdir(folder):
                continue
            m = re.match(r"Ultra-V([\d.]+)", entry)
            if not m:
                continue
            ver = m.group(1)
            bin_file = None
            zip_file = None
            notes = ""
            for f in os.listdir(folder):
                if f.endswith(".bin"):
                    bin_file = os.path.join(folder, f)
                elif f.endswith(".zip"):
                    zip_file = os.path.join(folder, f)
            hist = os.path.join(folder, "update_history.txt")
            if os.path.exists(hist):
                with open(hist, "r", encoding="utf-8", errors="replace") as hf:
                    notes = hf.read(500)
            if bin_file or zip_file:
                versions.append({
                    "version": ver,
                    "path": bin_file or zip_file,
                    "filename": os.path.basename(bin_file or zip_file),
                    "is_zip": zip_file is not None and bin_file is None,
                    "notes": notes[:200],
                    "source": "local",
                })
        return versions

    def _fetch_github_firmware(self):
        try:
            r = requests.get(GITHUB_API, timeout=6,
                             headers={"Accept": "application/vnd.github.v3+json"})
            if not r.ok:
                return []
            items = r.json()
            versions = []
            for item in items:
                if item.get("type") != "dir":
                    continue
                m = re.match(r"Ultra-V([\d.]+)", item["name"])
                if not m:
                    continue
                ver = m.group(1)
                folder_url = item["url"]
                try:
                    fr = requests.get(folder_url, timeout=5,
                                      headers={"Accept": "application/vnd.github.v3+json"})
                    if not fr.ok:
                        continue
                    files = fr.json()
                    for f in files:
                        name = f.get("name", "")
                        if name.endswith(".bin") or name.endswith(".zip"):
                            versions.append({
                                "version": ver,
                                "download_url": f["download_url"],
                                "filename": name,
                                "is_zip": name.endswith(".zip"),
                                "notes": "",
                                "source": "github",
                            })
                            break
                except Exception:
                    pass
            return sorted(versions, key=lambda x: x["version"], reverse=True)
        except Exception:
            return []

    def get_fw_download_progress(self):
        return {"ok": True, "progress": self._fw_download_progress,
                "status": self._fw_download_status}

    def install_local_firmware(self, file_path: str):
        """Upload a local .bin or .zip firmware to the device."""
        threading.Thread(target=self._do_firmware_upload, args=(file_path,), daemon=True).start()
        return {"ok": True, "message": "Upload started"}

    def download_and_install_firmware(self, download_url: str, filename: str):
        """Download from GitHub then upload to device."""
        threading.Thread(
            target=self._do_fw_download_and_upload,
            args=(download_url, filename),
            daemon=True,
        ).start()
        return {"ok": True, "message": "Download started"}

    def download_firmware_only(self, download_url: str, filename: str, version: str):
        """Download a GitHub firmware build into the local cache without flashing it."""
        threading.Thread(
            target=self._do_fw_download_only,
            args=(download_url, filename, version),
            daemon=True,
        ).start()
        return {"ok": True, "message": "Download started"}

    def _do_fw_download_only(self, url: str, filename: str, version: str):
        try:
            self._fw_download_progress = 0
            self._fw_download_status = f"Downloading {filename}..."
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            folder = os.path.join(FIRMWARE_DIR, f"Ultra-V{version}")
            os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, filename)
            received = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if total:
                            self._fw_download_progress = int(received / total * 100)
                        self._fw_download_status = (
                            f"Downloading... {received // 1024} KB"
                            + (f" / {total // 1024} KB" if total else "")
                        )
            self._fw_download_progress = 100
            self._fw_download_status = "Download complete. Ready to flash."
        except Exception as e:
            self._fw_download_status = f"Error: {e}"
            self._fw_download_progress = -1

    def open_firmware_file_dialog(self):
        import webview
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Firmware files (*.bin;*.zip)", "All files (*.*)")
        )
        if result:
            return {"ok": True, "file": result[0]}
        return {"ok": False, "file": None}

    def _do_fw_download_and_upload(self, url: str, filename: str):
        try:
            self._fw_download_progress = 0
            self._fw_download_status = f"Downloading {filename}..."
            r = requests.get(url, stream=True, timeout=60)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            tmp = os.path.join(tempfile.gettempdir(), filename)
            received = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if total:
                            self._fw_download_progress = int(received / total * 50)
                        self._fw_download_status = (
                            f"Downloading... {received // 1024} KB"
                            + (f" / {total // 1024} KB" if total else "")
                        )
            self._fw_download_status = "Download complete. Uploading to device..."
            self._do_firmware_upload(tmp, start_progress=50)
        except Exception as e:
            self._fw_download_status = f"Error: {e}"
            self._fw_download_progress = -1

    def _do_firmware_upload(self, file_path: str, start_progress: int = 0):
        try:
            bin_path = file_path
            # If zip, extract the .bin
            if file_path.lower().endswith(".zip"):
                self._fw_download_status = "Extracting firmware..."
                tmp_dir = tempfile.mkdtemp()
                with zipfile.ZipFile(file_path, "r") as zf:
                    for name in zf.namelist():
                        if name.endswith(".bin"):
                            zf.extract(name, tmp_dir)
                            bin_path = os.path.join(tmp_dir, name)
                            break
                if not os.path.exists(bin_path):
                    self._fw_download_status = "Error: no .bin found in zip"
                    self._fw_download_progress = -1
                    return

            self._fw_download_status = "Uploading firmware to device..."
            url = self._base() + "/update"
            total = os.path.getsize(bin_path)

            class ProgressFile:
                def __init__(self_, f, total, api):
                    self_.f = f
                    self_.total = total
                    self_.sent = 0
                    self_.api = api

                def read(self_, size=-1):
                    chunk = self_.f.read(size)
                    self_.sent += len(chunk)
                    if self_.total:
                        pct = start_progress + int(self_.sent / self_.total * (100 - start_progress))
                        self_.api._fw_download_progress = pct
                        self_.api._fw_download_status = (
                            f"Uploading... {self_.sent // 1024} / {self_.total // 1024} KB"
                        )
                    return chunk

            with open(bin_path, "rb") as raw:
                pf = ProgressFile(raw, total, self)
                with _device_lock:
                    r = requests.post(
                        url,
                        files={"file": (os.path.basename(bin_path), pf, "application/octet-stream")},
                        timeout=120,
                    )
            r.raise_for_status()
            self._fw_download_progress = 100
            self._fw_download_status = "Firmware uploaded! Device is rebooting..."
        except Exception as e:
            self._fw_download_status = f"Upload error: {e}"
            self._fw_download_progress = -1

    # ── Device management ─────────────────────────────────────────────────────

    # ── Weather ───────────────────────────────────────────────────────────────

    def get_weather_config(self):
        """Fetch all weather-related endpoints and return combined dict."""
        try:
            city    = self._safe_get("/city.json")
            unit    = self._safe_get("/unit.json")
            w_i     = self._safe_get("/w_i.json")
            key     = self._safe_get("/key.json")
            fkey    = self._safe_get("/fkey.json")
            return {"ok": True, "data": {
                "city": city, "unit": unit, "w_i": w_i,
                "key": key, "fkey": fkey,
            }}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_weather_city(self, city: str):
        """City name or numeric city ID. Endpoint: /set?cd1=<city>&cd2=1000"""
        try:
            city = city.strip()
            self._get(f"/set?cd1={urllib.parse.quote(city, safe='')}&cd2=1000")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_weather_units(self, wind: str, temp: str, pressure: str):
        """wind: m/s|km/h|mile/h  temp: °C|°F  pressure: hPa|kPa|mmHg|inHg"""
        try:
            w = urllib.parse.quote(wind.strip(), safe="")
            t = urllib.parse.quote(temp.strip(), safe="")
            p = urllib.parse.quote(pressure.strip(), safe="")
            self._get(f"/set?w_u={w}&t_u={t}&p_u={p}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_weather_interval(self, minutes: int):
        try:
            self._get(f"/set?w_i={minutes}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_weather_apikey(self, apikey: str):
        """Current-weather OpenWeatherMap key. Endpoint: /set?key="""
        try:
            self._get(f"/set?key={apikey.strip()}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_weather_forecast_key(self, apikey: str):
        """Forecast API key. Endpoint: /set?fkey="""
        try:
            self._get(f"/set?fkey={apikey.strip()}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Weather GIF animations (/gif directory, 80×80px) ──────────────────────

    def get_gif_list(self):
        try:
            raw = self._get("/filelist?dir=/gif")
            if raw in ("Empty", "Fail", "", None):
                raw = ""
            files = self._parse_filelist(raw)
            space = self._safe_get("/space.json")
            free_kb = None
            if isinstance(space, dict):
                fb = space.get("free") or space.get("space")
                if fb is not None:
                    free_kb = int(fb) // 1024
            return {"ok": True, "data": {"files": files, "free_kb": free_kb}}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _gif_thumb_cache_dir(self):
        d = os.path.join(_app_data_dir(), "gif_thumb_cache")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _is_complete_gif(data: bytes) -> bool:
        """Magic bytes (start) AND trailer (end) — rejects truncated transfers."""
        return len(data) >= 6 and data[:3] == b"GIF" and data[-1:] == b"\x3b"

    def _clear_gif_thumb_cache(self, filename: str = None):
        d = self._gif_thumb_cache_dir()
        try:
            if filename:
                safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
                p = os.path.join(d, safe_name)
                if os.path.exists(p):
                    os.remove(p)
            else:
                for name in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, name))
                    except Exception:
                        pass
        except Exception:
            pass

    def _cache_uploaded_gif(self, filename: str, local_path: str):
        """Write-through the exact bytes we just uploaded into the thumbnail
        cache, so viewing it afterwards never has to fetch it back from the
        (slow) device at all."""
        try:
            safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
            cache_path = os.path.join(self._gif_thumb_cache_dir(), safe_name)
            with open(local_path, "rb") as src, open(cache_path, "wb") as dst:
                dst.write(src.read())
        except Exception:
            pass

    def get_gif_thumb(self, filename: str, force: bool = False):
        """Return a base64 data URL for a device GIF thumbnail, cached on disk.

        Set force=True to bypass the cache and re-fetch (manual retry)."""
        import base64
        try:
            safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', filename)
            cache_path = os.path.join(self._gif_thumb_cache_dir(), safe_name)
            data = None
            if not force and os.path.exists(cache_path):
                with open(cache_path, "rb") as f:
                    cached = f.read()
                if self._is_complete_gif(cached):
                    data = cached
                else:
                    os.remove(cache_path)
            if data is None:
                url = self._base() + f"/gif/{urllib.parse.quote(filename)}"
                # Same reasoning as get_photo_thumb: large GIFs served slowly
                # by the ESP8266 were timing out under the short default.
                thumb_timeout = max(20, self._settings.get("timeout", 4) * 5)
                with _device_lock:
                    r = requests.get(url, timeout=thumb_timeout)
                r.raise_for_status()
                data = r.content
                if not self._is_complete_gif(data):
                    return {"ok": False, "error": "Incomplete GIF received from device"}
                with open(cache_path, "wb") as f:
                    f.write(data)
            b64 = base64.b64encode(data).decode()
            return {"ok": True, "dataUrl": f"data:image/gif;base64,{b64}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_gif(self, filename: str):
        try:
            full = urllib.parse.quote(f"/gif/{filename}")
            self._get(f"/delete?file={full}")
            self._clear_gif_thumb_cache(filename)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_weather_gif(self, filename: str):
        """Send /set?gif= with full /gif/ path as required by device firmware."""
        try:
            full = f"/gif/{filename}"
            enc = urllib.parse.quote(full, safe='')
            with _device_lock:
                r = requests.get(self._base() + f"/set?gif={enc}",
                                 timeout=self._settings.get("timeout", 4))
            return {"ok": True, "device_response": r.text.strip(), "url_sent": f"/set?gif={enc}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_gifs(self):
        try:
            self._get("/set?clear=gif")
            self._clear_gif_thumb_cache()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def upload_gif(self, file_path: str):
        """Upload a pre-resized 80×80 GIF to /gif directory."""
        filename = os.path.basename(file_path)
        self._clear_gif_thumb_cache(filename)
        try:
            too_big = self._check_fits_on_device(os.path.getsize(file_path))
            if too_big:
                return too_big
            url = self._base() + "/doUpload?dir=/gif"
            with open(file_path, "rb") as f:
                files = {"image": (filename, f, "image/gif")}
                with _device_lock:
                    r = requests.post(url, files=files, timeout=30)
            r.raise_for_status()
            # We already have the exact bytes we just sent, cache them now
            # instead of waiting to fetch them back from the device later.
            self._cache_uploaded_gif(filename, file_path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def process_and_upload_gif(self, file_path: str):
        """Resize any GIF to 80×80 and upload to /gif directory."""
        try:
            out = os.path.join(tempfile.gettempdir(),
                               os.path.splitext(os.path.basename(file_path))[0] + "_wgif.gif")
            self._resize_gif(file_path, out, size=80)
            return self.upload_gif(out)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _resize_gif(self, src: str, dst: str, size: int = 80):
        gif = Image.open(src)
        frames, durations = [], []
        try:
            while True:
                frame = gif.copy().convert("RGBA").resize((size, size), Image.LANCZOS)
                frames.append(frame)
                durations.append(gif.info.get("duration", 100))
                gif.seek(gif.tell() + 1)
        except EOFError:
            pass
        if frames:
            quantized, dur = self._quantize_frames(frames, durations, 64, 0)
            data = self._encode_gif(quantized, dur)
            with open(dst, "wb") as f:
                f.write(data)

    def set_wifi_delay(self, delay: int):
        try:
            self._set({"delay": delay})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_wifi_delay(self):
        try:
            data = self._safe_get("/delay.json")
            if data is None:
                return {"ok": False, "error": "Not available"}
            return {"ok": True, "data": data}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Device management ─────────────────────────────────────────────────────

    def reboot_device(self):
        try:
            self._set({"reboot": 1})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def factory_reset(self):
        try:
            self._set({"reset": 1})
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
