"""Desktop integration: the tray icon, the compact panel window, starting at sign-in, running without a console.

Windows is the main target (tray in the notification area, Edge app window docked above it, a Run key
for sign-in). macOS and Linux get the same tray menu through pystray where a tray is available.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("redtooth.desktop")

APP_NAME = "Redtooth"
PANEL_W, PANEL_H = 400, 700


# ----------------------------------------------------------------- how to launch ourselves
def background_command(extra: tuple[str, ...] = ()) -> list[str]:
    """Run Redtooth with no console window (pythonw on Windows)."""
    exe = sys.executable
    if sys.platform.startswith("win"):
        w = Path(exe).with_name("pythonw.exe")
        if w.exists():
            exe = str(w)
    return [exe, "-m", "redtooth.cli", "serve", "--background", *extra]


def start_detached(cmd: list[str]) -> None:
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)


def is_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/app", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


# ----------------------------------------------------------------- start at sign-in
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_supported() -> bool:
    return sys.platform.startswith("win") or sys.platform == "darwin" or sys.platform.startswith("linux")


def _quoted(cmd: list[str]) -> str:
    return " ".join(f'"{c}"' if " " in c else c for c in cmd)


def autostart_enabled() -> bool:
    if sys.platform.startswith("win"):
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False
    return _autostart_file().exists()


def _autostart_file() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents/app.redtooth.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart/redtooth.desktop"


def set_autostart(on: bool) -> bool:
    cmd = background_command()
    if sys.platform.startswith("win"):
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _quoted(cmd))
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
        return autostart_enabled()
    f = _autostart_file()
    if not on:
        f.unlink(missing_ok=True)
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        args = "".join(f"<string>{c}</string>" for c in cmd)
        f.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                     '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>Label</key><string>app.redtooth</string>'
                     f"<key>ProgramArguments</key><array>{args}</array><key>RunAtLoad</key><true/></dict></plist>\n")
    else:
        f.write_text(f"[Desktop Entry]\nType=Application\nName=Redtooth\nExec={_quoted(cmd)}\nX-GNOME-Autostart-enabled=true\n")
    return True


# ----------------------------------------------------------------- the panel window
def _edge() -> Optional[str]:
    for p in (os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe")):
        if os.path.exists(p):
            return p
    return shutil.which("msedge")


def _chromium() -> Optional[str]:
    if sys.platform.startswith("win"):
        e = _edge()
        if e:
            return e
        for p in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")):
            if os.path.exists(p):
                return p
        return None
    for exe in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        if os.path.exists(exe):
            return exe
    for exe in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        p = shutil.which(exe)
        if p:
            return p
    return None


def _work_area() -> tuple[int, int, int, int]:
    """Usable screen area (excludes the taskbar) on Windows; a guess elsewhere."""
    if sys.platform.startswith("win"):
        try:
            class RECT(ctypes.Structure):
                _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

            r = RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
            scale = ctypes.windll.user32.GetDpiForSystem() / 96 if hasattr(ctypes.windll.user32, "GetDpiForSystem") else 1
            return int(r.l / scale), int(r.t / scale), int(r.r / scale), int(r.b / scale)
        except Exception:
            pass
    return 0, 0, 1440, 900


def _focus_existing() -> bool:
    """Bring an open Redtooth window to the front (Windows)."""
    if not sys.platform.startswith("win"):
        return False
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def each(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == APP_NAME or buf.value.startswith(APP_NAME + " "):
                found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    for hwnd in found:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    return False


_window: Optional[subprocess.Popen] = None


def _navigate(url: str, view: str) -> None:
    """Tell an open panel to switch views (it listens on its event stream)."""
    import json

    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/navigate", data=json.dumps({"to": view}).encode(),
                                     headers={"Content-Type": "application/json", "X-Redtooth": "1"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def open_panel(url: str, home: Optional[Path] = None, view: Optional[str] = None) -> None:
    """A compact app window docked above the tray (Edge/Chrome --app), else the default browser.
    If the window is already open it's brought to the front instead of opening another."""
    global _window
    if _window and _window.poll() is None and _focus_existing():
        if view:
            _navigate(url, view)
        return
    if view:
        url = f"{url}#{view}"
    browser = _chromium()
    if not browser:
        webbrowser.open(url)
        return
    l, t, r, b = _work_area()
    x, y = max(l, r - PANEL_W - 12), max(t, b - PANEL_H - 12)
    args = [browser, f"--app={url}", f"--window-size={PANEL_W},{PANEL_H}", f"--window-position={x},{y}", "--no-first-run",
            "--no-default-browser-check"]
    if home:  # its own profile, so size/position apply and it doesn't open as a tab in your browser
        args.append(f"--user-data-dir={home / 'window'}")
    _window = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ----------------------------------------------------------------- the tray icon
