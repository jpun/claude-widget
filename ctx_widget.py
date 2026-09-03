"""Claude Code context usage widget.

Shows the context fill of whichever session is open in the Claude desktop app,
as a small always-on-top window.

The app's usage ring draws the plan limit, but its accessible label still
carries the context numbers:

    "사용량: 5시간 한도의 39% 1시간 9분 후 재설정 컨텍스트 325.8k / 1M (33%)"

Reading that label through Windows UI Automation follows the app's own session
selection exactly. When the label has no context figures, the widget says so
rather than guessing from somewhere else.

Run ``python ctx_widget.py --self-check`` to verify the parsing logic.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import winreg
from pathlib import Path


def app_dir() -> Path:
    """Where settings and logs live - next to the .exe once frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def resource_path(name: str) -> Path:
    """Bundled read-only files; PyInstaller unpacks them to _MEIPASS."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / name if base else Path(__file__).with_name(name)


STATE_FILE = app_dir() / "ctx_widget.state.json"
DEBUG_LOG = app_dir() / "ctx_widget.debug.log"
ERROR_LOG = app_dir() / "ctx_widget.error.log"

REFRESH_MS = 2000
TRAY_POLL_MS = 120

SIZES = {
    "작게": {"main": 10, "sub": 7, "bar": 170, "bar_height": 4, "pad": 7},
    "보통": {"main": 12, "sub": 8, "bar": 210, "bar_height": 5, "pad": 10},
    "크게": {"main": 15, "sub": 10, "bar": 265, "bar_height": 7, "pad": 13},
}
DEFAULT_SIZE = "보통"

# Upper bound of the green, yellow and orange bands, in percent of the context
# window. Anything above the last one is red.
DEFAULT_THRESHOLDS = (40, 55, 70)

# Band colours differ per theme: the dark set is too pale to read on white.
THEMES = {
    "다크": {
        "bg": "#1c1c1c", "fg": "#e8e8e8", "dim": "#8a8a8a",
        "track": "#3a3a3a", "border": "#3a3a3a", "field": "#2a2a2a",
        "green": "#4ec9a0", "yellow": "#e5c07b",
        "orange": "#e08a4b", "red": "#e06c75",
    },
    "화이트": {
        "bg": "#f6f6f4", "fg": "#1b1b1b", "dim": "#6b6b6b",
        "track": "#dcdcd6", "border": "#c6c6c0", "field": "#ffffff",
        "green": "#0f8a5f", "yellow": "#a8790a",
        "orange": "#c05a10", "red": "#c0392b",
    },
}
DEFAULT_THEME = "다크"
BANDS = ("green", "yellow", "orange", "red")

CONTEXT_LABEL_RE = re.compile(
    r"(?:컨텍스트|Context)\s+([\d.]+)\s*([kKmM])?\s*/\s*([\d.]+)\s*([kKmM])?",
)
# The plan half of the label, i.e. everything before the context figures. Its
# wording changes with whichever window the app considers representative:
#   "사용량: 5시간 한도의 39% 1시간 9분 후 재설정"
#   "사용량: 주간 · 전체 모델: 12% (수) 오전 6:00에 재설정"
PLAN_SECTION_RE = re.compile(
    r"(?:사용량|Usage)\s*[:：]\s*(.+?)\s*(?:컨텍스트|Context|$)")
PLAN_PARTS_RE = re.compile(r"^(.*?)\s*[:：의]?\s*(\d+)\s*%\s*(.*)$")

ICON_FILE = resource_path("ctx_widget.ico")
TRAY_TIP = "Claude 컨텍스트"

WINDOW_CLASS = "Chrome_WidgetWin_1"
WINDOW_TITLE = "Claude"
APP_EXE = "claude.exe"

UIA_CONTROL_TYPE_PROPERTY = 30003
UIA_BUTTON_CONTROL_TYPE = 50000
TREE_SCOPE_DESCENDANTS = 4

_uia_client = None

# --- Win32 plumbing for the notification-area icon --------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_USER = 0x0400
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
HWND_MESSAGE = -3
IMAGE_ICON = 1
IDI_APPLICATION = 32512
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG)]


_user32.DefWindowProcW.restype = LRESULT
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                    wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.HWND,
                                    wintypes.HMENU, wintypes.HINSTANCE,
                                    wintypes.LPVOID]
_user32.LoadImageW.restype = wintypes.HANDLE
_user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                               wintypes.UINT, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT]
_user32.LoadIconW.restype = wintypes.HICON
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
_user32.RegisterWindowMessageW.restype = wintypes.UINT
_user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                       ctypes.POINTER(NOTIFYICONDATAW)]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
_user32.SetTimer.restype = ctypes.c_void_p
_user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT,
                             ctypes.c_void_p]
# Handles are pointer-sized; the default c_int return would truncate them.
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                  wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GWL_EXSTYLE = -20
MONITOR_DEFAULTTONEAREST = 2
SM_CXSCREEN, SM_CYSCREEN = 0, 1


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(MONITORINFO)]
WS_EX_TRANSPARENT = 0x00000020
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                      ctypes.c_ssize_t]


# --- reading the app --------------------------------------------------------

def scale_tokens(number: str, suffix: str | None) -> int:
    value = float(number)
    if suffix and suffix.lower() == "k":
        value *= 1_000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return int(round(value))


def parse_context_label(label: str) -> tuple[int, int] | None:
    """(used, max) tokens out of the usage ring's accessible label."""
    match = CONTEXT_LABEL_RE.search(label)
    if not match:
        return None
    used = scale_tokens(match.group(1), match.group(2))
    maximum = scale_tokens(match.group(3), match.group(4))
    if maximum <= 0:
        return None
    return used, maximum


