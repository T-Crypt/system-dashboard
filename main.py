"""
SysDash (WebView edition) - Lightweight floating system stats dashboard
=========================================================================
Same lightweight footprint as the tkinter version, but the UI is real
HTML/CSS/SVG rendered through the OS's built-in web engine (WebView2 on
Windows / Edge Chromium) via pywebview - NOT Electron. No bundled browser,
just a thin wrapper around the engine that's already on your system.

This gets you anti-aliased gauges, glow effects, and smooth CSS transitions
that plain Tkinter Canvas can't do.

Dependencies (pip install -r requirements.txt):
    psutil, requests, pystray, Pillow, pywebview

Optional but recommended for temp/power readings:
    LibreHardwareMonitor -> Options > Remote Web Server > Run (port 8085)
"""

import os
import sys
import time
import threading

import psutil
import webview

try:
    import requests
except ImportError:
    requests = None

try:
    import pynvml
    NVML_AVAILABLE = True
except ImportError:
    NVML_AVAILABLE = False

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

LHM_URL = "http://localhost:8085/data.json"
POLL_INTERVAL_S = 1.0
DEFAULT_ACCENT = "#3ecf9a"
WINDOW_W, WINDOW_H = 320, 700

HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------------
# Accent color detection (matches Windows "accent color from background")
# ----------------------------------------------------------------------------
def get_windows_accent_color():
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        value, _ = winreg.QueryValueEx(key, "AccentColor")
        winreg.CloseKey(key)
        b = (value >> 16) & 0xFF
        g = (value >> 8) & 0xFF
        r = value & 0xFF
        return "#{:02x}{:02x}{:02x}".format(r, g, b)
    except Exception:
        return None


def get_cpu_name():
    """Query the real CPU model string from the registry (populated by the
    CPU driver at boot) - same live-hardware-query approach as the GPU name
    via NVML, rather than anything hardcoded."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        winreg.CloseKey(key)
        return " ".join(name.split())  # collapse the padded internal whitespace
    except Exception:
        import platform
        return platform.processor() or None


def clamp_color_brightness(hex_color, min_lum=90):
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    if lum < min_lum:
        boost = min_lum / max(lum, 1)
        r, g, b = [min(255, int(c * boost)) for c in (r, g, b)]
    return "#{:02x}{:02x}{:02x}".format(r, g, b)


ACCENT = clamp_color_brightness(get_windows_accent_color() or DEFAULT_ACCENT)


# ----------------------------------------------------------------------------
# Data collection (background thread; UI pulls latest snapshot via the API)
# ----------------------------------------------------------------------------
class DataStore:
    def __init__(self):
        self.lock = threading.Lock()
        self._snap = {}
        self._last_net = psutil.net_io_counters()
        self._last_net_time = time.time()
        psutil.cpu_percent(interval=None)
        for p in psutil.process_iter(["pid"]):
            try:
                p.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        self.cpu_name = get_cpu_name()

        self.gpu_handle = None
        self.gpu_name = None
        if NVML_AVAILABLE:
            try:
                pynvml.nvmlInit()
                self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self.gpu_name = pynvml.nvmlDeviceGetName(self.gpu_handle)
                if isinstance(self.gpu_name, bytes):
                    self.gpu_name = self.gpu_name.decode()
            except Exception:
                self.gpu_handle = None

    def poll(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\" if sys.platform == "win32" else "/")

        now = time.time()
        net = psutil.net_io_counters()
        dt = max(now - self._last_net_time, 0.001)
        up_kbs = (net.bytes_sent - self._last_net.bytes_sent) / 1024 / dt
        down_kbs = (net.bytes_recv - self._last_net.bytes_recv) / 1024 / dt
        self._last_net = net
        self._last_net_time = now

        procs = []
        for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
            try:
                info = p.info
                if info["cpu_percent"] is None:
                    continue
                name = info["name"] or "?"
                # "System Idle Process" reports nonsense >100% values on Windows
                # due to how psutil sums idle time across cores - it's not a
                # real workload, so it's excluded rather than shown as noise.
                if name in ("System Idle Process", "System"):
                    continue
                procs.append({"name": name,
                              "cpu": round(info["cpu_percent"], 1),
                              "mem": round(info["memory_percent"] or 0.0, 1)})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x["cpu"], reverse=True)

        temp, power, lhm_connected = self._poll_lhm()
        gpu = self._poll_gpu()

        with self.lock:
            self._snap = {
                "cpu_name": self.cpu_name,
                "cpu_percent": round(cpu, 1),
                "ram_percent": round(mem.percent, 1),
                "ram_used_gb": round(mem.used / (1024 ** 3), 1),
                "ram_total_gb": round(mem.total / (1024 ** 3), 1),
                "cpu_temp": temp,
                "cpu_power": power,
                "lhm_connected": lhm_connected,
                "disk_percent": round(disk.percent, 1),
                "net_up_kbs": round(up_kbs, 1),
                "net_down_kbs": round(down_kbs, 1),
                "top_procs": procs[:5],
                "gpu": gpu,
            }

    def _poll_gpu(self):
        """Direct NVIDIA readings via NVML - no external app required, unlike
        CPU package power/temp which need LibreHardwareMonitor."""
        if self.gpu_handle is None:
            return None
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(self.gpu_handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(self.gpu_handle)
            temp = pynvml.nvmlDeviceGetTemperature(self.gpu_handle, pynvml.NVML_TEMPERATURE_GPU)
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(self.gpu_handle) / 1000.0  # mW -> W
            except Exception:
                power = None
            try:
                power_limit = pynvml.nvmlDeviceGetPowerManagementLimit(self.gpu_handle) / 1000.0
            except Exception:
                power_limit = 450.0  # reasonable ceiling for a 4090 if the query fails
            return {
                "name": self.gpu_name,
                "util_percent": util.gpu,
                "mem_used_gb": round(mem.used / (1024 ** 3), 1),
                "mem_total_gb": round(mem.total / (1024 ** 3), 1),
                "temp": temp,
                "power": round(power, 0) if power is not None else None,
                "power_limit": power_limit,
            }
        except Exception:
            return None

    def _poll_lhm(self):
        """Pull CPU package temp + power from LibreHardwareMonitor's web server.
        Returns (temp, power, connected) - connected=False means LHM itself
        isn't reachable (not running / web server not enabled), which the UI
        surfaces differently than 'sensor not found while LHM is running'."""
        if requests is None:
            return None, None, False
        try:
            resp = requests.get(LHM_URL, timeout=0.5)
            data = resp.json()
        except Exception:
            return None, None, False

        temp, power = None, None

        def walk(node):
            nonlocal temp, power
            text = node.get("Text", "")
            val = node.get("Value", "")
            if "Package" in text or "Core (Tctl" in text or "CPU Package" in text:
                if "°C" in val and temp is None:
                    try:
                        temp = float(val.replace("°C", "").strip())
                    except ValueError:
                        pass
                if " W" in val and power is None:
                    try:
                        power = float(val.replace(" W", "").strip())
                    except ValueError:
                        pass
            for child in node.get("Children", []):
                walk(child)

        try:
            walk(data)
        except Exception:
            pass
        return temp, power, True

    def snapshot(self):
        with self.lock:
            return dict(self._snap)


def poller_thread(store, stop_event):
    while not stop_event.is_set():
        try:
            store.poll()
        except Exception:
            pass
        stop_event.wait(POLL_INTERVAL_S)


# ----------------------------------------------------------------------------
# JS-facing API
# ----------------------------------------------------------------------------
class Api:
    def __init__(self, store, window_ref, tray_ctrl):
        self.store = store
        self.window_ref = window_ref
        self.tray_ctrl = tray_ctrl

    def get_stats(self):
        return self.store.snapshot()

    def get_accent(self):
        return ACCENT

    def minimize_to_tray(self):
        self.tray_ctrl["ensure_running"]()
        self.window_ref["win"].hide()
        return True

    def close_app(self):
        self.tray_ctrl["stop"]()
        self.window_ref["win"].destroy()
        return True

    def move_window(self, x, y):
        # Titlebar-only drag driven from JS (see app.js). Keeping drag scoped
        # to the titlebar - rather than pywebview's whole-window easy_drag -
        # means the minimize/close buttons stay reliably clickable.
        try:
            self.window_ref["win"].move(int(x), int(y))
        except Exception:
            pass
        return True


# ----------------------------------------------------------------------------
# Window shape
# ----------------------------------------------------------------------------
def disable_dwm_corner_rounding(window_title):
    """Windows 11 auto-rounds every top-level window at a fixed ~8px system
    radius. The panel already draws its own corners in CSS against a fully
    transparent window, so DWM's rounding would clip a second, differently
    shaped corner on top of ours - opt this window out so only the CSS shape
    is ever visible."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_DONOTROUND = 1

        hwnd = None
        for _ in range(50):
            hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
            if hwnd:
                break
            time.sleep(0.05)
        if not hwnd:
            return

        pref = ctypes.c_int(DWMWCP_DONOTROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
        )
    except Exception:
        pass


