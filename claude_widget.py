"""Claude widget - a small always-on-top window beside the Claude desktop app.

It shows two things, both read off the app through Windows UI Automation:

* The context fill of whichever session is open. Since the session panel
  came in, the figures live in its "컨텍스트" card ("300.3k / 1M"); older
  builds put them in the usage ring's label instead
  ("사용량: … 컨텍스트 325.8k / 1M (33%)"). Both are read.
* The sessions waiting for the user. The sidebar row of such a session says
  so in its accessible name ("PhotobookServer 리뷰 입력 대기 중 …"); the
  widget lists them on a blue band, and a click opens that session.

When a value cannot be read, the widget says so rather than guessing from
somewhere else.

Run ``python claude_widget.py --self-check`` to verify the parsing logic.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import glob
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
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


# One JSON per Code session, written by the app: cwd, title, timestamps (ms).
SESSIONS_GLOB = os.path.join(os.environ.get("APPDATA", ""), "Claude",
                             "claude-code-sessions", "*", "*", "local_*.json")

STATE_FILE = app_dir() / "claude_widget.state.json"
# Settings from before the rename, read once when the new file is missing.
OLD_STATE_FILE = app_dir() / "ctx_widget.state.json"
DEBUG_LOG = app_dir() / "claude_widget.debug.log"
ERROR_LOG = app_dir() / "claude_widget.error.log"

REFRESH_MS = 2000
TRAY_POLL_MS = 120

# Malgun Gothic carries Hangul itself; Segoe UI did not, so Windows drew the
# Korean in a substitute font that clashed with the Latin at small sizes. The
# English family name resolves on every Windows locale.
BODY_FONT = "Malgun Gothic"

SIZES = {
    "작게": {"main": 10, "sub": 8, "bar": 170, "bar_height": 4, "pad": 7},
    "보통": {"main": 12, "sub": 9, "bar": 210, "bar_height": 5, "pad": 10},
    "크게": {"main": 15, "sub": 11, "bar": 265, "bar_height": 7, "pad": 13},
}
DEFAULT_SIZE = "보통"

# Window width. Each size has its own default (the old fixed widths); the
# user can drag the right edge, and that width is kept for every size.
DEFAULT_WIDTHS = {"작게": 222, "보통": 270, "크게": 320}
MIN_WIDTH, MAX_WIDTH = 180, 1400
EDGE_PX = 6  # how close to the right edge a press starts a resize

# Upper bound of the green, yellow and orange bands, in percent of the context
# window. Anything above the last one is red.
DEFAULT_THRESHOLDS = (40, 55, 70)

# Which sidebar statuses the widget lists, and as what. The labels are the
# app's own (ion-dist/i18n/ko-KR.json): "Awaiting input", "Awaiting answer"
# and "Unread response".
STATUS_KINDS = {
    "입력 대기 중": "awaiting",
    "답변 대기 중": "awaiting",
    "읽지 않은 응답": "ready",
}
# Waiting rows first, unread after.
KIND_ORDER = ("awaiting", "ready")
# The other statuses, listed under the folded "다른 세션" line in this order.
# The app writes a status only while a session runs, waits or is unread; rows
# without one are left out.
OTHER_ORDER = ("실행 중", "브라우저 사용", "유휴", "완료됨")
# Statuses whose time counts from the start of the current turn ("3분째");
# the rest count from the session's last activity ("5분 전").
WORKING = ("실행 중", "브라우저 사용")
# ponytail: a fixed cap instead of a scrolling list; the sidebar has the rest.
# Make it scroll if 20 turns out too few.
OTHERS_MAX = 20

# The app's own colours for those dots, from its design tokens (ion-dist CSS):
# awaiting is --cds-fill-warning (yellow-200), ready is --cds-fill-accent
# (blue-450); hover is the -hover token, text the role's "on" colour.
KIND_COLORS = {
    "awaiting": {"bg": "#fab219", "hover": "#eda100",
                 "fg": "#0b0b0b", "dim": "#383835"},
    "ready": {"bg": "#2a78d6", "hover": "#3987e5",
              "fg": "#ffffff", "dim": "#d4e4f7"},
}

# Band colours differ per theme: the dark set is too pale to read on white.
THEMES = {
    "다크": {
        "bg": "#1c1c1c", "fg": "#e8e8e8", "dim": "#8a8a8a", "soft": "#c8c8c8",
        "track": "#3a3a3a", "border": "#3a3a3a", "field": "#2a2a2a",
        "green": "#4ec9a0", "yellow": "#e5c07b",
        "orange": "#e08a4b", "red": "#e06c75",
    },
    "화이트": {
        "bg": "#f6f6f4", "fg": "#1b1b1b", "dim": "#6b6b6b", "soft": "#3c3c3c",
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
# "재설정" became "초기화" in a later build; keep both.
RESET_WORD_RE = re.compile(r"재설정|초기화")
# The session panel's context card: one text element holds just the figures.
CARD_CONTEXT_RE = re.compile(
    r"\s*([\d.]+)\s*([kKmM])?\s*/\s*([\d.]+)\s*([kKmM])?\s*")
CONTEXT_CARD_NAME = "컨텍스트"

# A sidebar row is a button followed by its "<title>에 대한 더 많은 옵션"
# button, which gives the bare title. The row's status ("실행 중", "입력 대기
# 중", "읽지 않은 응답") is a text inside its hidden tooltip (class "sr-only");
# the row's own name leaves it out, and the summary line under the title is
# free text that can say "입력 대기 중" without the session waiting at all.
OPTIONS_SUFFIX = "에 대한 더 많은 옵션"
STATUS_CLASS = "sr-only"
FOLDER_CLASS = "df-meta-place"
# The line under the title: Claude's summary of the session, then the folder.
META_CLASS = "df-meta-slots"
# Before the folder got its own slot it was the last text of this line.
OLD_FOLDER_LINE_CLASS = "df-meta-line"

ICON_FILE = resource_path("claude_widget.ico")
TRAY_TIP = "Claude 위젯"

WINDOW_CLASS = "Chrome_WidgetWin_1"
WINDOW_TITLE = "Claude"
APP_EXE = "claude.exe"

UIA_CONTROL_TYPE_PROPERTY = 30003
UIA_NAME_PROPERTY = 30005
UIA_BUTTON_CONTROL_TYPE = 50000
UIA_TEXT_CONTROL_TYPE = 50020
UIA_GROUP_CONTROL_TYPE = 50026
UIA_INVOKE_PATTERN = 10000
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
SW_RESTORE = 9


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
    rest = tidy(parts.group(3))
    reset = RESET_WORD_RE.search(rest)
    when = tidy(rest[:reset.start()] if reset else "") \
        .replace("(", "").replace(")", "")
    if reset and when:
        line += f" · {when} {reset.group(0)}"
    return line


def parse_card_context(text: str) -> tuple[int, int] | None:
    """(used, max) tokens out of the context card's "300.3k / 1M" text."""
    match = CARD_CONTEXT_RE.fullmatch(text)
    if not match:
        return None
    used = scale_tokens(match.group(1), match.group(2))
    maximum = scale_tokens(match.group(3), match.group(4))
    if maximum <= 0:
        return None
    return used, maximum