def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_plan_label(label: str) -> str | None:
    """One tidy line for the plan window the app is currently reporting.

    "사용량: 주간 · 전체 모델: 12% (수) 오전 6:00에 재설정 컨텍스트 …"
        -> "주간·전체 모델 12% · 수 오전 6:00에 재설정"
    """
    section = PLAN_SECTION_RE.search(label)
    if not section:
        return None
    parts = PLAN_PARTS_RE.match(tidy(section.group(1)))
    if not parts:
        return None
    window = tidy(parts.group(1)).replace(" · ", "·").rstrip(":：의")
    line = f"{window} {parts.group(2)}%" if window else f"{parts.group(2)}%"
    head, reset, _tail = tidy(parts.group(3)).partition("재설정")
    when = tidy(head).replace("(", "").replace(")", "")
    if reset and when:
        line += f" · {when} 재설정"
    return line


def process_name(hwnd: int) -> str | None:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                   pid.value)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                    ctypes.byref(size)):
            return None
        return Path(buffer.value).name.lower()
    finally:
        _kernel32.CloseHandle(handle)


def claude_windows() -> list[int]:
    """Claude windows, the one the user is looking at first.

    EnumWindows walks the Z-order top down, so the foreground window (when it
    is Claude) and then the frontmost Claude window lead the list.
    """
    candidates: list[int] = []

    def looks_right(hwnd: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return False
        buffer = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, buffer, 256)
        if buffer.value != WINDOW_CLASS:
            return False
        _user32.GetWindowTextW(hwnd, buffer, 256)
        if buffer.value != WINDOW_TITLE:
            return False
        return process_name(hwnd) == APP_EXE

    # Both sources are normalised to a plain int, so the de-duplication below
    # compares like with like.
    foreground = _user32.GetForegroundWindow() or 0
    if foreground and looks_right(foreground):
        candidates.append(int(foreground))

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _lparam):
        handle = int(hwnd)
        if handle not in candidates and looks_right(handle):
            candidates.append(handle)
        return True

    _user32.EnumWindows(collect, 0)
    return candidates


def work_area(hwnd: int) -> tuple[int, int, int, int]:
    """Usable bounds of the monitor the window sits on, taskbar excluded."""
    monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if monitor and _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        rect = info.rcWork
        return rect.left, rect.top, rect.right, rect.bottom
    return (0, 0, _user32.GetSystemMetrics(SM_CXSCREEN),
            _user32.GetSystemMetrics(SM_CYSCREEN))


def clamp_to_area(x: int, y: int, width: int, height: int,
                  area: tuple[int, int, int, int], margin: int = 8
                  ) -> tuple[int, int]:
    """Nudge a window back inside *area* - it opens near, not off, the screen."""
    left, top, right, bottom = area
    x = min(x, right - width - margin)
    y = min(y, bottom - height - margin)
    return max(x, left + margin), max(y, top + margin)


def set_click_through(hwnd: int, enabled: bool) -> None:
    """Let clicks fall through an invisible window instead of vanishing."""
    try:
        style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TRANSPARENT) if enabled \
            else (style & ~WS_EX_TRANSPARENT)
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    except OSError:
        pass


def uia_client():
    global _uia_client
    if _uia_client is None:
        import comtypes.client

        module = comtypes.client.GetModule("UIAutomationCore.dll")
        _uia_client = comtypes.client.CreateObject(
            module.CUIAutomation, interface=module.IUIAutomation)
    return _uia_client