# ----------------------------------------------------------------------------
# System tray
# ----------------------------------------------------------------------------
def make_tray(window_ref, stop_event):
    if not TRAY_AVAILABLE:
        return {"ensure_running": lambda: None, "stop": lambda: stop_event.set()}

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 10, 54, 54), outline=ACCENT, width=5)
    d.ellipse((24, 24, 40, 40), fill=ACCENT)

    state = {"icon": None, "running": False}

    def show(icon=None, item=None):
        window_ref["win"].show()

    def quit_app(icon=None, item=None):
        stop_event.set()
        if state["icon"]:
            state["icon"].stop()
        window_ref["win"].destroy()

    menu = pystray.Menu(
        pystray.MenuItem("Show", show, default=True),
        pystray.MenuItem("Quit", quit_app),
    )
    state["icon"] = pystray.Icon("sysdash", img, "SysDash", menu)

    def ensure_running():
        if not state["running"]:
            state["running"] = True
            threading.Thread(target=state["icon"].run, daemon=True).start()

    def stop():
        stop_event.set()
        if state["running"]:
            state["icon"].stop()

    return {"ensure_running": ensure_running, "stop": stop}


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    store = DataStore()
    stop_event = threading.Event()
    threading.Thread(target=poller_thread, args=(store, stop_event), daemon=True).start()

    window_ref = {}
    tray_ctrl = {}

    api = Api(store, window_ref, tray_ctrl)

    win = webview.create_window(
        "SysDash",
        url=os.path.join(HERE, "web", "index.html"),
        width=WINDOW_W,
        height=WINDOW_H,
        frameless=True,
        easy_drag=False,
        on_top=True,
        transparent=True,
        js_api=api,
    )
    window_ref["win"] = win
    tray_ctrl.update(make_tray(window_ref, stop_event))

    def on_closed():
        stop_event.set()
        tray_ctrl["stop"]()

    win.events.closed += on_closed
    win.events.shown += lambda: threading.Thread(
        target=disable_dwm_corner_rounding, args=("SysDash",), daemon=True
    ).start()

    webview.start()


if __name__ == "__main__":
    main()