def sidebar_rows(names: list[str]) -> list[tuple[int, str]]:
    """(index, title) of the session rows in the sidebar.

    *names* are the window's button names in document order. A row counts
    only when its own "더 많은 옵션" button follows it; that button also gives
    the title without the rest the row's own name runs on with. A row with
    nested sessions has its "중첩된 세션 표시" button in between, hence the
    short look back rather than the very previous button.
    """
    found = []
    for options_index, options in enumerate(names):
        if not options.endswith(OPTIONS_SUFFIX):
            continue
        title = options[:-len(OPTIONS_SUFFIX)]
        for index in range(options_index - 1, max(-1, options_index - 4), -1):
            name = names[index]
            if name.endswith(OPTIONS_SUFFIX):
                break  # the previous row's; this one has no row button
            if name.startswith(title):
                found.append((index, title))
                break
    return found


def parse_row_parts(parts: list[tuple[str, str]]) -> tuple[str, str, str]:
    """(status, folder, summary) out of a row's texts.

    *parts* are (text, classes of the ancestors inside the row) for every
    text in the row, in order. Status is the text in the hidden tooltip;
    folder is the text in the folder slot, or on older builds the last text
    of the meta line; summary is the line Claude writes about the session
    (the meta slots' other text).
    """
    status = folder = summary = ""
    old_line: list[str] = []
    for text, classes in parts:
        if not text:
            continue
        if STATUS_CLASS in classes and not status:
            status = text
        elif FOLDER_CLASS in classes and not folder:
            folder = text
        elif META_CLASS in classes and not summary:
            summary = text
        elif OLD_FOLDER_LINE_CLASS in classes:
            old_line.append(text)
    return status, folder or (old_line[-1] if old_line else ""), summary


_meta_cache: dict[str, tuple[float, dict]] = {}


def session_meta() -> dict[str, dict]:
    """The app's session files by title, re-reading only changed files.

    Titles can repeat (forks keep a name); the live, most recently active one
    wins, since that is the one the sidebar shows.
    """
    by_title: dict[str, dict] = {}
    seen = set()
    for path in glob.glob(SESSIONS_GLOB):
        seen.add(path)
        try:
            mtime = os.path.getmtime(path)
            cached = _meta_cache.get(path)
            if not cached or cached[0] != mtime:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                cached = (mtime, {key: data.get(key) for key in (
                    "title", "cwd", "isArchived", "lastActivityAt",
                    "lastTurnEdgeAt")})
                _meta_cache[path] = cached
        except (OSError, ValueError):
            continue  # being rewritten right now; next read gets it
        meta = cached[1]
        title = meta.get("title")
        if not title:
            continue
        rank = (not meta.get("isArchived"), meta.get("lastActivityAt") or 0)
        best = by_title.get(title)
        if best is None or rank > best["_rank"]:
            by_title[title] = {**meta, "_rank": rank}
    for path in set(_meta_cache) - seen:
        del _meta_cache[path]
    return by_title


def duration(ms: float) -> str:
    """'1분 미만', '7분', '2시간 5분', '3일'."""
    minutes = int(ms // 60000)
    if minutes < 1:
        return "1분 미만"
    if minutes < 60:
        return f"{minutes}분"
    if minutes < 24 * 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"
    return f"{minutes // (24 * 60)}일"


def describe(status: str, folder: str, summary: str, meta: dict | None,
             now_ms: float) -> str:
    """The second line of a row: folder · how long · Claude's summary."""
    if meta and meta.get("cwd"):
        folder = os.path.basename(meta["cwd"].rstrip("\\/")) or folder
    parts = [folder] if folder else []
    if meta:
        working = status in WORKING
        since = meta.get("lastTurnEdgeAt" if working else "lastActivityAt")
        if since:
            took = duration(max(0, now_ms - since))
            if working:
                parts.append(f"{took}째")
            elif STATUS_KINDS.get(status) == "awaiting":
                parts.append(f"{took}째 대기")
            else:
                parts.append(f"{took} 전")
    if summary:
        parts.append(summary)
    return " · ".join(parts)


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


def read_context_card(client, root) -> tuple[int, int] | None:
    """Figures from the session panel's 컨텍스트 card, if the panel shows it."""
    condition = client.CreateAndCondition(
        client.CreatePropertyCondition(UIA_NAME_PROPERTY, CONTEXT_CARD_NAME),
        client.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY,
                                       UIA_GROUP_CONTROL_TYPE))
    card = root.FindFirst(TREE_SCOPE_DESCENDANTS, condition)
    if not card:
        return None
    texts = card.FindAll(TREE_SCOPE_DESCENDANTS, client.CreatePropertyCondition(
        UIA_CONTROL_TYPE_PROPERTY, UIA_TEXT_CONTROL_TYPE))
    for index in range(texts.Length):
        found = parse_card_context(texts.GetElement(index).CurrentName or "")
        if found:
            return found
    return None