def tray_icon_image(size: int = 64, dot: Optional[str] = None):
    """Red rounded square with a white Bluetooth rune; an optional status dot."""
    from PIL import Image, ImageDraw

    s = size * 4  # draw large, scale down for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 4, fill=(224, 50, 43, 255))
    w = max(4, s // 13)
    cx, top, bot = s * 0.48, s * 0.18, s * 0.82
    left, right = s * 0.30, s * 0.70
    pts = [(left, s * 0.34), (right, s * 0.64), (cx, bot), (cx, top), (right, s * 0.36), (left, s * 0.66)]
    d.line(pts, fill="white", width=w, joint="curve")
    if dot:
        color = {"green": (52, 199, 89, 255), "blue": (10, 132, 255, 255), "orange": (255, 149, 0, 255)}.get(dot, (255, 255, 255, 255))
        r = s * 0.17
        d.ellipse((s - 2 * r - s * 0.02, s - 2 * r - s * 0.02, s - s * 0.02, s - s * 0.02), fill=color, outline=(255, 255, 255, 255), width=w // 2)
    return img.resize((size, size), Image.LANCZOS)


class Tray:
    """The notification-area icon. Left click opens the panel; right click shows the menu."""

    def __init__(self, url: str, home: Path, on_quit: Callable[[], None]):
        self.url, self.home, self.on_quit = url, home, on_quit
        self.icon = None
        self.status = "Starting…"

    def _menu(self):
        import pystray

        item = pystray.MenuItem
        return pystray.Menu(
            item("Open Redtooth", lambda: open_panel(self.url, self.home), default=True),
            item(lambda _: self.status, None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Add a device…", lambda: open_panel(self.url, self.home, "scan")),
            item("Your devices…", lambda: open_panel(self.url, self.home, "settings-devices")),
            item("Contacts…", lambda: open_panel(self.url, self.home, "settings-contacts")),
            pystray.Menu.SEPARATOR,
            item("Start when I sign in", lambda: set_autostart(not autostart_enabled()), checked=lambda _: autostart_enabled(),
                 visible=autostart_supported()),
            item("Quit Redtooth", self._quit),
        )

    def _quit(self) -> None:
        if self.icon:
            self.icon.stop()
        self.on_quit()

    def set_status(self, text: str, dot: Optional[str] = None) -> None:
        self.status = text
        if self.icon:
            self.icon.title = f"Redtooth · {text}"
            try:
                self.icon.icon = tray_icon_image(64, dot)
                self.icon.update_menu()
            except Exception:
                pass

    def run(self) -> None:
        """Blocks; call on the main thread (required on macOS)."""
        import pystray

        self.icon = pystray.Icon("redtooth", tray_icon_image(64), "Redtooth", self._menu())
        self.icon.run()


def tray_available() -> bool:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401

        return True
    except Exception:
        return False


def run_with_tray(serve: Callable[[Callable[[str, Optional[str]], None]], None], url: str, home: Path, open_at_start: bool) -> None:
    """Run the Redtooth service on a background thread and the tray icon on this (main) thread."""
    tray = Tray(url, home, on_quit=lambda: os._exit(0))

    def service() -> None:
        try:
            serve(tray.set_status)
        except Exception:
            log.exception("Redtooth stopped")
            os._exit(1)

    threading.Thread(target=service, name="redtooth-service", daemon=True).start()
    if open_at_start:
        def first_open() -> None:
            for _ in range(60):
                if is_running(int(url.rsplit(":", 1)[1].strip("/"))):
                    open_panel(url, home)
                    return
                time.sleep(0.5)

        threading.Thread(target=first_open, daemon=True).start()
    tray.run()