def read_window(hwnd: int):
    """(context, plan line) read off one Claude window."""
    try:
        client = uia_client()
        root = client.ElementFromHandle(hwnd)
        condition = client.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY,
                                                   UIA_BUTTON_CONTROL_TYPE)
        buttons = root.FindAll(TREE_SCOPE_DESCENDANTS, condition)
    except Exception:  # COM is allowed to fail; the widget just says 알 수 없음
        return None

    context: tuple[int, int] | None = None
    plan: str | None = None
    for index in range(buttons.Length):
        try:
            name = buttons.GetElement(index).CurrentName or ""
        except Exception:
            continue
        if context is None:
            context = parse_context_label(name)
        if plan is None:
            plan = parse_plan_label(name)
        if context and plan:
            break
    if context is None and plan is None:
        return None
    return context, plan


def read_active_session():
    """What the app is showing right now, or None if it cannot be read."""
    for hwnd in claude_windows():
        found = read_window(hwnd)
        if found:
            return found
    return None


# --- presentation -----------------------------------------------------------

def format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def elide(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def normalize_thresholds(values) -> tuple[int, int, int]:
    """Three ascending percentages, whatever the settings file or user typed."""
    try:
        numbers = [int(round(float(v))) for v in values][:3]
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLDS
    if len(numbers) != 3:
        return DEFAULT_THRESHOLDS
    numbers = [min(100, max(1, n)) for n in numbers]
    numbers.sort()
    return tuple(numbers)  # type: ignore[return-value]


def band_for(percent: float, thresholds: tuple[int, int, int]) -> str:
    """Which colour band a fill percentage falls in, theme-independent."""
    green, yellow, orange = thresholds
    if percent <= green:
        return "green"
    if percent <= yellow:
        return "yellow"
    if percent <= orange:
        return "orange"
    return "red"


def drag_target(press_pointer: tuple[int, int], press_window: tuple[int, int],
                pointer: tuple[int, int]) -> tuple[int, int]:
    """Where the window goes, measured from where the drag started.

    Deliberately absolute rather than incremental: ``winfo_x()`` still reports
    the old position until Tk has processed the move, so accumulating deltas
    against it pins the window in place.
    """
    return (press_window[0] + pointer[0] - press_pointer[0],
            press_window[1] + pointer[1] - press_pointer[1])


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeContextWidget"


def autostart_target() -> Path:
    """The file that is actually started - the .exe, or this script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def autostart_command() -> str:
    """The command Windows should run at logon, quoted for the registry."""
    target = autostart_target()
    if getattr(sys, "frozen", False):  # built as a standalone .exe
        return f'"{target}"'
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.exists():
        interpreter = windowless
    return f'"{interpreter}" "{target}"'


def command_target(command: str) -> Path | None:
    """The launched file inside a registry command - its last quoted path."""
    quoted = re.findall(r'"([^"]+)"', command)
    return Path(quoted[-1]) if quoted else None


def autostart_registered() -> str | None:
    """The command currently registered for logon, if any."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, RUN_VALUE)
        return value
    except OSError:
        return None


def autostart_enabled() -> bool:
    return autostart_registered() is not None


def refresh_autostart() -> None:
    """Repoint a stale entry - the folder may have been moved or renamed.

    Only within the same kind of launcher: running the script once must not
    hijack an entry that points at the built .exe, or the next logon would
    start the source copy instead.
    """
    registered = autostart_registered()
    if registered is None or registered == autostart_command():
        return
    target = command_target(registered)
    if target is None or target.suffix.lower() != autostart_target().suffix.lower():
        return
    try:
        set_autostart(True)
    except OSError:
        pass


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                              autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except OSError:
                pass


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


class Tray:
    """Notification-area icon, owned by its own thread.

    The icon needs a window, and a window belongs to the thread that created
    it. Created on the Tk thread, its messages get dispatched by Tk's own
    message loop, which runs with the GIL released - the process then dies
    with "PyEval_RestoreThread: ... the GIL is released" the first time
    Windows calls back into Python that way. Owning a private thread with a
    private message loop keeps the two apart.

    Nothing here touches Tk. Clicks go into a queue that the Tk side drains.
    """

    MESSAGE = WM_USER + 20
    TIP_TIMER = 1
    TIP_INTERVAL_MS = 1000

    def __init__(self) -> None:
        self.events: queue.Queue[str] = queue.Queue()
        self.tip = TRAY_TIP
        self.posted_tip = None
        self.hwnd = 0
        # Windows sends messages during CreateWindowExW, before the rest of
        # create_window() has run.
        self.taskbar_created = 0
        self.icon = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self.run, name="tray",
                                       daemon=True)
        self.thread.start()
        self.ready.wait(timeout=5)
        if self.error:
            raise self.error

    # ---- tray thread ---------------------------------------------------
    def run(self) -> None:
        try:
            self.create_window()
        except BaseException as error:  # reported to the caller in __init__
            self.error = error
            self.ready.set()
            return
        self.ready.set()

        message = MSG()
        while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(message))
            _user32.DispatchMessageW(ctypes.byref(message))
        # WM_CLOSE removes the icon; this covers the loop ending any other way
        # (GetMessageW returning -1), where it would otherwise be left behind.
        self.remove_icon()

    def create_window(self) -> None:
        self.instance = _kernel32.GetModuleHandleW(None)
        # ctypes must own a reference for as long as Windows can call back.
        self.window_proc = WNDPROC(self.on_message)
        self.class_name = f"CtxWidgetTray{os.getpid()}"
        window_class = WNDCLASSW()
        window_class.lpfnWndProc = self.window_proc
        window_class.hInstance = self.instance
        window_class.lpszClassName = self.class_name
        if not _user32.RegisterClassW(ctypes.byref(window_class)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = _user32.CreateWindowExW(0, self.class_name, TRAY_TIP, 0,
                                            0, 0, 0, 0, HWND_MESSAGE, None,
                                            self.instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.icon = self.load_icon()
        self.taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
        self.notify(NIM_ADD)
        self.posted_tip = self.tip
        _user32.SetTimer(self.hwnd, self.TIP_TIMER, self.TIP_INTERVAL_MS, None)

    def load_icon(self):
        handle = _user32.LoadImageW(None, str(ICON_FILE), IMAGE_ICON, 0, 0,
                                    LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if not handle:
            handle = _user32.LoadIconW(
                None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION),
                                  wintypes.LPCWSTR))
        return handle

    def notify(self, action: int) -> bool:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        data.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP
        data.uCallbackMessage = self.MESSAGE
        data.hIcon = self.icon
        data.szTip = self.tip[:127]
        return bool(_shell32.Shell_NotifyIconW(action, ctypes.byref(data)))

    def on_message(self, hwnd, message, wparam, lparam):
        if message == self.MESSAGE:
            if lparam == WM_LBUTTONUP:
                self.events.put("toggle")
            elif lparam == WM_RBUTTONUP:
                self.events.put("menu")
            return 0
        if message == WM_TIMER and wparam == self.TIP_TIMER:
            if self.tip != self.posted_tip:
                self.posted_tip = self.tip
                self.notify(NIM_MODIFY)
            return 0
        if message == self.taskbar_created:
            # Explorer restarted and forgot every icon; put ours back.
            self.notify(NIM_ADD)
            return 0
        if message == WM_CLOSE:
            # Drop the icon while its window still exists; deleting it after
            # DestroyWindow leaves a dead icon in the tray until Explorer
            # notices.
            self.remove_icon()
            _user32.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def remove_icon(self) -> None:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = 1
        _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))

    # ---- Tk thread -----------------------------------------------------
    def update_tip(self, text: str) -> None:
        self.tip = text  # picked up by the tray thread's timer

    def drain(self) -> list[str]:
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def close(self) -> None:
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self.thread.join(timeout=2)
            self.hwnd = 0