def read_window(hwnd: int):
    """(context, plan line, waiting sessions, other sessions) off one window.

    Waiting sessions are (title, info line, kind, row element), waiting ones
    before unread; other sessions are (title, info line, status, row element)
    in OTHER_ORDER. The info line is describe()'s folder · time · summary.
    The element is what a click on the widget invokes.
    """
    try:
        client = uia_client()
        root = client.ElementFromHandle(hwnd)
        condition = client.CreatePropertyCondition(UIA_CONTROL_TYPE_PROPERTY,
                                                   UIA_BUTTON_CONTROL_TYPE)
        buttons = root.FindAll(TREE_SCOPE_DESCENDANTS, condition)
        context = read_context_card(client, root)
    except Exception:  # COM is allowed to fail; the widget just says 알 수 없음
        return None

    plan: str | None = None
    names: list[str] = []
    for index in range(buttons.Length):
        try:
            name = buttons.GetElement(index).CurrentName or ""
        except Exception:
            name = ""
        names.append(name)
        if context is None:
            context = parse_context_label(name)
        if plan is None:
            plan = parse_plan_label(name)
    waiting, others = [], []
    metas = session_meta()
    now_ms = time.time() * 1000
    for index, title in sidebar_rows(names):
        row = buttons.GetElement(index)
        status, folder, summary = parse_row_parts(row_parts(row))
        if not status:
            continue
        info = describe(status, folder, summary, metas.get(title), now_ms)
        kind = STATUS_KINDS.get(status)
        if kind:
            waiting.append((title, info, kind, row))
        else:
            others.append((title, info, status, row))
    waiting.sort(key=lambda entry: KIND_ORDER.index(entry[2]))
    others.sort(key=lambda entry: other_rank(entry[2]))
    if context is None and plan is None and not waiting and not others:
        return None
    return context, plan, waiting, others


def other_rank(status: str) -> int:
    """Sort key for the other statuses; unknown ones go last."""
    return OTHER_ORDER.index(status) if status in OTHER_ORDER \
        else len(OTHER_ORDER)


def other_summary(statuses: list[str]) -> str:
    """'실행 중 2 · 유휴 1' for the folded line."""
    counts: dict[str, int] = {}
    for status in sorted(statuses, key=other_rank):
        counts[status] = counts.get(status, 0) + 1
    return " · ".join(f"{status} {count}" for status, count in counts.items())


def fit(text: str, font: tkfont.Font, width: int) -> str:
    """*text* cut with … so it measures at most *width* pixels."""
    if width <= 0:
        return ""
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…" if text else ""


def row_parts(row) -> list[tuple[str, str]]:
    """(text, ancestor classes) for every text inside a sidebar row.

    The status and the folder are not control elements, so FindAll (control
    view) skips them; the raw view walker reaches them.
    """
    parts: list[tuple[str, str]] = []
    try:
        walker = uia_client().RawViewWalker

        def visit(element, classes: str) -> None:
            child = walker.GetFirstChildElement(element)
            while child:
                if child.CurrentControlType == UIA_TEXT_CONTROL_TYPE:
                    parts.append((tidy(child.CurrentName or ""), classes))
                visit(child, f"{classes} {child.CurrentClassName or ''}")
                child = walker.GetNextSiblingElement(child)

        visit(row, "")
    except Exception:  # the row went away mid-read; it counts as not waiting
        pass
    return parts


def open_session(hwnd: int, row) -> None:
    """Bring the app forward and select the session behind a sidebar row."""
    try:
        import comtypes.client

        module = comtypes.client.GetModule("UIAutomationCore.dll")
        pattern = row.GetCurrentPattern(UIA_INVOKE_PATTERN)
        pattern.QueryInterface(module.IUIAutomationInvokePattern).Invoke()
    except Exception:  # the row went away since the last read; nothing to do
        return
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)


def read_active_session():
    """What the app is showing right now, or None if it cannot be read."""
    for hwnd in claude_windows():
        found = read_window(hwnd)
        if found:
            return hwnd, found
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
RUN_VALUE = "ClaudeWidget"
# The entry's name before the rename; taken over on the first launch.
OLD_RUN_VALUE = "ClaudeContextWidget"


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