class Widget:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.hidden = False
        self.tray: Tray | None = None
        self.settings: SettingsDialog | None = None
        state = load_state()
        self.size = state.get("size") if state.get("size") in SIZES else DEFAULT_SIZE
        self.theme = state.get("theme") if state.get("theme") in THEMES \
            else DEFAULT_THEME
        self.thresholds = normalize_thresholds(
            state.get("thresholds") or DEFAULT_THRESHOLDS)

        root.title("Claude 컨텍스트")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.shown_geometry = state.get("geometry") or "+40+40"
        root.geometry(self.shown_geometry)

        self.frame = tk.Frame(root, highlightthickness=1)
        self.frame.pack(fill="both", expand=True)

        # Fixed widths: a window that resizes as the numbers change slips out
        # from under the pointer mid-drag.
        self.main = tk.Label(self.frame, text="…", width=22, anchor="w")
        self.main.pack(fill="x")

        self.bar = tk.Canvas(self.frame, highlightthickness=0)
        self.bar.pack(fill="x")
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, 0, width=0)

        self.sub = tk.Label(self.frame, text="세션 찾는 중", width=38, anchor="w")
        self.sub.pack(fill="x")
        self.apply_size()
        self.apply_theme()

        self.menu = tk.Menu(root, tearoff=0)
        self.build_menu()

        # Binding the toplevel is enough: every child's bindtags include it, so
        # presses on the labels and the bar all arrive here. Binding the
        # children as well would run each handler twice.
        root.bind("<Button-1>", self.on_press)
        root.bind("<B1-Motion>", self.on_drag)
        root.bind("<ButtonRelease-1>", self.on_release)
        root.bind("<Button-3>", self.on_menu)
        root.bind("<Escape>", lambda _event: self.quit())

        self.press_pointer = (0, 0)
        self.press_window = (0, 0)
        self.tokens: int | None = None
        self.max_now = 0
        self.detail = "세션 찾는 중"
        self.refresh()

    def start_tray(self) -> None:
        try:
            self.tray = Tray()
        except Exception:  # no notification area - the widget still runs
            self.tray = None
            return
        self.poll_tray()

    def poll_tray(self) -> None:
        if not self.tray:
            return
        for event in self.tray.drain():
            if event == "toggle":
                self.toggle_visible()
            elif event == "menu":
                self.popup_menu_from_tray()
        self.root.after(TRAY_POLL_MS, self.poll_tray)

    def toggle_visible(self) -> None:
        # Made transparent rather than withdrawn or moved off-screen: a menu
        # posts only from a mapped, on-screen toplevel, and the tray menu is
        # the way back. -disabled keeps the invisible window from eating clicks.
        self.hidden = not self.hidden
        self.root.attributes("-alpha", 0.0 if self.hidden else 1.0)
        set_click_through(self.root.winfo_id(), self.hidden)
        if not self.hidden:
            self.root.attributes("-topmost", True)

    def popup_menu_from_tray(self) -> None:
        self.build_menu()
        x, y = self.root.winfo_pointerxy()
        try:
            # A tray menu has to own the foreground, or it stays up after the
            # click that should dismiss it.
            self.root.focus_force()
        except tk.TclError:
            pass
        self.popup_menu(x, y)

    def popup_menu(self, x: int, y: int) -> None:
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()

    # ---- window chrome -------------------------------------------------
    @property
    def colors(self) -> dict[str, str]:
        return THEMES[self.theme]

    def apply_theme(self) -> None:
        colors = self.colors
        self.root.configure(bg=colors["bg"])
        self.frame.configure(bg=colors["bg"],
                             highlightbackground=colors["border"])
        self.main.configure(bg=colors["bg"], fg=colors["fg"])
        self.sub.configure(bg=colors["bg"], fg=colors["fg"])
        self.bar.configure(bg=colors["track"])

    def set_theme(self, name: str) -> None:
        # An unknown name would leave self.colors raising KeyError for good.
        if name not in THEMES:
            return
        self.theme = name
        self.apply_theme()
        self.persist()
        self.render()

    def apply_size(self) -> None:
        spec = SIZES[self.size]
        self.frame.configure(padx=spec["pad"], pady=max(4, spec["pad"] - 3))
        self.main.configure(font=("Consolas", spec["main"]))
        self.sub.configure(font=("Segoe UI", spec["sub"]))
        self.bar.configure(width=spec["bar"], height=spec["bar_height"])
        self.bar.pack_configure(pady=(spec["pad"] // 2, spec["pad"] // 2 + 1))

    def build_menu(self) -> None:
        self.menu.delete(0, "end")
        if self.tray:
            self.menu.add_command(label="표시" if self.hidden else "숨기기",
                                  command=self.toggle_visible)
            self.menu.add_separator()
        self.menu.add_command(label="설정…", command=self.open_settings)
        self.menu.add_separator()
        self.menu.add_command(label="종료", command=self.quit)

    def set_size(self, name: str) -> None:
        if name not in SIZES:
            return
        self.size = name
        self.apply_size()
        self.persist()
        self.render()

    def open_settings(self) -> None:
        # One dialog only: a second one holds a stale copy of the settings and
        # would write them back over the first one's 적용.
        if self.settings and self.settings.top.winfo_exists():
            self.settings.top.lift()
            self.settings.top.focus_force()
            return
        self.settings = SettingsDialog(self)

    def log(self, text: str) -> None:
        if "--debug" not in sys.argv:
            return
        with open(DEBUG_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.3f} {text}\n")

    def on_press(self, event: tk.Event) -> None:
        self.press_pointer = (event.x_root, event.y_root)
        self.press_window = (self.root.winfo_x(), self.root.winfo_y())
        self.log(f"press {self.press_pointer} window {self.press_window}")

    def on_drag(self, event: tk.Event) -> None:
        target = drag_target(self.press_pointer, self.press_window,
                             (event.x_root, event.y_root))
        self.root.geometry(f"+{target[0]}+{target[1]}")
        self.log(f"drag -> {target} winfo={self.root.winfo_x()},"
                 f"{self.root.winfo_y()}")

    def on_release(self, _event: tk.Event) -> None:
        self.log("release")
        if not self.hidden:
            self.shown_geometry = self.current_geometry()
        self.persist()

    def on_menu(self, event: tk.Event) -> None:
        self.build_menu()
        self.popup_menu(event.x_root, event.y_root)

    def current_geometry(self) -> str:
        return f"+{self.root.winfo_x()}+{self.root.winfo_y()}"

    def persist(self) -> None:
        save_state({
            "size": self.size,
            "theme": self.theme,
            "thresholds": list(self.thresholds),
            # While hidden the window sits off-screen; save where it belongs.
            "geometry": self.shown_geometry if self.hidden
                        else self.current_geometry(),
        })

    def quit(self) -> None:
        self.persist()
        if self.tray:
            self.tray.close()
        self.root.destroy()

    # ---- data ----------------------------------------------------------
    def refresh(self) -> None:
        active = read_active_session()
        if active is None:
            self.tokens = None
            self.detail = "클로드 앱을 못 찾음"
        else:
            context, plan = active
            self.tokens, self.max_now = context if context else (None, 0)
            self.detail = plan or "알 수 없음"
        self.render()
        if self.tray:
            self.tray.update_tip(self.tip_text())
        self.root.after(REFRESH_MS, self.refresh)

    def percent(self) -> float | None:
        """Context fill, or None when there is nothing to show."""
        if self.tokens is None or self.max_now <= 0:
            return None
        return min(self.tokens / self.max_now, 1.0) * 100

    def tip_text(self) -> str:
        percent = self.percent()
        if percent is None:
            return f"{TRAY_TIP} · 알 수 없음"
        return f"{TRAY_TIP} {percent:.1f}%\n{self.detail}"

    def render(self) -> None:
        height = SIZES[self.size]["bar_height"]
        self.sub.configure(text=elide(self.detail, 34))
        percent = self.percent()
        if percent is None:
            self.main.configure(text="알 수 없음", fg=self.colors["dim"])
            self.bar.coords(self.bar_fill, 0, 0, 0, height)
            return

        color = self.colors[band_for(percent, self.thresholds)]
        self.main.configure(
            text=f"{percent:4.1f}%   "
                 f"{format_tokens(self.tokens)} / {format_tokens(self.max_now)}",
            fg=color,
        )
        # Before the window is mapped winfo_width() is 1, not 0, so the bar
        # would be drawn one pixel wide on the very first paint.
        width = self.bar.winfo_width()
        if width <= 1:
            width = SIZES[self.size]["bar"]
        self.bar.coords(self.bar_fill, 0, 0, round(width * percent / 100), height)
        self.bar.itemconfigure(self.bar_fill, fill=color)


class SettingsDialog:
    """설정 - size, colour bands and autostart, applied on 적용."""

    def __init__(self, widget: Widget) -> None:
        self.widget = widget
        colors = widget.colors
        top = tk.Toplevel(widget.root)
        self.top = top
        top.title("설정")
        top.configure(bg=colors["bg"], padx=14, pady=12)
        top.attributes("-topmost", True)
        top.resizable(False, False)

        def caption(text: str, pady=(0, 2)) -> None:
            tk.Label(top, text=text, bg=colors["bg"], fg=colors["dim"],
                     font=("Segoe UI", 9)).pack(anchor="w", pady=pady)

        def choice(parent, text, value, variable, command) -> None:
            tk.Radiobutton(parent, text=text, value=value, variable=variable,
                           command=command, bg=colors["bg"], fg=colors["fg"],
                           activebackground=colors["bg"],
                           activeforeground=colors["fg"],
                           selectcolor=colors["field"], highlightthickness=0,
                           bd=0, font=("Segoe UI", 9)).pack(side="left",
                                                            padx=(0, 8))

        caption("테마")
        self.theme = tk.StringVar(value=widget.theme)
        themes = tk.Frame(top, bg=colors["bg"])
        themes.pack(fill="x", pady=(2, 10))
        for name in THEMES:
            choice(themes, name, name, self.theme, self.apply_theme)

        caption("크기")
        self.size = tk.StringVar(value=widget.size)
        sizes = tk.Frame(top, bg=colors["bg"])
        sizes.pack(fill="x", pady=(2, 10))
        for name in SIZES:
            choice(sizes, name, name, self.size, self.apply_size)

        caption("색 구간")
        rows = (("이하 초록", "green"), ("이하 노랑", "yellow"),
                ("이하 주황", "orange"))
        self.entries: list[tk.Entry] = []
        for index, (label, band) in enumerate(rows):
            line = tk.Frame(top, bg=colors["bg"])
            line.pack(fill="x", pady=3)
            tk.Label(line, text="■", bg=colors["bg"], fg=colors[band],
                     font=("Segoe UI", 11)).pack(side="left")
            entry = tk.Entry(line, width=5, justify="right",
                             bg=colors["field"], fg=colors["fg"],
                             insertbackground=colors["fg"], relief="flat",
                             font=("Consolas", 11))
            entry.insert(0, str(widget.thresholds[index]))
            entry.pack(side="left", padx=6)
            tk.Label(line, text=f"% {label}", bg=colors["bg"], fg=colors["fg"],
                     font=("Segoe UI", 9)).pack(side="left")
            self.entries.append(entry)

        tk.Label(top, text="그 이상은 빨강", bg=colors["bg"], fg=colors["red"],
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 10))

        self.autostart = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(top, text="윈도우 시작 시 자동 실행",
                       variable=self.autostart, bg=colors["bg"],
                       fg=colors["fg"], activebackground=colors["bg"],
                       activeforeground=colors["fg"],
                       selectcolor=colors["field"], highlightthickness=0, bd=0,
                       font=("Segoe UI", 9),
                       anchor="w").pack(fill="x", pady=(0, 10))

        buttons = tk.Frame(top, bg=colors["bg"])
        buttons.pack(fill="x")
        tk.Button(buttons, text="기본값", command=self.reset, relief="flat",
                  bg=colors["field"], fg=colors["fg"], font=("Segoe UI", 9),
                  padx=8).pack(side="left")
        tk.Button(buttons, text="적용", command=self.apply, relief="flat",
                  bg=colors["field"], fg=colors["fg"], font=("Segoe UI", 9),
                  padx=12).pack(side="right")
        top.bind("<Return>", lambda _event: self.apply())
        top.bind("<Escape>", lambda _event: top.destroy())
        self.entries[0].focus_set()
        self.place_near_widget()

    def place_near_widget(self) -> None:
        """Open beside the widget, but never past the edge of the screen."""
        root = self.widget.root
        self.top.update_idletasks()
        # winfo_reqheight misses the title bar and frame; add what Windows draws.
        width = self.top.winfo_reqwidth() + 16
        height = self.top.winfo_reqheight() + 40
        x, y = clamp_to_area(root.winfo_x() + 20, root.winfo_y() + 40,
                             width, height, work_area(root.winfo_id()))
        self.top.geometry(f"+{x}+{y}")

    def apply_size(self) -> None:
        # Size and theme are the settings worth seeing straight away.
        self.widget.set_size(self.size.get())

    def apply_theme(self) -> None:
        self.widget.set_theme(self.theme.get())
        # Rebuild the dialog so its own colours follow the widget's.
        self.top.destroy()
        self.widget.settings = None
        self.widget.open_settings()

    def reset(self) -> None:
        for entry, value in zip(self.entries, DEFAULT_THRESHOLDS):
            entry.delete(0, "end")
            entry.insert(0, str(value))

    def apply(self) -> None:
        self.widget.thresholds = normalize_thresholds(
            [entry.get() for entry in self.entries])
        wanted = self.autostart.get()
        if wanted != autostart_enabled():
            try:
                set_autostart(wanted)
            except OSError:
                pass
        self.widget.persist()
        self.widget.render()
        self.top.destroy()


def self_check() -> None:
    import tempfile

    label = "사용량: 5시간 한도의 39% 1시간 9분 후 재설정 컨텍스트 325.8k / 1M (33%)"
    assert parse_context_label(label) == (325800, 1_000_000)
    assert parse_context_label("View usage limits · Context 12.5k / 200k (6%)") \
        == (12500, 200_000)
    assert parse_context_label("사용량: 5시간 한도의 39% 54분 후 재설정") is None
    assert parse_plan_label(label) == "5시간 한도 39% · 1시간 9분 후 재설정"
    assert parse_plan_label("사용량: 5시간 한도의 44% 54분 후 재설정") \
        == "5시간 한도 44% · 54분 후 재설정"
    # The app switched wording once already; both shapes have to survive.
    assert parse_plan_label(
        "사용량: 주간 · 전체 모델: 12% (수) 오전 6:00에 재설정 컨텍스트 91.4k / 1M (9%)") \
        == "주간·전체 모델 12% · 수 오전 6:00에 재설정"
    assert parse_plan_label("사용량: 주간 한도의 7%") == "주간 한도 7%"
    assert parse_plan_label("보내기") is None
    assert parse_plan_label("사용량: 한도 없음") is None

    # Registry commands: only the same kind of launcher may repoint an entry.
    exe_command = r'"C:\Programs\ClaudeContextWidget.exe"'
    script_command = r'"C:\py\pythonw.exe" "E:\w\ctx_widget.py"'
    assert command_target(exe_command) == Path(r"C:\Programs\ClaudeContextWidget.exe")
    assert command_target(script_command) == Path(r"E:\w\ctx_widget.py")
    assert command_target(exe_command).suffix == ".exe"
    assert command_target(script_command).suffix == ".py"
    assert command_target("no quotes here") is None

    # Bands are inclusive of their upper bound: 40% is still green.
    assert band_for(0, DEFAULT_THRESHOLDS) == "green"
    assert band_for(40, DEFAULT_THRESHOLDS) == "green"
    assert band_for(40.1, DEFAULT_THRESHOLDS) == "yellow"
    assert band_for(55, DEFAULT_THRESHOLDS) == "yellow"
    assert band_for(55.1, DEFAULT_THRESHOLDS) == "orange"
    assert band_for(70, DEFAULT_THRESHOLDS) == "orange"
    assert band_for(70.1, DEFAULT_THRESHOLDS) == "red"
    assert band_for(100, DEFAULT_THRESHOLDS) == "red"
    assert band_for(45, (10, 20, 30)) == "red"

    # Every theme must answer for every colour the UI asks it for.
    keys = set(THEMES[DEFAULT_THEME])
    assert set(BANDS) <= keys
    for name, palette in THEMES.items():
        assert set(palette) == keys, name
        assert all(value.startswith("#") for value in palette.values()), name

    assert normalize_thresholds(["70", "40", "55"]) == (40, 55, 70)
    assert normalize_thresholds([0, 500, 55]) == (1, 55, 100)
    assert normalize_thresholds(["", "x", "3"]) == DEFAULT_THRESHOLDS
    assert normalize_thresholds([40, 55]) == DEFAULT_THRESHOLDS
    assert normalize_thresholds(None) == DEFAULT_THRESHOLDS

    assert format_tokens(136280) == "136.3k"
    assert format_tokens(1_000_000) == "1.00M"
    assert format_tokens(42) == "42"
    assert elide("짧은 제목", 34) == "짧은 제목"
    assert elide("x" * 40, 10) == "x" * 9 + "…"

    # Registry command must be quoted, so a path with spaces still launches.
    command = autostart_command()
    assert command.startswith('"'), command
    assert command.count('"') in (2, 4), command
    assert command.rstrip('"').lower().endswith((".exe", "ctx_widget.py")), command

    # A dialog opened next to a widget parked at the bottom right must slide
    # back onto the screen instead of hanging off it.
    screen = (0, 0, 1920, 1040)
    assert clamp_to_area(300, 300, 260, 340, screen) == (300, 300)
    assert clamp_to_area(1700, 900, 260, 340, screen) == (1652, 692)
    assert clamp_to_area(-50, -80, 260, 340, screen) == (8, 8)
    # Second monitor to the left: clamping respects its own bounds.
    assert clamp_to_area(-1900, 900, 260, 340, (-1920, 0, 0, 1040)) \
        == (-1900, 692)

    # Absolute-from-press math: repeating the same motion event must land the
    # window in the same place, and never fold in a stale window position.
    assert drag_target((100, 100), (1325, 356), (150, 130)) == (1375, 386)
    assert drag_target((100, 100), (1325, 356), (150, 130)) == (1375, 386)
    assert drag_target((100, 100), (1325, 356), (100, 100)) == (1325, 356)
    assert drag_target((100, 100), (1325, 356), (60, 70)) == (1285, 326)

    # Redirect the settings file so the check never touches the real one.
    saved_state_file = STATE_FILE
    scratch = tempfile.TemporaryDirectory()
    globals()["STATE_FILE"] = Path(scratch.name) / "state.json"

    root = tk.Tk()
    root.withdraw()
    widget = Widget(root)
    assert root.bind("<Button-1>")
    assert root.bind("<B1-Motion>")
    # The toplevel carries the bindings, and the children must NOT repeat them
    # (their bindtags already include the toplevel, so each handler would run
    # twice and the second run would undo the first).
    for child in (widget.main, widget.bar, widget.sub):
        assert not child.bind("<Button-1>"), child

    # Nothing to read means "알 수 없음", never a number from elsewhere.
    widget.tokens = None
    widget.detail = "테스트"
    widget.render()
    assert widget.main.cget("text") == "알 수 없음"

    # Every size actually resizes the text and the bar.
    seen = set()
    for name in SIZES:
        widget.set_size(name)
        seen.add((widget.main.cget("font"), int(widget.bar.cget("width"))))
    assert len(seen) == len(SIZES), seen

    # Every theme actually repaints, and a bad name is ignored rather than
    # leaving self.colors raising KeyError from then on.
    painted = set()
    for name in THEMES:
        widget.set_theme(name)
        painted.add(widget.main.cget("bg"))
    assert len(painted) == len(THEMES), painted
    widget.set_theme("없는테마")
    assert widget.theme in THEMES
    widget.set_size("없는크기")
    assert widget.size in SIZES

    root.destroy()
    globals()["STATE_FILE"] = saved_state_file
    scratch.cleanup()

    print("self-check ok")


def report_exception(exc_type, value, traceback_object) -> None:
    import traceback

    text = "".join(traceback.format_exception(exc_type, value,
                                              traceback_object))
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
    except OSError:
        pass


def main() -> None:
    if "--self-check" in sys.argv:
        self_check()
        return
    root = tk.Tk()
    # Built with pythonw there is no stderr, and Tk's default handler writing
    # to it would take the process down with the error it was reporting.
    root.report_callback_exception = report_exception
    refresh_autostart()
    widget = Widget(root)
    widget.start_tray()
    root.mainloop()


if __name__ == "__main__":
    main()