def autostart_registered(name: str = RUN_VALUE) -> str | None:
    """The command currently registered for logon, if any."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None


def autostart_enabled() -> bool:
    return autostart_registered() is not None


def refresh_autostart() -> None:
    """Repoint a stale entry - the folder may have been moved or renamed.

    Only within the same kind of launcher: running the script once must not
    hijack an entry that points at the built .exe, or the next logon would
    start the source copy instead. An entry under the old name moves to the
    new one on the same terms.
    """
    old = autostart_registered(OLD_RUN_VALUE)
    registered = autostart_registered() or old
    if registered is None:
        return
    if registered == autostart_command() and old is None:
        return
    target = command_target(registered)
    if target is None or target.suffix.lower() != autostart_target().suffix.lower():
        return
    try:
        set_autostart(True)
        if old is not None:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.DeleteValue(key, OLD_RUN_VALUE)
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
    source = STATE_FILE if STATE_FILE.exists() else OLD_STATE_FILE
    try:
        return json.loads(source.read_text(encoding="utf-8"))
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
        self.class_name = f"ClaudeWidgetTray{os.getpid()}"
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
        width = state.get("width")
        self.width: int | None = min(MAX_WIDTH, max(MIN_WIDTH, width)) \
            if isinstance(width, int) else None
        self.others_open = bool(state.get("others_open"))

        root.title("Claude 위젯")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.shown_geometry = state.get("geometry") or "+40+40"
        root.geometry(self.shown_geometry)

        self.frame = tk.Frame(root, highlightthickness=1)
        self.frame.pack(fill="both", expand=True)

        # The window's width is set outright (place() gives it), never taken
        # from the text: a window that resizes as the numbers change slips out
        # from under the pointer mid-drag. Long text is cut to fit instead.
        self.main = tk.Label(self.frame, text="…", width=1, anchor="w")
        self.main.pack(fill="x")

        self.bar = tk.Canvas(self.frame, highlightthickness=0, width=1)
        self.bar.pack(fill="x")
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, 0, width=0)

        self.sub = tk.Label(self.frame, text="세션 찾는 중", width=1, anchor="w")
        self.sub.pack(fill="x")

        # Sessions waiting for input or unread; packed only while there are any.
        self.waiting_box = tk.Frame(self.frame)
        # One frame per listed session, holding its labels.
        self.waiting_rows: list[tk.Frame] = []
        # (title, folder, kind) of the rows on screen.
        self.shown_titles: list[tuple[str, str, str]] = []

        # Every other session with a status, folded under one line.
        self.others_box = tk.Frame(self.frame)
        self.others_toggle = tk.Label(self.others_box, anchor="w", width=1,
                                      cursor="hand2")
        self.others_toggle.pack(fill="x")
        self.others_list = tk.Frame(self.others_box)
        self.others_rows: list[tk.Frame] = []
        self.shown_others: tuple[list, int] | None = None

        # What the last read found; apply_size() below already lays it out.
        self.tokens: int | None = None
        self.max_now = 0
        self.detail = "세션 찾는 중"
        self.app_hwnd = 0
        self.app_found = False
        self.waiting: list[tuple[str, str, str, object]] = []
        self.others: list[tuple[str, str, str, object]] = []
        # (title, kind) of the rows the user closed with ✕.
        self.dismissed: set[tuple[str, str]] = set()
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
        root.bind("<Motion>", self.on_motion)
        root.bind("<Escape>", lambda _event: self.quit())

        self.press_pointer = (0, 0)
        self.press_window = (0, 0)
        self.resizing = False
        self.press_width = 0
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
                             highlightbackground=self.border_color())
        self.main.configure(bg=colors["bg"], fg=colors["fg"])
        self.sub.configure(bg=colors["bg"], fg=colors["fg"])
        self.bar.configure(bg=colors["track"])
        self.waiting_box.configure(bg=colors["bg"])
        for row in self.waiting_rows:
            self.paint_row(row)
        self.others_box.configure(bg=colors["bg"])
        self.others_list.configure(bg=colors["bg"])
        self.others_toggle.configure(bg=colors["bg"], fg=colors["fg"])
        for row in self.others_rows:
            if hasattr(row, "key"):
                self.paint_other(row)
            else:  # the "… 외 N개" line
                row.configure(bg=colors["bg"], fg=colors["soft"])

    def set_theme(self, name: str) -> None:
        # An unknown name would leave self.colors raising KeyError for good.
        if name not in THEMES:
            return
        self.theme = name
        self.apply_theme()
        self.persist()
        self.render()

    def fonts(self) -> dict[str, tuple]:
        spec = SIZES[self.size]
        return {"main": ("Consolas", spec["main"]),
                "sub": (BODY_FONT, spec["sub"]),
                "title": (BODY_FONT, spec["sub"], "bold"),
                "small": (BODY_FONT, max(7, spec["sub"] - 1))}

    def window_width(self) -> int:
        return self.width or DEFAULT_WIDTHS[self.size]

    def text_width(self, inset: int = 0) -> int:
        """Pixels a full-width line of text gets, less *inset*."""
        pad = SIZES[self.size]["pad"]
        return self.window_width() - 2 * pad - 2 - inset

    def apply_size(self) -> None:
        spec = SIZES[self.size]
        fonts = self.fonts()
        self.frame.configure(padx=spec["pad"], pady=max(4, spec["pad"] - 3))
        self.main.configure(font=fonts["main"])
        self.sub.configure(font=fonts["sub"])
        self.others_toggle.configure(font=fonts["title"])
        self.bar.configure(height=spec["bar_height"])
        self.bar.pack_configure(pady=(spec["pad"] // 2, spec["pad"] // 2 + 1))
        self.relayout()

    def relayout(self) -> None:
        """Rebuild what depends on the font or width, then resize."""
        self.shown_titles = []
        self.shown_others = None
        self.show_waiting()
        self.show_others()
        self.render()
        self.place()

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

    def near_right_edge(self, x_root: int) -> bool:
        right = self.root.winfo_rootx() + self.root.winfo_width()
        return right - EDGE_PX <= x_root <= right

    def on_motion(self, event: tk.Event) -> None:
        # Children without a cursor of their own inherit this one.
        cursor = "sb_h_double_arrow" if self.near_right_edge(event.x_root) \
            else ""
        if self.frame.cget("cursor") != cursor:
            self.frame.configure(cursor=cursor)

    def on_press(self, event: tk.Event) -> None:
        self.press_pointer = (event.x_root, event.y_root)
        self.press_window = (self.root.winfo_x(), self.root.winfo_y())
        self.resizing = self.near_right_edge(event.x_root)
        self.press_width = self.root.winfo_width()
        self.log(f"press {self.press_pointer} window {self.press_window}")

    def on_drag(self, event: tk.Event) -> None:
        if self.resizing:
            width = self.press_width + event.x_root - self.press_pointer[0]
            width = min(MAX_WIDTH, max(MIN_WIDTH, width))
            if width != self.window_width():
                self.width = width
                self.relayout()
            return
        target = drag_target(self.press_pointer, self.press_window,
                             (event.x_root, event.y_root))
        self.root.geometry(f"+{target[0]}+{target[1]}")
        self.log(f"drag -> {target} winfo={self.root.winfo_x()},"
                 f"{self.root.winfo_y()}")

    def on_release(self, event: tk.Event) -> None:
        self.log("release")
        if self.resizing:
            self.resizing = False
            self.persist()
            return
        moved = max(abs(event.x_root - self.press_pointer[0]),
                    abs(event.y_root - self.press_pointer[1])) > 3
        if not moved:
            # A click, not a drag. Tk delivers the release to the widget that
            # got the press, so this is what the click started on.
            self.on_click(event.widget)
            return
        if not self.hidden:
            self.shown_geometry = self.current_geometry()
        self.persist()

    def on_click(self, widget) -> None:
        if widget is self.others_toggle:
            self.others_open = not self.others_open
            self.shown_others = None
            self.show_others()
            self.persist()
            return
        row = widget if hasattr(widget, "key") else getattr(widget, "master", None)
        if not hasattr(row, "key"):
            return
        title, status = row.key
        if widget is getattr(row, "close", None):
            self.dismiss(title, status)
            return
        # The element from the latest read, not the one the row was built
        # with: rows outlive many reads.
        for entry in (*self.waiting, *self.others):
            if (entry[0], entry[2]) == row.key:
                open_session(self.app_hwnd, entry[3])
                return

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
            # Where the user put it, not where place() nudged it to make room
            # for the waiting list.
            "geometry": self.shown_geometry,
            "width": self.width,
            "others_open": self.others_open,
        })

    def place(self) -> None:
        """Back at the user's spot, moved up only as far as the list needs."""
        match = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", self.shown_geometry)
        if not match:
            return
        self.root.update_idletasks()
        width, height = self.window_width(), self.root.winfo_reqheight()
        x, y = clamp_to_area(int(match.group(1)), int(match.group(2)),
                             width, height, work_area(self.root.winfo_id()))
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def border_color(self) -> str:
        """The first listed kind's colour while anything is listed."""
        if self.shown_titles:
            return KIND_COLORS[self.shown_titles[0][2]]["bg"]
        return self.colors["border"]

    def paint_row(self, row: tk.Frame, hover: bool = False) -> None:
        palette = KIND_COLORS[row.kind]
        bg = palette["hover" if hover else "bg"]
        row.configure(bg=bg)
        for label in (row.title, row.close):
            label.configure(bg=bg, fg=palette["fg"])
        row.folder.configure(bg=bg, fg=palette["dim"])

    def paint_other(self, row: tk.Frame, hover: bool = False) -> None:
        colors = self.colors
        bg = colors["track" if hover else "field"]
        row.configure(bg=bg)
        row.title.configure(bg=bg, fg=colors["fg"])
        row.status.configure(bg=bg, fg=colors["green"])
        row.folder.configure(bg=bg, fg=colors["soft"])

    def bind_hover(self, row: tk.Frame, paint) -> None:
        # Enter/Leave are not bound on the toplevel, so binding them here
        # does not run anything twice. Each part repaints the whole row:
        # moving onto a label sends the frame a Leave first.
        for part in (row, *row.winfo_children()):
            part.bind("<Enter>", lambda _e, r=row: paint(r, True))
            part.bind("<Leave>", lambda _e, r=row: paint(r))

    def dismiss(self, title: str, kind: str) -> None:
        """Hide one row until its session leaves that status and comes back."""
        self.dismissed.add((title, kind))
        self.waiting = [entry for entry in self.waiting
                        if (entry[0], entry[2]) != (title, kind)]
        self.show_waiting()
        if self.tray:
            self.tray.update_tip(self.tip_text())

    def show_waiting(self) -> None:
        """Rebuild the rows - only when the set of sessions changed."""
        titles = [(title, folder, kind)
                  for title, folder, kind, _row in self.waiting]
        if titles == self.shown_titles:
            return
        self.shown_titles = titles
        for row in self.waiting_rows:
            row.destroy()
        self.waiting_rows = []
        fonts = self.fonts()
        title_font = tkfont.Font(font=fonts["title"])
        small_font = tkfont.Font(font=fonts["small"])
        close_width = tkfont.Font(font=fonts["title"]).measure("✕") + 12
        room = self.text_width(inset=12 + close_width)
        for title, folder, kind in titles:
            row = tk.Frame(self.waiting_box, padx=6, pady=2, cursor="hand2")
            row.kind = kind
            row.key = (title, kind)
            row.columnconfigure(0, weight=1)
            row.title = tk.Label(row, text=fit(f"● {title}", title_font, room),
                                 anchor="w", width=1, cursor="hand2",
                                 font=fonts["title"])
            row.title.grid(row=0, column=0, sticky="we")
            row.close = tk.Label(row, text="✕", cursor="hand2", padx=4,
                                 font=fonts["title"])
            row.close.grid(row=0, column=1, rowspan=2, sticky="ne")
            row.folder = tk.Label(row, text=fit(f"   {folder}", small_font, room),
                                  anchor="w", width=1, cursor="hand2",
                                  font=fonts["small"])
            if folder:
                row.folder.grid(row=1, column=0, sticky="we")
            row.pack(fill="x", pady=(2, 0))
            self.paint_row(row)
            self.bind_hover(row, self.paint_row)
            self.waiting_rows.append(row)
        if titles:
            self.waiting_box.pack(fill="x", pady=(4, 0))
        else:
            self.waiting_box.pack_forget()
        self.frame.configure(highlightbackground=self.border_color())
        self.place()

    def show_others(self) -> None:
        """The folded line, and under it the other sessions when open."""
        shown = [(title, folder, status)
                 for title, folder, status, _row in self.others[:OTHERS_MAX]] \
            if self.others_open else []
        rest = len(self.others) - len(shown) if self.others_open else 0
        summary = other_summary([entry[2] for entry in self.others])
        fonts = self.fonts()
        toggle_font = tkfont.Font(font=fonts["title"])
        arrow = "▾" if self.others_open else "▸"
        self.others_toggle.configure(text=fit(
            f"{arrow} 다른 세션 {len(self.others)}  ·  {summary}", toggle_font,
            self.text_width()))
        if self.others:
            self.others_box.pack(fill="x", pady=(4, 0))
        else:
            self.others_box.pack_forget()
        if (shown, rest) == self.shown_others:
            self.place()
            return
        self.shown_others = (shown, rest)
        for row in self.others_rows:
            row.destroy()
        self.others_rows = []
        title_font = tkfont.Font(font=fonts["title"])
        info_font = tkfont.Font(font=fonts["sub"])
        for title, folder, status in shown:
            row = tk.Frame(self.others_list, padx=6, pady=3, cursor="hand2")
            row.key = (title, status)
            row.columnconfigure(0, weight=1)
            status_width = title_font.measure(status) + 8
            room = self.text_width(inset=12 + status_width)
            row.title = tk.Label(row, text=fit(title, title_font, room),
                                 anchor="w", width=1, cursor="hand2",
                                 font=fonts["title"])
            row.title.grid(row=0, column=0, sticky="we")
            row.status = tk.Label(row, text=status, cursor="hand2",
                                  font=fonts["title"])
            row.status.grid(row=0, column=1, sticky="e")
            row.folder = tk.Label(row, text=fit(folder, info_font,
                                                self.text_width(inset=12)),
                                  anchor="w", width=1, cursor="hand2",
                                  font=fonts["sub"])
            if folder:
                row.folder.grid(row=1, column=0, columnspan=2, sticky="we")
            row.pack(fill="x", pady=(2, 0))
            self.paint_other(row)
            self.bind_hover(row, self.paint_other)
            self.others_rows.append(row)
        if rest:
            more = tk.Label(self.others_list,
                            text=f"… 외 {rest}개 (사이드바에서 확인)",
                            anchor="w", width=1, font=fonts["sub"],
                            bg=self.colors["bg"], fg=self.colors["soft"])
            more.pack(fill="x", pady=(1, 0))
            self.others_rows.append(more)
        if shown:
            self.others_list.pack(fill="x")
        else:
            self.others_list.pack_forget()
        self.place()

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
            self.waiting = []
            self.others = []
            self.app_found = False
        else:
            self.app_found = True
            self.app_hwnd, (context, plan, waiting, self.others) = active
            # A hidden row stays hidden only while its session keeps waiting;
            # once it stops, the next wait shows it again.
            self.dismissed &= {(title, kind)
                               for title, _folder, kind, _row in waiting}
            self.waiting = [entry for entry in waiting
                            if (entry[0], entry[2]) not in self.dismissed]
            self.tokens, self.max_now = context if context else (None, 0)
            self.detail = plan or "알 수 없음"
        self.render()
        self.show_waiting()
        self.show_others()
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
        waiting = f"\n확인 필요 {len(self.waiting)}" if self.waiting else ""
        if percent is None:
            return f"{TRAY_TIP} · 알 수 없음{waiting}"
        return f"{TRAY_TIP} {percent:.1f}%\n{self.detail}{waiting}"

    def render(self) -> None:
        height = SIZES[self.size]["bar_height"]
        self.sub.configure(text=fit(self.detail, tkfont.Font(
            font=self.fonts()["sub"]), self.text_width()))
        percent = self.percent()
        if percent is None:
            # The figures now live in the session panel; with the panel shut
            # there is nothing to read, so say where they would come from.
            text = "알 수 없음 · 세션 패널" if self.app_found else "알 수 없음"
            self.main.configure(text=text, fg=self.colors["dim"])
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
            width = self.text_width()
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
                     font=(BODY_FONT, 9)).pack(anchor="w", pady=pady)

        def choice(parent, text, value, variable, command) -> None:
            tk.Radiobutton(parent, text=text, value=value, variable=variable,
                           command=command, bg=colors["bg"], fg=colors["fg"],
                           activebackground=colors["bg"],
                           activeforeground=colors["fg"],
                           selectcolor=colors["field"], highlightthickness=0,
                           bd=0, font=(BODY_FONT, 9)).pack(side="left",
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
                     font=(BODY_FONT, 11)).pack(side="left")
            entry = tk.Entry(line, width=5, justify="right",
                             bg=colors["field"], fg=colors["fg"],
                             insertbackground=colors["fg"], relief="flat",
                             font=("Consolas", 11))
            entry.insert(0, str(widget.thresholds[index]))
            entry.pack(side="left", padx=6)
            tk.Label(line, text=f"% {label}", bg=colors["bg"], fg=colors["fg"],
                     font=(BODY_FONT, 9)).pack(side="left")
            self.entries.append(entry)

        tk.Label(top, text="그 이상은 빨강", bg=colors["bg"], fg=colors["red"],
                 font=(BODY_FONT, 9)).pack(anchor="w", pady=(2, 10))

        self.autostart = tk.BooleanVar(value=autostart_enabled())
        tk.Checkbutton(top, text="윈도우 시작 시 자동 실행",
                       variable=self.autostart, bg=colors["bg"],
                       fg=colors["fg"], activebackground=colors["bg"],
                       activeforeground=colors["fg"],
                       selectcolor=colors["field"], highlightthickness=0, bd=0,
                       font=(BODY_FONT, 9),
                       anchor="w").pack(fill="x", pady=(0, 10))

        buttons = tk.Frame(top, bg=colors["bg"])
        buttons.pack(fill="x")
        tk.Button(buttons, text="기본값", command=self.reset, relief="flat",
                  bg=colors["field"], fg=colors["fg"], font=(BODY_FONT, 9),
                  padx=8).pack(side="left")
        tk.Button(buttons, text="적용", command=self.apply, relief="flat",
                  bg=colors["field"], fg=colors["fg"], font=(BODY_FONT, 9),
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
    # Later wording: "모든 모델", "초기화", no context half.
    assert parse_plan_label("사용량: 주간 · 모든 모델: 80% 7시간 4분 후 초기화") \
        == "주간·모든 모델 80% · 7시간 4분 후 초기화"
    assert parse_context_label("사용량: 주간 · 모든 모델: 80% 7시간 4분 후 초기화") \
        is None

    # The session panel's context card.
    assert parse_card_context("300.3k / 1M") == (300300, 1_000_000)
    assert parse_card_context("12.5k/200k") == (12500, 200_000)
    assert parse_card_context("30% 사용됨") is None
    assert parse_card_context("21/47 페이지") is None

    # Sidebar rows: only a row followed by its own options button counts, and
    # the title comes without the rest of the row's name.
    names = [
        "새로 만들기",
        "PhotobookServer 리뷰 열림 · 풀 리퀘스트 4개",
        "중첩된 세션 표시 PhotobookServer 리뷰",
        "PhotobookServer 리뷰에 대한 더 많은 옵션",
        "앱 UI 확인필요 표시 강조 빠른 실행 - 데스크에서 포크됨 desk",
        "앱 UI 확인필요 표시 강조에 대한 더 많은 옵션",
        "도움말",
        "설정",
        "기존 API 조사에 대한 더 많은 옵션",
    ]
    assert sidebar_rows(names) == [(1, "PhotobookServer 리뷰"),
                                   (4, "앱 UI 확인필요 표시 강조")]
    assert sidebar_rows([]) == []
    assert sidebar_rows(["x 입력 대기 중"]) == []

    # Status comes only from the hidden tooltip, never from the summary line.
    # Real shapes, read off the app on 2026-11-09:
    waiting_row = [
        ("앱 UI 확인필요 표시 강조", " dframe-fade-label block"),
        ("빠른 실행 - 데스크에서 포크됨", " dframe-fade-label block"),
        ("입력 대기 중", " dframe-fade-label block sr-only [[role=tooltip]_&]:hidden"),
        ("오른쪽 끝에 강제로 닫는 엑스버튼 만들어줘", " df-meta-slots flex shrink-0"),
        ("desk", " df-meta-slots flex df-meta-place shrink-0"),
    ]
    assert parse_row_parts(waiting_row) \
        == ("입력 대기 중", "desk", "오른쪽 끝에 강제로 닫는 엑스버튼 만들어줘")
    summary_says_waiting = [
        ("치이워크 피드백 세션", " dframe-fade-label"),
        ("chiiwork · 연결됨", " dframe-fade-label"),
        ("피드백 파일 준비됨; 치이워크 피드백 입력 대기 중", " df-meta-slots shrink-0"),
        ("chiiwork", " df-meta-slots df-meta-place shrink-0"),
    ]
    assert parse_row_parts(summary_says_waiting) \
        == ("", "chiiwork", "피드백 파일 준비됨; 치이워크 피드백 입력 대기 중")
    running = [("세션", " dframe-fade-label"),
               ("실행 중", " dframe-fade-label sr-only")]
    assert parse_row_parts(running) == ("실행 중", "", "")
    # Older build: no folder slot, folder last on the meta line.
    older = [("데스크", " dframe-fade-label"),
             ("main", " df-meta-line shrink-0"), ("desk", " df-meta-line shrink-0")]
    assert parse_row_parts(older) == ("", "desk", "")
    assert parse_row_parts([]) == ("", "", "")

    # The info line: folder from the session file, time, Claude's summary.
    assert duration(20_000) == "1분 미만"
    assert duration(7 * 60_000) == "7분"
    assert duration(125 * 60_000) == "2시간 5분"
    assert duration(120 * 60_000) == "2시간"
    assert duration(3 * 24 * 3600_000) == "3일"
    now = 10_000_000_000
    meta = {"cwd": "E:\\agent\\desk", "lastTurnEdgeAt": now - 3 * 60_000,
            "lastActivityAt": now - 30 * 60_000}
    assert describe("실행 중", "x", "위젯 고치는 중", meta, now) \
        == "desk · 3분째 · 위젯 고치는 중"
    assert describe("입력 대기 중", "x", "", meta, now) == "desk · 30분째 대기"
    assert describe("읽지 않은 응답", "x", "", meta, now) == "desk · 30분 전"
    assert describe("실행 중", "chiiwork", "요약", None, now) == "chiiwork · 요약"
    assert describe("실행 중", "", "", None, now) == ""
    # Which statuses are listed, and as what; running and idle are not.
    unread = [("PdfConvert2 드림라보 출력 오류 원인 분석", " dframe-fade-label"),
              ("읽지 않은 응답", " dframe-fade-label sr-only"),
              ("PdfConvert2_4.6.1", " df-meta-slots df-meta-place")]
    assert parse_row_parts(unread) == ("읽지 않은 응답", "PdfConvert2_4.6.1", "")
    assert STATUS_KINDS["읽지 않은 응답"] == "ready"
    assert STATUS_KINDS["입력 대기 중"] == STATUS_KINDS["답변 대기 중"] == "awaiting"
    assert "실행 중" not in STATUS_KINDS and "유휴" not in STATUS_KINDS
    assert set(STATUS_KINDS.values()) == set(KIND_ORDER) == set(KIND_COLORS)

    # Registry commands: only the same kind of launcher may repoint an entry.
    exe_command = r'"C:\Programs\ClaudeWidget.exe"'
    script_command = r'"C:\py\pythonw.exe" "E:\w\claude_widget.py"'
    assert command_target(exe_command) == Path(r"C:\Programs\ClaudeWidget.exe")
    assert command_target(script_command) == Path(r"E:\w\claude_widget.py")
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
    assert command.rstrip('"').lower().endswith((".exe", "claude_widget.py")), command

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
    saved_state_file, saved_old_state_file = STATE_FILE, OLD_STATE_FILE
    scratch = tempfile.TemporaryDirectory()
    globals()["STATE_FILE"] = Path(scratch.name) / "state.json"
    globals()["OLD_STATE_FILE"] = Path(scratch.name) / "old.json"
    # Settings saved under the old file name carry over.
    OLD_STATE_FILE.write_text(json.dumps({"size": "작게"}), encoding="utf-8")
    assert load_state() == {"size": "작게"}
    STATE_FILE.write_text(json.dumps({"size": "크게"}), encoding="utf-8")
    assert load_state() == {"size": "크게"}
    STATE_FILE.unlink()
    OLD_STATE_FILE.unlink()

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
    widget.app_found = False
    widget.render()
    assert widget.main.cget("text") == "알 수 없음"
    widget.app_found = True
    widget.render()
    assert widget.main.cget("text") == "알 수 없음 · 세션 패널"

    # Waiting sessions appear as yellow rows and go away with the last one.
    widget.waiting = [
        ("PhotobookServer 리뷰", "photobook-server-workspace", "awaiting", None),
        ("기존 API 조사", "", "ready", None)]
    widget.show_waiting()
    assert [(row.title.cget("text"), row.folder.cget("text").strip(),
             row.close.cget("text")) for row in widget.waiting_rows] \
        == [("● PhotobookServer 리뷰", "photobook-server-workspace", "✕"),
            ("● 기존 API 조사", "", "✕")]
    # A row without a folder shows the title line alone.
    assert widget.waiting_rows[0].folder.winfo_manager() == "grid"
    assert widget.waiting_rows[1].folder.winfo_manager() == ""
    # Each kind in the app's own colours: waiting yellow, unread blue.
    assert widget.waiting_rows[0].title.cget("bg") == "#fab219"
    assert widget.waiting_rows[0].title.cget("fg") == "#0b0b0b"
    assert widget.waiting_rows[1].title.cget("bg") == "#2a78d6"
    assert widget.waiting_rows[1].title.cget("fg") == "#ffffff"
    assert widget.frame.cget("highlightbackground") == "#fab219"
    assert widget.waiting_box.winfo_manager() == "pack"
    for row in widget.waiting_rows:
        for part in (row, *row.winfo_children()):
            assert not part.bind("<Button-1>"), part
    # A theme switch repaints the rows; a size switch refonts them.
    for name in THEMES:
        widget.set_theme(name)
        assert widget.waiting_rows[0].cget("bg") == "#fab219"
    for name in SIZES:
        widget.set_size(name)
    # ✕ hides that row only.
    widget.dismiss("PhotobookServer 리뷰", "awaiting")
    assert [row.title.cget("text") for row in widget.waiting_rows] \
        == ["● 기존 API 조사"]
    assert widget.dismissed == {("PhotobookServer 리뷰", "awaiting")}
    # It stays hidden while that session keeps waiting, and comes back on
    # its next wait once it has stopped in between.
    saved_read = read_active_session
    try:
        for reported, shown in (
                (["PhotobookServer 리뷰", "기존 API 조사"], ["기존 API 조사"]),
                (["기존 API 조사"], ["기존 API 조사"]),
                (["PhotobookServer 리뷰"], ["PhotobookServer 리뷰"])):
            globals()["read_active_session"] = lambda r=reported: (
                0, (None, None, [(title, "", "awaiting", None) for title in r],
                    []))
            widget.refresh()
            assert [entry[0] for entry in widget.waiting] == shown, reported
    finally:
        globals()["read_active_session"] = saved_read
    widget.waiting = []
    widget.show_waiting()
    assert widget.waiting_rows == []
    assert widget.waiting_box.winfo_manager() == ""

    # Other sessions: one folded line with counts; a click opens the list.
    assert other_summary(["유휴", "실행 중", "유휴", "완료됨"]) \
        == "실행 중 1 · 유휴 2 · 완료됨 1"
    assert other_rank("실행 중") < other_rank("완료됨") < other_rank("새 상태")
    widget.others_open = False
    widget.others = [("BookPrintAPI", "BookPrintAPI", "실행 중", None),
                     ("데스크", "desk", "유휴", None)]
    widget.show_others()
    assert widget.others_box.winfo_manager() == "pack"
    assert widget.others_toggle.cget("text").startswith("▸ 다른 세션 2")
    assert widget.others_rows == []
    widget.on_click(widget.others_toggle)
    assert widget.others_open
    assert widget.others_toggle.cget("text").startswith("▾ 다른 세션 2")
    assert [(row.title.cget("text"), row.status.cget("text"))
            for row in widget.others_rows] \
        == [("BookPrintAPI", "실행 중"), ("데스크", "유휴")]
    for row in widget.others_rows:
        for part in (row, *row.winfo_children()):
            assert not part.bind("<Button-1>"), part
    widget.on_click(widget.others_toggle)
    assert not widget.others_open and widget.others_rows == []
    # A long list stops at OTHERS_MAX and says how many more there are.
    widget.others = [(f"세션 {n}", "", "실행 중", None)
                     for n in range(OTHERS_MAX + 5)]
    widget.on_click(widget.others_toggle)
    assert len(widget.others_rows) == OTHERS_MAX + 1
    assert widget.others_rows[-1].cget("text").startswith("… 외 5개")
    widget.on_click(widget.others_rows[-1])  # clicking it does nothing
    widget.on_click(widget.others_toggle)
    widget.others = [("BookPrintAPI", "BookPrintAPI", "실행 중", None),
                     ("데스크", "desk", "유휴", None)]
    widget.others = []
    widget.show_others()
    assert widget.others_box.winfo_manager() == ""

    # Width: the default per size until the edge is dragged; text is cut to
    # whatever width there is.
    widget.set_size("보통")
    widget.width = None
    assert widget.window_width() == DEFAULT_WIDTHS["보통"]
    font = tkfont.Font(font=(BODY_FONT, 8))
    assert fit("짧음", font, 500) == "짧음"
    cut_text = fit("아주 긴 세션 제목 " * 10, font, 120)
    assert cut_text.endswith("…") and font.measure(cut_text) <= 120
    assert fit("abc", font, 0) == ""
    widget.width = 500
    widget.waiting = [("아주 긴 세션 제목 " * 10, "", "awaiting", None)]
    widget.relayout()
    wide = widget.waiting_rows[0].title.cget("text")
    widget.width = MIN_WIDTH
    widget.relayout()
    narrow = widget.waiting_rows[0].title.cget("text")
    assert len(narrow) < len(wide), (narrow, wide)
    widget.width = None
    widget.waiting = []
    widget.relayout()

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
    globals()["OLD_STATE_FILE"] = saved_old_state_file
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
