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
READ_POLL_MS = 40  # how often the Tk side picks up the reader's results

# Malgun Gothic carries Hangul itself; Segoe UI did not, so Windows drew the
# Korean in a substitute font that clashed with the Latin at small sizes. The
# English family name resolves on every Windows locale.
BODY_FONT = "Malgun Gothic"

# "hero" is the context percentage, the one figure read at a glance.
SIZES = {
    "작게": {"main": 10, "hero": 13, "sub": 8, "bar": 170, "bar_height": 4,
           "pad": 7},
    "보통": {"main": 12, "hero": 16, "sub": 9, "bar": 210, "bar_height": 5,
           "pad": 10},
    "크게": {"main": 15, "hero": 20, "sub": 11, "bar": 265, "bar_height": 7,
           "pad": 13},
}

# Spacing steps, in pixels: every gap and inner padding is one of these.
SPACE = {"xs": 2, "sm": 4, "md": 8, "lg": 12, "xl": 16}
ROW_PAD = SPACE["md"]   # a list row's text inset, left and right

# Motion: durations (ms) and curves by role. Only state changes move -
# the widget sits beside the user's work and refreshes every two seconds.
MOTION = {"fast": 120, "base": 200, "slow": 300}
FRAME_MS = 16


def ease_out(t: float) -> float:
    """Decelerate: things arriving (a row appearing, the bar settling)."""
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    """Accelerate then decelerate: something moving within the window."""
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def blend(start: str, end: str, t: float) -> str:
    """The colour *t* of the way from *start* to *end* ("#rrggbb")."""
    a = [int(start[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(end[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))
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

# The tabs under the header, in order.
TABS = (("attention", "확인 필요"), ("running", "실행 중"),
        ("favorites", "즐겨찾기"), ("recent", "최근"))
TAB_KEYS = tuple(key for key, _label in TABS)
DEFAULT_TAB = "attention"
# The list scrolls once it would take more than this share of the screen.
LIST_MAX_SHARE = 0.6
# One wheel notch moves the list this many 24px steps.
SCROLL_STEPS = 3
# Opening a session in a collapsed group: how many groups to unfold at most,
# and how long to let the sidebar draw after each.
REVEAL_MAX = 12
REVEAL_WAIT_S = 0.25
EXPAND_ALL_PASSES = 4
# Unfolding the sidebar by itself: whenever the app is not the window in
# front (the user is elsewhere, so nothing jumps under their pointer), and
# when the pointer comes onto the widget, at most this often.
HOVER_EXPAND_S = 10

# 즐겨찾기 dividers: a name and one of these colours (the app's design
# tokens, the 400 step), or none for the theme's own grey.
DIVIDER_COLORS = (
    ("red", "빨강", "#e34948"), ("orange", "주황", "#d95926"),
    ("yellow", "노랑", "#eda100"), ("green", "초록", "#0ca30c"),
    ("blue", "파랑", "#3987e5"), ("violet", "보라", "#8173e3"),
    ("magenta", "분홍", "#d55181"))
DIVIDER_HEX = {key: hex_ for key, _name, hex_ in DIVIDER_COLORS}

# 확인 필요 sessions that left the list stay below it for an hour, so a
# mistaken click or a ✕ can be undone and a look put off can be had later.
RECHECK_MS = 60 * 60 * 1000
STRIPE_PX = 3      # the colour band at the left of a session under a divider
DRAG_START_PX = 5  # how far a press moves before it becomes a drag

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
# The open session's page is a document named "<session title> - Claude Code".
DOCUMENT_SUFFIX = " - Claude Code"

# A sidebar row is a button followed by its "<title>에 대한 더 많은 옵션"
# button, which gives the bare title. The row's status ("실행 중", "입력 대기
# 중", "읽지 않은 응답") is a text inside its hidden tooltip (class "sr-only");
# the row's own name leaves it out, and the summary line under the title is
# free text that can say "입력 대기 중" without the session waiting at all.
OPTIONS_SUFFIX = "에 대한 더 많은 옵션"
# Sidebar buttons that fold things but are not group headers.
NESTED_PREFIX = "중첩된 세션"            # a fork family's toggle
NESTED_SHOW = "중첩된 세션 표시 "          # …while folded
NESTED_HIDE = "중첩된 세션 숨기기 "        # …while open
SORT_BUTTON = "그룹화 및 정렬"
# A collapsed group header says this after its name while a session in it
# waits for input. It is the only status the sidebar gives for folded rows.
GROUP_WAITING_SUFFIX = " 이 그룹에 입력을 기다리는 세션이 있습니다"
# Group headers (and fork toggles) carry this class; the sidebar's menu
# buttons (템플릿으로 새로 만들기, the account menu) also expand but do not.
HEADER_CLASS = "hide-focus-ring"
# The sidebar itself: the group of this name whose class says so.
SIDEBAR_NAME = "사이드바"
SIDEBAR_CLASS = "dframe-sidebar"
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
UIA_DOCUMENT_CONTROL_TYPE = 50030
UIA_INVOKE_PATTERN = 10000
UIA_EXPAND_COLLAPSE_PATTERN = 10005
UIA_IS_EXPAND_COLLAPSE_AVAILABLE = 30028
UIA_EXPAND_STATE = 30070  # 0 collapsed, 1 expanded
TREE_SCOPE_DESCENDANTS = 4

_uia_local = threading.local()

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


def session_meta() -> tuple[dict[str, dict], dict[str, dict]]:
    """The app's session files (by id, by title), re-reading changed files.

    Titles can repeat (forks keep a name); the live, most recently active one
    wins, since that is the one the sidebar shows. A deleted session leaves
    its file behind next to a "deleted_<uuid>" marker; those are skipped.
    """
    by_id: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    seen = set()
    deleted = {"local_" + os.path.basename(path)[len("deleted_"):]
               for path in glob.glob(os.path.join(
                   os.path.dirname(os.path.dirname(SESSIONS_GLOB)), "*",
                   "deleted_*"))}
    for path in glob.glob(SESSIONS_GLOB):
        seen.add(path)
        try:
            mtime = os.path.getmtime(path)
            cached = _meta_cache.get(path)
            if not cached or cached[0] != mtime:
                with open(path, encoding="utf-8") as handle:
                    data = json.load(handle)
                cached = (mtime, {key: data.get(key) for key in (
                    "sessionId", "title", "cwd", "isArchived",
                    "lastActivityAt", "lastTurnEdgeAt")})
                _meta_cache[path] = cached
        except (OSError, ValueError):
            continue  # being rewritten right now; next read gets it
        meta = cached[1]
        sid, title = meta.get("sessionId"), meta.get("title")
        if not sid or not title or sid in deleted:
            continue
        by_id[sid] = meta
        rank = (not meta.get("isArchived"), meta.get("lastActivityAt") or 0)
        best = by_title.get(title)
        if best is None or rank > best["_rank"]:
            by_title[title] = {**meta, "_rank": rank}
    for path in set(_meta_cache) - seen:
        del _meta_cache[path]
    return by_id, by_title


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
                parts.append("방금" if took == "1분 미만" else f"{took} 전")
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
    """This thread's UI Automation client; COM objects stay on their thread."""
    client = getattr(_uia_local, "client", None)
    if client is None:
        import comtypes.client

        module = comtypes.client.GetModule("UIAutomationCore.dll")
        client = comtypes.client.CreateObject(
            module.CUIAutomation, interface=module.IUIAutomation)
        _uia_local.client = client
    return client


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


def group_rows(rows: list[tuple[int, str]], headers: dict[int, str]
               ) -> list[tuple[int, str, str]]:
    """(index, title, group) for each row: the last group header above it."""
    marks = sorted([(index, "header", name) for index, name in headers.items()]
                   + [(index, "row", title) for index, title in rows])
    group = ""
    found = []
    for index, kind, name in marks:
        if kind == "header":
            group = group_name(name)
        else:
            found.append((index, name, group))
    return found


def is_group_header(element, name: str) -> bool:
    """A sidebar group header: foldable, header-styled, not a fork toggle."""
    return is_group_candidate(name) \
        and HEADER_CLASS in (element.CurrentClassName or "") \
        and bool(element.GetCurrentPropertyValue(
            UIA_IS_EXPAND_COLLAPSE_AVAILABLE))


def group_name(header: str) -> str:
    """A group header's name without the app's 'something waits' note."""
    return header[:-len(GROUP_WAITING_SUFFIX)] \
        if header.endswith(GROUP_WAITING_SUFFIX) else header


def is_group_candidate(name: str) -> bool:
    """A button that may be a sidebar group header, before asking UIA."""
    return bool(name) and not name.endswith(OPTIONS_SUFFIX) \
        and not name.startswith(NESTED_PREFIX) and name != SORT_BUTTON


def sidebar_buttons(client, root):
    """The sidebar's buttons and their names; (None, []) with no sidebar.

    Searching only the sidebar keeps the chat's own expandable buttons
    (command logs, tool calls) out of the group headers.
    """
    try:
        named = root.FindAll(TREE_SCOPE_DESCENDANTS, client.CreatePropertyCondition(
            UIA_NAME_PROPERTY, SIDEBAR_NAME))
        for index in range(named.Length):
            element = named.GetElement(index)
            if SIDEBAR_CLASS in (element.CurrentClassName or ""):
                buttons = element.FindAll(
                    TREE_SCOPE_DESCENDANTS, client.CreatePropertyCondition(
                        UIA_CONTROL_TYPE_PROPERTY, UIA_BUTTON_CONTROL_TYPE))
                return buttons, [buttons.GetElement(i).CurrentName or ""
                                 for i in range(buttons.Length)]
    except Exception:
        pass
    return None, []


def read_window(hwnd: int):
    """(context, plan, rows, groups) read off one Claude window.

    Rows are the sidebar's session rows in order, each a dict: title, status,
    folder, summary, group (the header it sits under) and element (what a
    click invokes). Groups are the headers: name, collapsed, waiting (a
    session inside waits for input) and element.
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

    side_buttons, side_names = sidebar_buttons(client, root)
    found = sidebar_rows(side_names)
    row_indexes = {index for index, _title in found}
    headers: dict[int, str] = {}
    groups = []
    for index, name in enumerate(side_names):
        if name.startswith(NESTED_SHOW):
            # A folded fork family: no status for its rows either.
            groups.append({"name": name, "collapsed": True, "waiting": False,
                           "element": None, "nested": True})
            continue
        if index in row_indexes or not is_group_candidate(name):
            continue
        try:
            element = side_buttons.GetElement(index)
            if is_group_header(element, name):
                headers[index] = name
                groups.append({
                    "name": group_name(name),
                    "collapsed": element.GetCurrentPropertyValue(
                        UIA_EXPAND_STATE) == 0,
                    "waiting": name.endswith(GROUP_WAITING_SUFFIX),
                    "element": element})
        except Exception:
            continue
    rows = []
    for index, title, group in group_rows(found, headers):
        element = side_buttons.GetElement(index)
        status, folder, summary = parse_row_parts(row_parts(element))
        rows.append({"title": title, "status": status, "folder": folder,
                     "summary": summary, "group": group, "element": element})
    if context is None and plan is None and not rows:
        return None
    return context, plan, rows, groups


def recent_bucket(age_ms: float) -> str:
    """The 최근 tab's divider for a session last active *age_ms* ago."""
    hours = age_ms / 3_600_000
    if hours < 24:
        return "24시간 이내"
    days = int(hours // 24)
    return f"{days}일 전" if days < 7 else "1주 이상"


def hidden_session(sid: str, meta: dict | None, group: str | None,
                   now_ms: float, title: str = "") -> dict:
    """A session the sidebar does not show right now (collapsed, archived…)."""
    if not meta:
        return {"key": sid, "sid": sid, "title": title or sid, "status": "",
                "kind": None, "info": "찾을 수 없음", "element": None,
                "group": "", "visible": False, "last": 0, "gone": True}
    if meta.get("isArchived"):
        return {"key": sid, "sid": sid, "title": meta.get("title") or title,
                "status": "", "kind": None, "info": "보관됨", "element": None,
                "group": "", "visible": False,
                "last": meta.get("lastActivityAt") or 0, "gone": True}
    where = f"{group} (접힘)" if group else "접힌 그룹 안"
    info = describe("", "", "", meta, now_ms)
    return {"key": sid, "sid": sid, "title": meta.get("title") or title,
            "status": "", "kind": None,
            "info": f"{where} · {info}" if info else where, "element": None,
            "group": group or "", "visible": False,
            "last": meta.get("lastActivityAt") or 0, "gone": False}


def build_sessions(rows: list[dict], by_id: dict, by_title: dict,
                   group_cache: dict, now_ms: float,
                   groups: list[dict] = ()) -> list[dict]:
    """Every session the widget knows: the sidebar's rows, then the ones it
    has seen before that now sit in a collapsed group.

    A collapsed group that says a session in it waits becomes one waiting
    entry of its own (key "group:<name>"): which session it is, the sidebar
    does not say until the group is open. Opening that entry opens the group.
    """
    sessions, seen = [], set()
    for group in groups:
        if group["collapsed"] and group["waiting"]:
            sessions.append({
                "key": f"group:{group['name']}", "sid": None,
                "title": group["name"], "status": "입력 대기 중",
                "kind": "awaiting", "info": "접힌 그룹 안 · 누르면 그룹을 펼침",
                "element": group["element"], "group": group["name"],
                "visible": True, "last": 0, "gone": False,
                "group_header": True})
    for row in rows:
        meta = by_title.get(row["title"])
        sid = meta.get("sessionId") if meta else None
        key = sid or f"title:{row['title']}"
        if key in seen:
            continue
        seen.add(key)
        sessions.append({
            "key": key, "sid": sid, "title": row["title"],
            "status": row["status"], "kind": STATUS_KINDS.get(row["status"]),
            "info": describe(row["status"], row["folder"], row["summary"],
                             meta, now_ms),
            "element": row["element"], "group": row["group"], "visible": True,
            "last": (meta or {}).get("lastActivityAt") or 0, "gone": False})
    for sid, group in group_cache.items():
        meta = by_id.get(sid)
        if sid in seen or not meta or meta.get("isArchived"):
            continue
        seen.add(sid)
        sessions.append(hidden_session(sid, meta, group, now_ms))
    return sessions


def attention_of(sessions: list[dict], dismissed: set) -> list[dict]:
    """확인 필요: waiting first, then unread, minus the rows closed with ✕."""
    found = [s for s in sessions
             if s["kind"] and (s["key"], s["kind"]) not in dismissed]
    return sorted(found, key=lambda s: KIND_ORDER.index(s["kind"]))


def running_of(sessions: list[dict]) -> list[dict]:
    found = [s for s in sessions if s["status"] and not s["kind"]]
    return sorted(found, key=lambda s: other_rank(s["status"]))


def auto_expand_due(enabled: bool, groups: list[dict], foreground: int,
                    app_hwnd: int) -> bool:
    """Unfold on its own: allowed, something folded, the app not in front."""
    return enabled and bool(app_hwnd) and foreground != app_hwnd \
        and any(group["collapsed"] for group in groups)


def move_entry(entries: list, source: int, target: int) -> list:
    """*entries* with the one at *source* moved to before *target*.

    *target* counts positions in the list as it is now, so len() means the
    end; dropping an entry just above or below itself changes nothing.
    """
    moved = list(entries)
    entry = moved.pop(source)
    if target > source:
        target -= 1
    moved.insert(target, entry)
    return moved


def valid_favorite(entry) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
        return False
    if entry.get("type") == "session":
        return True
    return entry.get("type") == "divider" and isinstance(entry.get("name"), str) \
        and (entry.get("color") is None or entry.get("color") in DIVIDER_HEX)


def departures(before: dict, now_keys: set, dismissed: set) -> list[dict]:
    """Sessions that left 확인 필요 since the last read.

    *before* maps key -> session as listed last time. A ✕ is noted when it
    happens, not here; folded-group stand-ins are not sessions.
    """
    return [session for key, session in before.items()
            if key not in now_keys
            and (key, session["kind"]) not in dismissed
            and not key.startswith("group:")]


def held_waiting(held: list[dict], active_keys: set) -> list[dict]:
    """보류 entries not already back on the list by themselves."""
    return [entry for entry in held if entry["key"] not in active_keys]


def tab_items(tab: str, sessions: list[dict], favorites: list[dict],
              dismissed: set, by_id: dict, group_cache: dict,
              now_ms: float, held: list[dict] = (),
              checked: list[dict] = ()) -> list[tuple]:
    """What a tab lists: ("session", s), ("divider", text) or ("empty", text)."""
    if tab == "attention":
        active = attention_of(sessions, dismissed)
        items = [("session", s) for s in active]
        active_keys = {s["key"] for s in active}
        live = {s["key"]: s for s in sessions}

        def copy_of(entry: dict) -> dict | None:
            base = live.get(entry["key"])
            if base is None and entry.get("sid"):
                base = hidden_session(entry["sid"], by_id.get(entry["sid"]),
                                      group_cache.get(entry["sid"]), now_ms,
                                      entry.get("title", ""))
            # A plain row (kind None), but the status keeps its colour: a
            # ✕-hidden session may still be waiting.
            return dict(base, origin=entry["key"], kind=None,
                        status_kind=base["kind"]) if base else None

        # 보류: kept on the list by the user, whatever the app says.
        for entry in held_waiting(held, active_keys):
            session = copy_of(entry)
            if session:
                session.update(key=f"held:{entry['key']}", status="보류",
                               tone="soft", held=True, status_kind=None)
                items.append(("session", session))
        # 방금 확인함: what left in the last hour, newest first.
        held_keys = {entry["key"] for entry in held}
        recent = [entry for entry in checked
                  if now_ms - entry["at"] < RECHECK_MS
                  and entry["key"] not in active_keys
                  and entry["key"] not in held_keys]
        if recent:
            items.append(("divider", "방금 확인함"))
        for entry in recent:
            session = copy_of(entry)
            if not session:
                continue
            took = duration(now_ms - entry["at"])
            when = "방금" if took == "1분 미만" else f"{took} 전"
            # On the right, what the session is doing now (실행 중 and the
            # like), as in 즐겨찾기; nothing when it is idle.
            session.update(key=f"checked:{entry['key']}", checked=True,
                           info=f"{when} · {session['info']}"
                           if session["info"] else when)
            items.append(("session", session))
        return items or [("empty", "확인할 세션이 없음")]
    if tab == "running":
        items = [("session", s) for s in running_of(sessions)]
        return items or [("empty", "실행 중인 세션이 없음")]
    if tab == "favorites":
        by_sid = {s["sid"]: s for s in sessions if s["sid"]}
        items = []
        for entry in favorites:
            if entry["type"] == "divider":
                items.append(("mark", entry))
                continue
            sid = entry["id"]
            session = by_sid.get(sid) or hidden_session(
                sid, by_id.get(sid), group_cache.get(sid), now_ms,
                entry.get("title", ""))
            items.append(("session", session))
        return items or [("empty", "세션에서 오른쪽 버튼 → 즐겨찾기에 추가")]
    ended = sorted((s for s in sessions if not s["status"] and s["last"]),
                   key=lambda s: s["last"], reverse=True)
    items, bucket = [], None
    for session in ended:
        label = recent_bucket(now_ms - session["last"])
        if label != bucket:
            items.append(("divider", label))
            bucket = label
        items.append(("session", session))
    return items or [("empty", "끝난 세션이 없음")]


def other_rank(status: str) -> int:
    """Sort key for the other statuses; unknown ones go last."""
    return OTHER_ORDER.index(status) if status in OTHER_ORDER \
        else len(OTHER_ORDER)


def fit(text: str, font: tkfont.Font, width: int) -> str:
    """*text* cut with … so it measures at most *width* pixels."""
    if width <= 0:
        return ""
    if font.measure(text) <= width:
        return text
    # The longest prefix that fits with its "…": a binary search, since each
    # measure is a call into Tk and titles run to a hundred characters.
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(text[:middle] + "…") <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…" if low else ""


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
    bring_forward(hwnd)


def bring_forward(hwnd: int) -> None:
    if not hwnd:
        return
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)


def expand_collapse(element, expand: bool) -> bool:
    try:
        import comtypes.client

        module = comtypes.client.GetModule("UIAutomationCore.dll")
        pattern = element.GetCurrentPattern(UIA_EXPAND_COLLAPSE_PATTERN) \
            .QueryInterface(module.IUIAutomationExpandCollapsePattern)
        pattern.Expand() if expand else pattern.Collapse()
        return True
    except Exception:
        return False


def reveal_and_open(hwnd: int, title: str, group_hint: str) -> bool:
    """Open a session whose row a collapsed group (or fork family) hides.

    Expands collapsed groups one at a time, the remembered group first, then
    collapsed fork families, until the row shows; opens it; folds back
    everything it unfolded so the sidebar looks as it did.
    ponytail: tries one expander per pass with a short wait; enough for a few
    dozen groups. Ask the app for the session directly if it ever offers a way.
    """
    client = uia_client()
    opened_names: list[str] = []   # names to fold back, in unfold order
    tried: set[str] = set()
    found = False

    def scan():
        return sidebar_buttons(client, client.ElementFromHandle(hwnd))

    try:
        for _attempt in range(REVEAL_MAX):
            buttons, names = scan()
            if buttons is None:
                break
            rows = sidebar_rows(names)
            for index, row_title in rows:
                if row_title == title:
                    open_session(hwnd, buttons.GetElement(index))
                    found = True
                    break
            if found:
                break
            collapsed = []
            for index, name in enumerate(names):
                if name in tried:
                    continue
                element = buttons.GetElement(index)
                if name.startswith(NESTED_SHOW):
                    collapsed.append((2, name, element))
                elif is_group_header(element, name) and \
                        element.GetCurrentPropertyValue(UIA_EXPAND_STATE) == 0:
                    collapsed.append((0 if group_name(name) == group_hint
                                      else 1, name, element))
            if not collapsed:
                break
            _rank, name, element = min(collapsed, key=lambda c: c[0])
            tried.add(name)
            if expand_collapse(element, True):
                opened_names.append(name)
                time.sleep(REVEAL_WAIT_S)
    finally:
        # Fold back what was unfolded, last first. Names change once open: a
        # fork family's toggle says 숨기기 instead of 표시, and a group header
        # drops its "something waits" note.
        for name in reversed(opened_names):
            shown = NESTED_HIDE + name[len(NESTED_SHOW):] \
                if name.startswith(NESTED_SHOW) else group_name(name)
            buttons, names = scan()
            if buttons is None:
                break
            for index, current in enumerate(names):
                if group_name(current) == shown:
                    expand_collapse(buttons.GetElement(index), False)
                    break
    return found


def expand_all(hwnd: int) -> int:
    """Unfold every collapsed sidebar group and fork family; how many.

    Folded rows give the widget no status, so a tab click unfolds them and
    leaves them open. A group may hold folded fork families, hence the passes.
    """
    client = uia_client()
    opened = 0
    for _pass in range(EXPAND_ALL_PASSES):
        buttons, names = sidebar_buttons(client, client.ElementFromHandle(hwnd))
        if buttons is None:
            break
        targets = []
        for index, name in enumerate(names):
            try:
                element = buttons.GetElement(index)
                if name.startswith(NESTED_SHOW) or (
                        is_group_header(element, name) and
                        element.GetCurrentPropertyValue(UIA_EXPAND_STATE) == 0):
                    targets.append(element)
            except Exception:
                continue
        if not targets:
            break
        for element in targets:
            if expand_collapse(element, True):
                opened += 1
        time.sleep(REVEAL_WAIT_S)
    return opened


def current_title(names: list[str]) -> str | None:
    """The open session's title, out of the window's document names."""
    for name in names:
        if name.endswith(DOCUMENT_SUFFIX) and len(name) > len(DOCUMENT_SUFFIX):
            return name[:-len(DOCUMENT_SUFFIX)]
    return None


def document_names(hwnd: int) -> list[str]:
    """Names of the window's documents (the app shell, the open session…)."""
    try:
        client = uia_client()
        found = client.ElementFromHandle(hwnd).FindAll(
            TREE_SCOPE_DESCENDANTS, client.CreatePropertyCondition(
                UIA_CONTROL_TYPE_PROPERTY, UIA_DOCUMENT_CONTROL_TYPE))
        return [found.GetElement(i).CurrentName or ""
                for i in range(found.Length)]
    except Exception:  # COM is allowed to fail; the menu then says so
        return []


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


def detach_elements(rows: list[dict], groups: list[dict]) -> dict:
    """Swap the UIA elements in a read for plain handles; handle -> element.

    The elements stay on the reader thread that made them, so the Tk side
    only ever holds strings. A title shared by two rows keeps the first,
    the one the widget shows.
    """
    elements: dict[str, object] = {}
    for kind, entries, name_key in (("row", rows, "title"),
                                    ("group", groups, "name")):
        for entry in entries:
            element, entry["element"] = entry.get("element"), None
            if element is None:
                continue
            handle = f"{kind}:{entry[name_key]}"
            elements.setdefault(handle, element)
            entry["element"] = handle
    return elements


class Reader:
    """Reads and drives the app on a thread of its own.

    A read takes 0.3-0.7 s of UI Automation calls, and unfolding the sidebar
    waits for it to redraw; on the Tk thread either froze the window. Jobs
    queue in, results queue out for the Tk side to drain. COM is initialised
    on this thread and the UIA elements never leave it (see detach_elements).
    """

    def __init__(self) -> None:
        self.jobs: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue()
        self.elements: dict[str, object] = {}   # reader thread only
        self.thread = threading.Thread(target=self.run, name="reader",
                                       daemon=True)
        self.thread.start()

    # ---- Tk thread -----------------------------------------------------
    def submit(self, job: str, **args) -> None:
        self.jobs.put((job, args))

    def drain(self) -> list[dict]:
        found = []
        while True:
            try:
                found.append(self.results.get_nowait())
            except queue.Empty:
                return found

    def stop(self) -> None:
        self.jobs.put(("stop", {}))
        self.thread.join(timeout=2)

    # ---- reader thread -------------------------------------------------
    def run(self) -> None:
        import comtypes

        comtypes.CoInitialize()
        try:
            while True:
                job, args = self.jobs.get()
                if job == "stop":
                    break
                try:
                    read = getattr(self, f"do_{job}")(**args)
                except Exception:
                    report_exception(*sys.exc_info())
                    read = None
                self.results.put({"job": job, "read": read})
        finally:
            self.elements.clear()
            comtypes.CoUninitialize()

    def read(self) -> dict:
        active = read_active_session()
        by_id, by_title = session_meta() if active else ({}, {})
        current = None
        if active:
            hwnd, (_context, _plan, rows, groups) = active
            self.elements = detach_elements(rows, groups)
            current = current_title(document_names(hwnd))
        return {"active": active, "by_id": by_id, "by_title": by_title,
                "current": current}

    def do_read(self, auto_expand: bool = False) -> dict:
        """One read; with *auto_expand*, unfold and read again when due."""
        found = self.read()
        if auto_expand and found["active"]:
            hwnd, (_context, _plan, _rows, groups) = found["active"]
            if auto_expand_due(True, groups,
                               int(_user32.GetForegroundWindow() or 0), hwnd) \
                    and expand_all(hwnd):
                found = self.read()
        return found

    def do_unfold(self, hwnd: int) -> dict | None:
        """Unfold the sidebar; a fresh read if anything opened."""
        return self.read() if hwnd and expand_all(hwnd) else None

    def do_open(self, hwnd: int, session: dict) -> None:
        element = self.elements.get(session["element"] or "")
        if session.get("group_header"):
            # Unfold the group in the app and leave it open: the waiting
            # session shows there, and on the next read here as itself.
            if element is not None:
                expand_collapse(element, True)
            bring_forward(hwnd)
        elif session["visible"]:
            if element is not None:   # gone since the last read: nothing to do
                open_session(hwnd, element)
        elif session["sid"] and hwnd:
            reveal_and_open(hwnd, session["title"], session["group"])


class Widget:
    def __init__(self, root: tk.Tk, reader: Reader | None = None) -> None:
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
        self.tab = state.get("tab") if state.get("tab") in TAB_KEYS \
            else DEFAULT_TAB
        # Sessions saved by id (a renamed session stays in the list) and the
        # user's dividers, in the order shown.
        self.favorites: list[dict] = [
            entry for entry in state.get("favorites") or []
            if valid_favorite(entry)]
        self.auto_expand = state.get("auto_expand", True) is not False
        # 보류: sessions the user asked to see again later, kept across runs.
        self.held: list[dict] = [
            {"key": entry["key"], "sid": entry.get("sid"),
             "title": entry.get("title", "")}
            for entry in state.get("held") or []
            if isinstance(entry, dict) and isinstance(entry.get("key"), str)]
        # 방금 확인함: what left 확인 필요 lately (this run only).
        self.checked: list[dict] = []
        # session id -> the sidebar group it was last seen under.
        groups = state.get("groups")
        self.group_cache: dict[str, str] = {
            str(sid): str(name) for sid, name in groups.items()} \
            if isinstance(groups, dict) else {}

        root.title("Claude 위젯")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.shown_geometry = state.get("geometry") or "+40+40"
        root.geometry(self.shown_geometry)

        self.frame = tk.Frame(root, highlightthickness=1)
        self.frame.pack(fill="both", expand=True)

        # The header: context and plan. The only place that moves the window
        # and opens the widget menu; the tabs and the list below keep the
        # mouse for themselves.
        self.header = tk.Frame(self.frame)
        self.header.pack(fill="x")
        # The window's width is set outright (place() gives it), never taken
        # from the text: a window that resizes as the numbers change slips out
        # from under the pointer mid-drag. Long text is cut to fit instead.
        # The percentage large on the left, the token figures quiet beside it.
        self.figures = tk.Frame(self.header)
        self.figures.pack(fill="x")
        self.figures.columnconfigure(1, weight=1)
        self.main = tk.Label(self.figures, text="…", anchor="w", padx=0, pady=0)
        self.main.grid(row=0, column=0, sticky="sw")
        self.tokens_label = tk.Label(self.figures, anchor="e", width=1,
                                     padx=0)
        self.tokens_label.grid(row=0, column=1, sticky="sew",
                               pady=(0, SPACE["xs"]))
        self.bar = tk.Canvas(self.header, highlightthickness=0, width=1)
        self.bar.pack(fill="x")
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, 0, width=0)
        self.bar_width = 0.0   # the fill's drawn width, while it glides
        self.sub = tk.Label(self.header, text="세션 찾는 중", width=1, anchor="w")
        self.sub.pack(fill="x")

        # Tabs: a label and a 2px underline each. Each takes its text's width
        # and an even share of what is left, so 확인 필요 3 fits next to 최근.
        self.tabbar = tk.Frame(self.frame)
        self.tabbar.pack(fill="x", pady=(SPACE["md"], 0))
        self.tab_parts: dict[str, tuple] = {}
        for column, (key, _label) in enumerate(TABS):
            cell = tk.Frame(self.tabbar, cursor="hand2")
            cell.grid(row=0, column=column, sticky="we")
            self.tabbar.columnconfigure(column, weight=1)
            label = tk.Label(cell, cursor="hand2", pady=SPACE["sm"],
                             padx=SPACE["xs"])
            label.pack(fill="x")
            line = tk.Frame(cell, height=2)
            line.pack(fill="x")
            for part in (cell, label, line):
                part.tab = key
            self.tab_parts[key] = (cell, label, line)
        # One underline for the open tab, gliding from tab to tab over the
        # tabs' own 2px lines.
        self.tab_indicator = tk.Frame(self.tabbar, height=2, cursor="hand2")
        self.indicator_at: tuple[float, float] | None = None

        # The list scrolls inside a canvas once it is taller than its share
        # of the screen.
        self.canvas = tk.Canvas(self.frame, highlightthickness=0, bd=0,
                                yscrollincrement=24, width=1, height=1)
        self.canvas.pack(fill="x", pady=(SPACE["sm"], 0))
        # One list per tab, each in its own canvas window; switching tabs
        # shows another window instead of repacking rows.
        self.listboxes: dict[str, tk.Frame] = {}
        self.list_windows: dict[str, int] = {}
        for key in TAB_KEYS:
            self.listboxes[key] = tk.Frame(self.canvas)
            self.list_windows[key] = self.canvas.create_window(
                0, 0, anchor="nw", window=self.listboxes[key], state="hidden")
        self.shown_list: str | None = None
        self.rows: list[tk.Widget] = []
        self.list_signature = None
        # What the rows were built for; rows are reused while it holds.
        self.list_layout = None
        self.row_pools: dict[str, list] = {}   # tab -> its rows, kept
        self.font_cache: dict[str, tkfont.Font] = {}
        # Dragging a 즐겨찾기 row: its index, and the line showing where it lands.
        self.drag_index: int | None = None
        self.dragging = False
        self.drop_index = 0
        self.drop_line = tk.Frame(self.listboxes["favorites"], height=2)

        # What the last read found.
        self.tokens: int | None = None
        self.max_now = 0
        self.detail = "세션 찾는 중"
        self.app_hwnd = 0
        self.app_found = False
        self.sessions: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.by_key: dict[str, dict] = {}   # what the shown rows stand for
        # (session key, kind) of the rows the user closed with ✕.
        self.dismissed: set[tuple[str, str]] = set()
        self.groups: list[dict] = []
        # The session open in the app: {"sid", "title"}, or None.
        self.current: dict | None = None
        self.last_hover_expand = 0.0
        self.animations: dict = {}   # key -> the running animation's token
        self.apply_size()
        self.apply_theme()

        self.menu = tk.Menu(root, tearoff=0)
        self.item_menu = tk.Menu(root, tearoff=0)
        self.build_menu()

        # Binding the toplevel is enough: every child's bindtags include it, so
        # presses on the labels and the bar all arrive here. Binding the
        # children as well would run each handler twice.
        root.bind("<Button-1>", self.on_press)
        root.bind("<B1-Motion>", self.on_drag)
        root.bind("<ButtonRelease-1>", self.on_release)
        root.bind("<Button-3>", self.on_right_click)
        root.bind("<Motion>", self.on_motion)
        root.bind("<MouseWheel>", self.on_wheel)
        root.bind("<Enter>", self.on_enter)
        root.bind("<Escape>", lambda _event: self.quit())

        self.press_pointer = (0, 0)
        self.press_window = (0, 0)
        self.resizing = False
        self.moving = False
        self.press_width = 0
        # Jobs handed to the reader and not answered yet; one of each at most.
        self.pending: set[str] = set()
        self.reader = reader or Reader()
        self.poll_reader()
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

    def popup_menu(self, x: int, y: int, menu: tk.Menu | None = None) -> None:
        menu = menu or self.menu
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # ---- window chrome -------------------------------------------------
    @property
    def listbox(self) -> tk.Frame:
        """The open tab's list."""
        return self.listboxes[self.tab]

    @property
    def list_window(self) -> int:
        return self.list_windows[self.tab]

    @property
    def colors(self) -> dict[str, str]:
        return THEMES[self.theme]

    def apply_theme(self) -> None:
        colors = self.colors
        self.root.configure(bg=colors["bg"])
        self.frame.configure(bg=colors["bg"],
                             highlightbackground=self.border_color())
        for part in (self.header, self.figures, self.tabbar, self.canvas,
                     *self.listboxes.values()):
            part.configure(bg=colors["bg"])
        self.main.configure(bg=colors["bg"], fg=colors["fg"])
        self.tokens_label.configure(bg=colors["bg"], fg=colors["dim"])
        self.sub.configure(bg=colors["bg"], fg=colors["fg"])
        self.bar.configure(bg=colors["track"])
        self.render_tabs()
        self.list_signature = None
        self.render_list()

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
                "hero": ("Consolas", spec["hero"], "bold"),
                "figure": ("Consolas", spec["sub"]),
                "sub": (BODY_FONT, spec["sub"]),
                "title": (BODY_FONT, spec["sub"], "bold"),
                "small": (BODY_FONT, max(7, spec["sub"] - 1))}

    def font(self, name: str) -> tkfont.Font:
        """A measuring font for fit(), made once per size."""
        if name not in self.font_cache:
            self.font_cache[name] = tkfont.Font(font=self.fonts()[name])
        return self.font_cache[name]

    def window_width(self) -> int:
        return self.width or DEFAULT_WIDTHS[self.size]

    def text_width(self, inset: int = 0) -> int:
        """Pixels a full-width line of text gets, less *inset*."""
        pad = SIZES[self.size]["pad"]
        return self.window_width() - 2 * pad - 2 - inset

    def apply_size(self) -> None:
        spec = SIZES[self.size]
        fonts = self.fonts()
        self.font_cache = {}
        self.frame.configure(padx=spec["pad"], pady=max(4, spec["pad"] - 3))
        self.main.configure(font=fonts["hero"])
        self.tokens_label.configure(font=fonts["figure"])
        self.sub.configure(font=fonts["sub"])
        self.bar.configure(height=spec["bar_height"])
        self.bar_width = -1.0   # redraw at the new height
        self.bar.pack_configure(pady=(spec["pad"] // 2, spec["pad"] // 2 + 1))
        self.relayout()

    def relayout(self) -> None:
        """Rebuild what depends on the font or width, then resize."""
        self.list_signature = None
        self.render_tabs()
        self.render_list()
        self.render()
        self.place()
        self.root.after_idle(self.move_indicator, False)

    def build_menu(self, current: bool = False) -> None:
        """The widget menu; with *current*, the open session's favourite
        item on top (the header's menu, not the tray's)."""
        self.menu.delete(0, "end")
        if current:
            session = self.current
            if session is None:
                self.menu.add_command(label="현재 세션을 찾을 수 없음",
                                      state="disabled")
            elif self.is_favorite(session["sid"]):
                self.menu.add_command(
                    label=f"즐겨찾기에서 빼기 — {session['title']}",
                    command=lambda: self.toggle_favorite(session))
            else:
                self.menu.add_command(
                    label=f"★ 즐겨찾기에 추가 — {session['title']}",
                    command=lambda: self.toggle_favorite(session))
            self.menu.add_separator()
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

    # ---- pointer -------------------------------------------------------
    def in_header(self, widget) -> bool:
        return widget in (self.header, self.figures, self.main,
                          self.tokens_label, self.bar, self.sub)

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
        self.moving = not self.resizing and self.in_header(event.widget)
        self.press_width = self.root.winfo_width()
        row = self.row_of(event.widget) if self.tab == "favorites" else None
        self.drag_index = getattr(row, "fav_index", None)
        self.dragging = False
        self.log(f"press {self.press_pointer} window {self.press_window}")

    def on_drag(self, event: tk.Event) -> None:
        if self.resizing:
            width = self.press_width + event.x_root - self.press_pointer[0]
            width = min(MAX_WIDTH, max(MIN_WIDTH, width))
            if width != self.window_width():
                self.width = width
                self.relayout()
            return
        if self.drag_index is not None:
            if not self.dragging and abs(event.y_root - self.press_pointer[1]) \
                    < DRAG_START_PX:
                return
            self.dragging = True
            self.show_drop(event.y_root)
            return
        if not self.moving:
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
        if self.dragging:
            self.dragging = False
            self.drop_line.place_forget()
            source, self.drag_index = self.drag_index, None
            self.favorites = move_entry(self.favorites, source, self.drop_index)
            self.persist()
            self.render_list()
            return
        self.drag_index = None
        moved = max(abs(event.x_root - self.press_pointer[0]),
                    abs(event.y_root - self.press_pointer[1])) > 3
        if self.moving:
            self.moving = False
            if moved and not self.hidden:
                self.shown_geometry = self.current_geometry()
                self.persist()
            return
        if not moved:
            # A click. Tk delivers the release to the widget that got the
            # press, so this is what the click started on.
            self.on_click(event.widget)

    def on_wheel(self, event: tk.Event) -> None:
        if not str(event.widget).startswith(str(self.canvas)):
            return
        if self.listbox.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(
                -SCROLL_STEPS if event.delta > 0 else SCROLL_STEPS, "units")

    def row_of(self, widget):
        """The list row a widget belongs to, or None."""
        while widget is not None and widget is not self.listbox:
            if hasattr(widget, "key"):
                return widget
            widget = getattr(widget, "master", None)
        return None

    def on_click(self, widget) -> None:
        tab = getattr(widget, "tab", None)
        if tab:
            # Any tab click, the open one too, unfolds the sidebar and reads
            # again: folded rows hide their status from the widget. The tab
            # shows at once; the unfolding happens on the reader.
            self.show_tab(tab)
            self.unfold_and_update()
            return
        row = self.row_of(widget)
        session = self.by_key.get(row.key) if row is not None else None
        if not session:
            return
        if widget is getattr(row, "close", None):
            self.dismiss(session["key"], session["kind"])
            return
        self.open(session)

    def on_enter(self, _event: tk.Event) -> None:
        """The pointer came onto the widget: a good moment to unfold."""
        if not self.auto_expand or time.time() - self.last_hover_expand \
                < HOVER_EXPAND_S or not any(g["collapsed"] for g in self.groups):
            return
        self.last_hover_expand = time.time()
        self.unfold_and_update()

    def in_list(self, widget) -> bool:
        return str(widget).startswith(str(self.canvas))

    def on_right_click(self, event: tk.Event) -> None:
        if self.in_header(event.widget):
            self.build_menu(current=True)
            self.popup_menu(event.x_root, event.y_root)
            return
        if not self.in_list(event.widget):
            return
        row = self.row_of(event.widget)
        menu = self.item_menu
        menu.delete(0, "end")
        favorites_tab = self.tab == "favorites"
        mark = getattr(row, "mark", None)
        if mark is not None:
            menu.add_command(label="구분선 편집",
                             command=lambda: DividerDialog(self, mark))
            menu.add_command(label="구분선 삭제",
                             command=lambda: self.delete_divider(mark))
            self.popup_menu(event.x_root, event.y_root, menu)
            return
        session = self.by_key.get(row.key) if row is not None else None
        if session and self.tab == "attention" \
                and not session.get("group_header"):
            origin = session.get("origin", session["key"])
            if session.get("held"):
                menu.add_command(label="보류 해제",
                                 command=lambda: self.release(origin))
            else:
                menu.add_command(label="나중에 다시 확인",
                                 command=lambda: self.hold(session))
            if session.get("checked"):
                menu.add_command(label="방금 확인함에서 빼기",
                                 command=lambda: self.forget_checked(origin))
            menu.add_separator()
        if session and not session.get("group_header"):
            if not session["sid"]:
                menu.add_command(label="즐겨찾기 불가 (세션 파일 없음)",
                                 state="disabled")
            elif self.is_favorite(session["sid"]):
                menu.add_command(label="즐겨찾기에서 빼기",
                                 command=lambda: self.toggle_favorite(session))
            else:
                menu.add_command(label="즐겨찾기에 추가",
                                 command=lambda: self.toggle_favorite(session))
        if favorites_tab:
            # Above the row pressed on, or at the end on empty space.
            at = getattr(row, "fav_index", len(self.favorites))
            if menu.index("end") is not None:
                menu.add_separator()
            menu.add_command(label="구분선 추가",
                             command=lambda: DividerDialog(self, None, at))
        if menu.index("end") is not None:
            self.popup_menu(event.x_root, event.y_root, menu)

    def show_drop(self, y_root: int) -> None:
        """Where a dragged 즐겨찾기 row would land, as a line between rows."""
        y = y_root - self.listbox.winfo_rooty()
        placed = [row for row in self.rows if hasattr(row, "fav_index")]
        self.drop_index, line_y = len(self.favorites), 0
        for row in placed:
            if y < row.winfo_y() + row.winfo_height() / 2:
                self.drop_index, line_y = row.fav_index, row.winfo_y() - 2
                break
            # Inside the last row's bottom edge: the list ends right there,
            # so a line below it would be cut off.
            line_y = row.winfo_y() + row.winfo_height() - 2
        self.drop_line.configure(bg=self.colors["fg"])
        self.drop_line.place(x=0, y=max(0, line_y), relwidth=1)
        self.drop_line.lift()

    def current_geometry(self) -> str:
        return f"+{self.root.winfo_x()}+{self.root.winfo_y()}"

    def persist(self) -> None:
        save_state({
            "size": self.size,
            "theme": self.theme,
            "thresholds": list(self.thresholds),
            # Where the user put it, not where place() nudged it to make room
            # for the list.
            "geometry": self.shown_geometry,
            "width": self.width,
            "tab": self.tab,
            "favorites": self.favorites,
            "groups": self.group_cache,
            "auto_expand": self.auto_expand,
            "held": self.held,
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
        wanted = f"{width}x{height}+{x}+{y}"
        # place() runs on every list change; most leave the size as it was.
        if self.root.winfo_geometry() != wanted:
            self.root.geometry(wanted)

    def quit(self) -> None:
        self.persist()
        if self.tray:
            self.tray.close()
        self.reader.stop()
        self.root.destroy()

    # ---- motion ----------------------------------------------------------
    def animate(self, key, duration: int, ease, step) -> None:
        """Call step(eased 0..1) every frame for *duration* ms.

        A new animation under the same key takes over from the old one. With
        the window hidden or not yet shown it jumps to the end. A step whose
        widget has gone ends the animation.
        """
        token = object()
        self.animations[key] = token
        if self.hidden or not self.root.winfo_viewable():
            self.animations.pop(key)
            step(1.0)
            return
        start = time.perf_counter()

        def frame() -> None:
            if self.animations.get(key) is not token:
                return
            t = min(1.0, (time.perf_counter() - start) * 1000 / duration)
            try:
                step(ease(t))
            except tk.TclError:
                t = 1.0
            if t < 1.0:
                self.root.after(FRAME_MS, frame)
            elif self.animations.get(key) is token:
                del self.animations[key]

        frame()

    def move_indicator(self, glide: bool = True) -> None:
        """Put the underline under the open tab, gliding there if *glide*."""
        cell = self.tab_parts[self.tab][0]
        x, width = cell.winfo_x(), cell.winfo_width()
        if width <= 1:   # not laid out yet
            self.root.after(50, self.move_indicator, False)
            return
        self.tab_indicator.tab = self.tab
        self.tab_indicator.lift()
        start = self.indicator_at if glide and self.indicator_at else (x, width)

        def step(t: float) -> None:
            at = (start[0] + (x - start[0]) * t,
                  start[1] + (width - start[1]) * t)
            self.indicator_at = at
            self.tab_indicator.place(x=round(at[0]), rely=1.0, y=-2,
                                     width=round(at[1]), height=2)
        self.animate("indicator", MOTION["base"], ease_in_out, step)

    # ---- tabs and list ---------------------------------------------------
    def attention(self) -> list[dict]:
        return attention_of(self.sessions, self.dismissed)

    def border_color(self) -> str:
        """The first waiting kind's colour while anything needs a look."""
        attention = self.attention() if hasattr(self, "sessions") else []
        if attention:
            return KIND_COLORS[attention[0]["kind"]]["bg"]
        return self.colors["border"]

    def unfold_and_update(self) -> None:
        """Have the reader unfold the sidebar and, if it did, read again."""
        if self.app_hwnd:
            self.submit("unfold", hwnd=self.app_hwnd)

    def submit(self, job: str, **args) -> None:
        """Hand *job* to the reader unless the same job is still pending."""
        if job in self.pending:
            return
        self.pending.add(job)
        self.reader.submit(job, **args)

    def poll_reader(self) -> None:
        for result in self.reader.drain():
            self.pending.discard(result["job"])
            if result["read"] is not None:
                self.apply_read(result["read"])
        self.root.after(READ_POLL_MS, self.poll_reader)

    def show_tab(self, tab: str) -> None:
        if tab not in TAB_KEYS or tab == self.tab:
            return
        self.tab = tab
        self.canvas.yview_moveto(0)
        self.render_tabs()
        self.move_indicator()
        self.render_list()
        self.persist()

    def render_tabs(self) -> None:
        colors = self.colors
        fonts = self.fonts()
        attention = self.attention()
        # 보류 rows count, but do not colour the tab: that stays for what the
        # app says needs a look.
        counts = {"attention": len(attention) + len(held_waiting(
                      self.held, {s["key"] for s in attention})),
                  "running": len(running_of(self.sessions))}
        for key, label_text in TABS:
            cell, label, line = self.tab_parts[key]
            text = label_text + (f" {counts[key]}" if counts.get(key) else "")
            active = key == self.tab
            font = fonts["title"] if active else fonts["sub"]
            bg, fg = colors["bg"], colors["fg"] if active else colors["soft"]
            underline = colors["fg"] if active else colors["bg"]
            if key == "attention" and attention:
                # Something to look at: the tab wears the first kind's colour
                # wherever the user is, so it is never missed.
                palette = KIND_COLORS[attention[0]["kind"]]
                bg, fg, underline = palette["bg"], palette["fg"], palette["bg"]
                if active:
                    underline = palette["fg"]
            if active:
                # The gliding indicator draws the open tab's underline.
                self.tab_indicator.configure(bg=underline)
                underline = bg
            # Every read lands here; reconfiguring an unchanged tab redraws it.
            look = (text, font, bg, fg, underline)
            if getattr(label, "look", None) == look:
                continue
            label.look = look
            label.configure(text=text, font=font, bg=bg, fg=fg)
            cell.configure(bg=bg)
            line.configure(bg=underline)
        self.tabbar.configure(bg=colors["bg"])
        border = self.border_color()
        if self.frame.cget("highlightbackground") != border:
            self.frame.configure(highlightbackground=border)

    def clock(self) -> float:
        """Now, in ms; the self-check swaps it for a fixed time."""
        return time.time() * 1000

    def current_items(self) -> list[tuple]:
        return tab_items(self.tab, self.sessions, self.favorites,
                         self.dismissed, self.by_id, self.group_cache,
                         self.clock(), self.held, self.checked)

    def render_list(self) -> None:
        """Show the tab's rows - only when what they show has changed.

        Rows are kept and their texts updated in place; a row is made anew
        only when its look changes (status, colour, a band) or it is new.
        Rebuilding every row made the list flash each time a minute ticked.
        """
        items = self.current_items()
        self.by_key = {item[1]["key"]: item[1] for item in items
                       if item[0] == "session"}
        signature = (self.tab, self.theme, self.size, self.window_width(),
                     tuple((kind, item["key"], item["title"], item["info"],
                            item["status"], item["visible"], item["gone"],
                            self.is_favorite(item["sid"]))
                           if kind == "session" else
                           (kind, tuple(sorted(item.items())))
                           if kind == "mark" else (kind, item)
                           for kind, item in items))
        if signature == self.list_signature:
            return
        self.list_signature = signature
        top = self.canvas.yview()[0]
        layout = (self.theme, self.size, self.window_width())
        if layout != self.list_layout:
            # Fonts, colours or widths changed: nothing can be kept.
            for pool in self.row_pools.values():
                for row in pool:
                    row.destroy()
            self.row_pools = {}
            self.rows = []
            self.list_layout = layout
        # Each tab keeps its rows in its own list while another is shown, so
        # going back to a tab redraws what changed instead of every row.
        pool = self.row_pools.get(self.tab, [])
        if self.shown_list != self.tab:
            if self.shown_list:
                self.canvas.itemconfigure(self.list_windows[self.shown_list],
                                          state="hidden")
            self.canvas.itemconfigure(self.list_window, state="normal")
            self.shown_list = self.tab
        self.rows = pool
        spare: dict = {}
        for row in pool:
            spare.setdefault(row.shape, row)
        rows = []
        band = None   # the colour of the 즐겨찾기 divider above, if any
        for index, (kind, item) in enumerate(items):
            if kind == "mark":
                band = DIVIDER_HEX.get(item.get("color"))
            shape = self.row_shape(kind, item, band)
            row = spare.pop(shape, None)
            if row is None:
                row = self.make_row(kind, item, band)
                row.shape = shape
                # Only a newly flagged row rises in; 보류 and 방금 확인함 are
                # plain rows and just appear.
                if pool and self.tab == "attention" and kind == "session" \
                        and item["kind"]:
                    self.fade_row(row, self.paint_attention,
                                  start=self.colors["bg"],
                                  duration=MOTION["slow"])
            elif kind == "session":
                self.fill_row(row, item)
            elif kind == "mark":
                row.mark = item
            if self.tab == "favorites" and kind in ("session", "mark"):
                row.fav_index = index
            rows.append(row)
        for row in spare.values():
            row.destroy()
        if rows != self.rows:
            # Repack only when the order or the set of rows changed.
            for row in rows:
                row.pack_forget()
            for row in rows:
                row.pack(**row.pack_options)
        self.rows = self.row_pools[self.tab] = rows
        self.fit_list()
        self.canvas.yview_moveto(top)
        self.place()

    def row_shape(self, kind: str, item, band: str | None) -> tuple:
        """What a row looks like apart from its texts; equal shapes reuse."""
        if kind == "session":
            star = self.tab != "favorites" and self.is_favorite(item["sid"])
            return (kind, item["key"], item["kind"], item["status"],
                    item["gone"], bool(item["info"]), band, star)
        if kind == "mark":
            return (kind, item["id"], item["name"], item.get("color"))
        return (kind, item)

    def make_row(self, kind: str, item, band: str | None) -> tk.Widget:
        if kind == "session":
            row = self.attention_row(item) \
                if self.tab == "attention" and item["kind"] \
                else self.plain_row(item, band)
        elif kind == "mark":
            row = self.mark_row(item)
        elif kind == "divider":
            row = self.divider_row(item)
        else:
            row = self.empty_row(item)
        return row

    def fill_row(self, row: tk.Frame, session: dict) -> None:
        """Put *session*'s texts into a row made for the same shape."""
        row.session = session
        star = self.tab != "favorites" and self.is_favorite(session["sid"])
        texts = (session["title"], session["info"], star)
        if getattr(row, "texts", None) == texts:
            return   # fitting is the costly part: skip it when nothing changed
        row.texts = texts
        if self.tab == "attention" and session["kind"]:
            room = self.text_width(inset=2 * ROW_PAD + row.close_width)
            title = fit(f"● {session['title']}", self.font("title"), room)
            info = fit(f"   {session['info']}", self.font("small"), room)
        else:
            title = fit(("★ " if star else "") + session["title"],
                        self.font("title"),
                        self.text_width(inset=2 * ROW_PAD + row.status_width))
            info = fit(session["info"], self.font("sub"),
                       self.text_width(inset=2 * ROW_PAD))
        for label, text in ((row.title, title), (row.info, info)):
            if label.cget("text") != text:
                label.configure(text=text)

    def fit_list(self) -> None:
        """Size the canvas to the list, up to its share of the screen."""
        self.listbox.update_idletasks()
        width = self.text_width()
        height = self.listbox.winfo_reqheight()
        left, top, right, bottom = work_area(self.root.winfo_id())
        cap = max(120, int((bottom - top) * LIST_MAX_SHARE))
        wanted = (self.tab, width, min(height, cap), height)
        if wanted == getattr(self, "list_size", None):
            return
        self.list_size = wanted
        self.canvas.itemconfigure(self.list_window, width=width)
        self.canvas.configure(width=width, height=min(height, cap),
                              scrollregion=(0, 0, width, height))

    def attention_row(self, session: dict) -> tk.Frame:
        fonts = self.fonts()
        row = tk.Frame(self.listbox, padx=ROW_PAD, pady=SPACE["sm"],
                       cursor="hand2")
        row.key = session["key"]
        row.kind = session["kind"]
        row.close_width = self.font("title").measure("✕") + 2 * SPACE["sm"]
        row.columnconfigure(0, weight=1)
        row.title = tk.Label(row, anchor="w", width=1, cursor="hand2",
                             font=fonts["title"])
        row.title.grid(row=0, column=0, sticky="we")
        row.close = tk.Label(row, text="✕", cursor="hand2", padx=SPACE["sm"],
                             font=fonts["title"])
        row.close.grid(row=0, column=1, rowspan=2, sticky="ne")
        row.info = tk.Label(row, anchor="w", width=1, cursor="hand2",
                            font=fonts["small"])
        if session["info"]:
            row.info.grid(row=1, column=0, sticky="we")
        row.pack_options = {"fill": "x", "pady": (SPACE["sm"], 0)}
        self.fill_row(row, session)
        self.paint_attention(row)
        self.bind_hover(row, self.paint_attention)
        return row

    def plain_row(self, session: dict, band: str | None = None) -> tk.Frame:
        fonts = self.fonts()
        status = session["status"]
        if session["kind"] or session.get("status_kind"):
            status = f"● {status}"
        row = tk.Frame(self.listbox, pady=SPACE["sm"], cursor="hand2")
        row.key = session["key"]
        if session.get("held"):
            # 보류: outlined, so it reads as kept rather than flagged.
            row.configure(highlightthickness=1,
                          highlightbackground=self.colors["soft"])
        row.status_width = self.font("title").measure(status) + ROW_PAD \
            if status else 0
        row.columnconfigure(1, weight=1)
        # A divider's colour shows as a thin band down the left edge.
        if band:
            tk.Frame(row, width=STRIPE_PX, bg=band).grid(
                row=0, column=0, rowspan=2, sticky="ns")
        row.title = tk.Label(row, anchor="w", width=1, cursor="hand2",
                             padx=ROW_PAD, font=fonts["title"])
        row.title.grid(row=0, column=1, sticky="we")
        row.status = tk.Label(row, text=status, cursor="hand2", padx=ROW_PAD,
                              font=fonts["title"])
        row.status.grid(row=0, column=2, sticky="e")
        row.info = tk.Label(row, anchor="w", width=1, cursor="hand2",
                            padx=ROW_PAD, font=fonts["sub"])
        if session["info"]:
            row.info.grid(row=1, column=1, columnspan=2, sticky="we")
        row.pack_options = {"fill": "x", "pady": (SPACE["sm"], 0)}
        self.fill_row(row, session)
        self.paint_plain(row)
        self.bind_hover(row, self.paint_plain)
        return row

    def divider_row(self, text: str) -> tk.Frame:
        colors = self.colors
        row = tk.Frame(self.listbox, bg=colors["bg"])
        row.columnconfigure(1, weight=1)
        tk.Label(row, text=text, font=self.fonts()["small"], bg=colors["bg"],
                 fg=colors["soft"]).grid(row=0, column=0, sticky="w")
        tk.Frame(row, height=1, bg=colors["track"]).grid(
            row=0, column=1, sticky="we", padx=(SPACE["md"], 0))
        row.pack_options = {"fill": "x", "pady": (SPACE["md"], SPACE["xs"])}
        return row

    def mark_row(self, entry: dict) -> tk.Frame:
        """A 즐겨찾기 divider: its name, then a line, in its colour."""
        colors = self.colors
        color = DIVIDER_HEX.get(entry.get("color"))
        row = tk.Frame(self.listbox, bg=colors["bg"], cursor="fleur")
        row.key = f"divider:{entry['id']}"
        row.mark = entry
        row.columnconfigure(1, weight=1)
        name = entry["name"] or "구분선"
        tk.Label(row, text=fit(name, self.font("title"),
                               self.text_width() // 2),
                 font=self.fonts()["title"], bg=colors["bg"],
                 fg=color or colors["soft"], cursor="fleur").grid(
            row=0, column=0, sticky="w")
        tk.Frame(row, height=2 if color else 1, bg=color or colors["track"],
                 cursor="fleur").grid(row=0, column=1, sticky="we",
                                      padx=(SPACE["md"], 0))
        row.pack_options = {"fill": "x", "pady": (SPACE["lg"], SPACE["xs"])}
        return row

    def empty_row(self, text: str) -> tk.Label:
        label = tk.Label(self.listbox, text=text, anchor="w", width=1,
                         font=self.fonts()["sub"], bg=self.colors["bg"],
                         fg=self.colors["dim"], pady=SPACE["md"])
        label.pack_options = {"fill": "x"}
        return label

    def paint_attention(self, row: tk.Frame, hover: bool = False,
                        bg: str | None = None) -> None:
        palette = KIND_COLORS[row.kind]
        bg = bg or palette["hover" if hover else "bg"]
        row.configure(bg=bg)
        for label in (row.title, row.close):
            label.configure(bg=bg, fg=palette["fg"])
        row.info.configure(bg=bg, fg=palette["dim"])

    def paint_plain(self, row: tk.Frame, hover: bool = False,
                    bg: str | None = None) -> None:
        colors = self.colors
        session = row.session
        bg = bg or colors["track" if hover else "field"]
        row.configure(bg=bg)
        row.title.configure(bg=bg, fg=colors["dim" if session["gone"]
                                             else "fg"])
        # (the colour band, a plain Frame child, keeps its own colour)
        if session["kind"] or session.get("status_kind"):
            status_fg = KIND_COLORS[session["kind"]
                                    or session["status_kind"]]["bg"]
        elif session.get("tone"):
            status_fg = colors[session["tone"]]
        else:
            status_fg = colors["green"]
        row.status.configure(bg=bg, fg=status_fg)
        row.info.configure(bg=bg, fg=colors["dim" if session["gone"]
                                            or session.get("checked")
                                            else "soft"])

    def bind_hover(self, row: tk.Frame, paint) -> None:
        # Enter/Leave reach the toplevel's own handler (on_enter) as well,
        # which only looks at the time. Each part repaints the whole row:
        # moving onto a label sends the frame a Leave first, and the fade
        # that starts then is taken over by the Enter right after it.
        for part in (row, *row.winfo_children()):
            part.bind("<Enter>", lambda _e, r=row: self.fade_row(r, paint, True))
            part.bind("<Leave>", lambda _e, r=row: self.fade_row(r, paint))

    def fade_row(self, row: tk.Frame, paint, hover: bool = False,
                 start: str | None = None, duration: int = MOTION["fast"]
                 ) -> None:
        """Ease a row's background from where it is (or *start*) to its
        resting or hover colour."""
        start = start or row.cget("bg")   # mid-fade, the colour it got to
        paint(row, hover)
        end = row.cget("bg")

        def step(t: float) -> None:
            paint(row, hover, blend(start, end, t))
        self.animate(("row", str(row)), duration, ease_out, step)

    # ---- actions -----------------------------------------------------------
    def open(self, session: dict) -> None:
        """Open the session in the app; the reader does the clicking."""
        if session["gone"]:
            return
        self.submit("open", hwnd=self.app_hwnd, session=dict(session))

    def is_favorite(self, sid: str | None) -> bool:
        return bool(sid) and any(entry["id"] == sid for entry in self.favorites)

    def toggle_favorite(self, session: dict) -> None:
        sid = session["sid"]
        if not sid:
            return
        if self.is_favorite(sid):
            self.favorites = [entry for entry in self.favorites
                              if entry["id"] != sid]
        else:
            self.favorites.append({"type": "session", "id": sid,
                                   "title": session["title"]})
        self.persist()
        self.render_list()

    def save_divider(self, entry: dict | None, name: str, color: str | None,
                     at: int | None = None) -> None:
        """Change a divider, or add a new one before position *at*."""
        if entry is None:
            entry = {"type": "divider", "id": f"d{int(time.time() * 1000)}",
                     "name": name, "color": color}
            at = len(self.favorites) if at is None else at
            self.favorites.insert(at, entry)
        else:
            entry["name"], entry["color"] = name, color
        self.persist()
        self.list_signature = None
        self.render_list()

    def delete_divider(self, entry: dict) -> None:
        self.favorites = [e for e in self.favorites if e is not entry]
        self.persist()
        self.render_list()

    def note_checked(self, session: dict) -> None:
        """Put a session that left 확인 필요 under 방금 확인함."""
        key = session.get("origin", session["key"])
        now = self.clock()
        self.checked = [entry for entry in self.checked
                        if entry["key"] != key and now - entry["at"] < RECHECK_MS]
        self.checked.insert(0, {"key": key, "sid": session["sid"],
                                "title": session["title"], "at": now})

    def hold(self, session: dict) -> None:
        """Keep a session on 확인 필요 until released (나중에 다시 확인)."""
        key = session.get("origin", session["key"])
        if not any(entry["key"] == key for entry in self.held):
            self.held.append({"key": key, "sid": session["sid"],
                              "title": session["title"]})
            self.persist()
        self.render_tabs()
        self.render_list()

    def release(self, key: str) -> None:
        self.held = [entry for entry in self.held if entry["key"] != key]
        self.persist()
        self.render_tabs()
        self.render_list()

    def forget_checked(self, key: str) -> None:
        """Take one row off 방금 확인함 before its half hour is up."""
        self.checked = [entry for entry in self.checked if entry["key"] != key]
        self.render_list()

    def dismiss(self, key: str, kind: str) -> None:
        """Hide one row until its session leaves that status and comes back."""
        session = self.by_key.get(key)
        if session:
            self.note_checked(session)
        self.dismissed.add((key, kind))
        self.render_tabs()
        self.render_list()
        if self.tray:
            self.tray.update_tip(self.tip_text())

    # ---- data ----------------------------------------------------------
    def refresh(self) -> None:
        self.submit("read", auto_expand=self.auto_expand)
        self.root.after(REFRESH_MS, self.refresh)

    def apply_read(self, read: dict) -> None:
        """Show what the reader found, redrawing only what changed."""
        active = read["active"]
        if active is None:
            self.tokens = None
            self.detail = "클로드 앱을 못 찾음"
            self.sessions = []
            self.groups = []
            self.app_found = False
            self.current = None
        else:
            self.app_found = True
            self.app_hwnd, (context, plan, rows, groups) = active
            self.groups = groups
            self.by_id, by_title = read["by_id"], read["by_title"]
            # The session open in the app, by the same rule as the sidebar:
            # of sessions sharing a title, the live, most recently active.
            meta = by_title.get(read.get("current") or "")
            self.current = {"sid": meta["sessionId"], "title": meta["title"]} \
                if meta else None
            self.remember_groups(rows, by_title)
            before = {s["key"]: s for s in self.attention()}
            self.sessions = build_sessions(rows, self.by_id, by_title,
                                           self.group_cache, time.time() * 1000,
                                           groups)
            now_keys = {s["key"] for s in attention_of(self.sessions,
                                                       self.dismissed)}
            for session in departures(before, now_keys, self.dismissed):
                self.note_checked(session)
            # Back on the list by itself: no longer "just checked".
            self.checked = [entry for entry in self.checked
                            if entry["key"] not in now_keys]
            # A hidden row stays hidden only while its session keeps that
            # status; once it changes, the next time shows it again.
            self.dismissed &= {(s["key"], s["kind"]) for s in self.sessions
                               if s["kind"]}
            self.tokens, self.max_now = context if context else (None, 0)
            self.detail = plan or "알 수 없음"
        self.render()
        self.render_tabs()
        self.render_list()
        if self.tray:
            self.tray.update_tip(self.tip_text())

    def remember_groups(self, rows: list[dict], by_title: dict) -> None:
        """Note each visible session's group, for when it gets folded away."""
        changed = False
        for row in rows:
            meta = by_title.get(row["title"])
            sid = meta.get("sessionId") if meta else None
            if sid and self.group_cache.get(sid) != row["group"]:
                self.group_cache[sid] = row["group"]
                changed = True
        if changed:
            self.persist()

    def percent(self) -> float | None:
        """Context fill, or None when there is nothing to show."""
        if self.tokens is None or self.max_now <= 0:
            return None
        return min(self.tokens / self.max_now, 1.0) * 100

    def tip_text(self) -> str:
        percent = self.percent()
        count = len(self.attention())
        waiting = f"\n확인 필요 {count}" if count else ""
        if percent is None:
            return f"{TRAY_TIP} · 알 수 없음{waiting}"
        return f"{TRAY_TIP} {percent:.1f}%\n{self.detail}{waiting}"

    def render(self) -> None:
        self.sub.configure(text=fit(self.detail, self.font("sub"),
                                    self.text_width()))
        percent = self.percent()
        if percent is None:
            # The figures now live in the session panel; with the panel shut
            # there is nothing to read, so say where they would come from.
            text = "알 수 없음 · 세션 패널" if self.app_found else "알 수 없음"
            self.set_label(self.main, text=text, fg=self.colors["dim"],
                           font=self.fonts()["title"])
            self.set_label(self.tokens_label, text="")
            self.glide_bar(0)
            return

        color = self.colors[band_for(percent, self.thresholds)]
        self.set_label(self.main, text=f"{percent:.1f}%", fg=color,
                       font=self.fonts()["hero"])
        self.set_label(self.tokens_label, text=fit(
            f"{format_tokens(self.tokens)} / {format_tokens(self.max_now)}",
            self.font("figure"), self.text_width() // 2))
        # Before the window is mapped winfo_width() is 1, not 0, so the bar
        # would be drawn one pixel wide on the very first paint.
        width = self.bar.winfo_width()
        if width <= 1:
            width = self.text_width()
        self.bar.itemconfigure(self.bar_fill, fill=color)
        self.glide_bar(width * percent / 100)

    @staticmethod
    def set_label(label: tk.Label, **options) -> None:
        """Configure only what differs from the last call: every read lands
        here, and reconfiguring a label redraws it."""
        last = label.__dict__.setdefault("last_options", {})
        changed = {key: value for key, value in options.items()
                   if last.get(key) != value}
        if changed:
            last.update(changed)
            label.configure(**changed)

    def glide_bar(self, target: float) -> None:
        """Ease the bar's fill to *target* pixels wide."""
        if round(target) == round(self.bar_width) and \
                "bar" not in self.animations:
            return
        height = SIZES[self.size]["bar_height"]
        start = self.bar_width

        def step(t: float) -> None:
            self.bar_width = start + (target - start) * t
            self.bar.coords(self.bar_fill, 0, 0, round(self.bar_width), height)
        self.animate("bar", MOTION["slow"], ease_out, step)


def dialog_button(parent, text: str, command, colors: dict,
                  primary: bool = False) -> tk.Button:
    """A flat button with hover and pressed states; *primary* is the one
    action the dialog is for, in the app's accent colour."""
    if primary:
        accent = KIND_COLORS["ready"]
        rest, hover, fg = accent["bg"], accent["hover"], accent["fg"]
    else:
        rest, hover, fg = colors["field"], colors["track"], colors["fg"]
    button = tk.Button(parent, text=text, command=command, relief="flat",
                       bd=0, bg=rest, fg=fg, activebackground=hover,
                       activeforeground=fg, cursor="hand2",
                       font=(BODY_FONT, 9), padx=SPACE["lg"], pady=SPACE["xs"])
    button.bind("<Enter>", lambda _e: button.configure(bg=hover))
    button.bind("<Leave>", lambda _e: button.configure(bg=rest))
    return button


class DividerDialog:
    """구분선 추가·편집: a name and a colour from DIVIDER_COLORS (or none)."""

    def __init__(self, widget: "Widget", entry: dict | None,
                 at: int | None = None) -> None:
        self.widget, self.entry, self.at = widget, entry, at
        colors = widget.colors
        top = tk.Toplevel(widget.root)
        self.top = top
        top.title("구분선 편집" if entry else "구분선 추가")
        top.configure(bg=colors["bg"], padx=SPACE["xl"], pady=SPACE["lg"])
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.minsize(260, 0)  # room for the title bar's own text

        tk.Label(top, text="이름", bg=colors["bg"], fg=colors["dim"],
                 font=(BODY_FONT, 9)).pack(anchor="w")
        self.name = tk.Entry(top, width=24, bg=colors["field"], fg=colors["fg"],
                             insertbackground=colors["fg"], relief="flat",
                             font=(BODY_FONT, 10))
        self.name.insert(0, entry["name"] if entry else "")
        self.name.pack(fill="x", pady=(SPACE["xs"], SPACE["lg"]),
                       ipady=SPACE["sm"])

        tk.Label(top, text="색", bg=colors["bg"], fg=colors["dim"],
                 font=(BODY_FONT, 9)).pack(anchor="w")
        self.color = entry.get("color") if entry else None
        swatches = tk.Frame(top, bg=colors["bg"])
        swatches.pack(fill="x", pady=(SPACE["xs"], SPACE["lg"]))
        self.swatches: dict = {}
        for key, label, hex_ in ((None, "없음", colors["track"]),
                                 *DIVIDER_COLORS):
            swatch = tk.Label(swatches, text="  ", bg=hex_, cursor="hand2",
                              width=2, relief="flat", bd=0,
                              highlightthickness=2)
            swatch.pack(side="left", padx=(0, SPACE["sm"]))
            swatch.bind("<Button-1>", lambda _e, k=key: self.pick(k))
            self.swatches[key] = swatch
        self.pick(self.color)

        buttons = tk.Frame(top, bg=colors["bg"])
        buttons.pack(fill="x")
        dialog_button(buttons, "취소", top.destroy, colors).pack(side="left")
        dialog_button(buttons, "확인", self.save, colors,
                      primary=True).pack(side="right")
        top.bind("<Return>", lambda _event: self.save())
        top.bind("<Escape>", lambda _event: top.destroy())
        self.name.focus_set()
        root = widget.root
        top.update_idletasks()
        x, y = clamp_to_area(root.winfo_x() + 20, root.winfo_y() + 60,
                             top.winfo_reqwidth() + 16, top.winfo_reqheight() + 40,
                             work_area(root.winfo_id()))
        top.geometry(f"+{x}+{y}")

    def pick(self, key: str | None) -> None:
        self.color = key
        colors = self.widget.colors
        for swatch_key, swatch in self.swatches.items():
            ring = colors["fg"] if swatch_key == key else colors["bg"]
            swatch.configure(highlightbackground=ring, highlightcolor=ring)

    def save(self) -> None:
        self.widget.save_divider(self.entry, self.name.get().strip(),
                                 self.color, self.at)
        self.top.destroy()


class SettingsDialog:
    """설정 - size, colour bands and autostart, applied on 적용."""

    def __init__(self, widget: Widget) -> None:
        self.widget = widget
        colors = widget.colors
        top = tk.Toplevel(widget.root)
        self.top = top
        top.title("설정")
        top.configure(bg=colors["bg"], padx=SPACE["xl"], pady=SPACE["lg"])
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
                                                            padx=(0, SPACE["md"]))

        caption("테마")
        self.theme = tk.StringVar(value=widget.theme)
        themes = tk.Frame(top, bg=colors["bg"])
        themes.pack(fill="x", pady=(SPACE["xs"], SPACE["lg"]))
        for name in THEMES:
            choice(themes, name, name, self.theme, self.apply_theme)

        caption("크기")
        self.size = tk.StringVar(value=widget.size)
        sizes = tk.Frame(top, bg=colors["bg"])
        sizes.pack(fill="x", pady=(SPACE["xs"], SPACE["lg"]))
        for name in SIZES:
            choice(sizes, name, name, self.size, self.apply_size)

        caption("색 구간")
        rows = (("이하 초록", "green"), ("이하 노랑", "yellow"),
                ("이하 주황", "orange"))
        self.entries: list[tk.Entry] = []
        for index, (label, band) in enumerate(rows):
            line = tk.Frame(top, bg=colors["bg"])
            line.pack(fill="x", pady=SPACE["sm"])
            tk.Label(line, text="■", bg=colors["bg"], fg=colors[band],
                     font=(BODY_FONT, 11)).pack(side="left")
            entry = tk.Entry(line, width=5, justify="right",
                             bg=colors["field"], fg=colors["fg"],
                             insertbackground=colors["fg"], relief="flat",
                             font=("Consolas", 11))
            entry.insert(0, str(widget.thresholds[index]))
            entry.pack(side="left", padx=SPACE["md"])
            tk.Label(line, text=f"% {label}", bg=colors["bg"], fg=colors["fg"],
                     font=(BODY_FONT, 9)).pack(side="left")
            self.entries.append(entry)

        tk.Label(top, text="그 이상은 빨강", bg=colors["bg"], fg=colors["red"],
                 font=(BODY_FONT, 9)).pack(anchor="w", pady=(SPACE["xs"], SPACE["lg"]))

        self.autostart = tk.BooleanVar(value=autostart_enabled())
        self.auto_expand = tk.BooleanVar(value=widget.auto_expand)
        for text, variable in (("윈도우 시작 시 자동 실행", self.autostart),
                               ("접힌 그룹 자동으로 펼치기", self.auto_expand)):
            tk.Checkbutton(top, text=text, variable=variable, bg=colors["bg"],
                           fg=colors["fg"], activebackground=colors["bg"],
                           activeforeground=colors["fg"],
                           selectcolor=colors["field"], highlightthickness=0,
                           bd=0, font=(BODY_FONT, 9),
                           anchor="w").pack(fill="x", pady=(0, SPACE["sm"]))
        tk.Label(top, text="앱이 앞 창이 아닐 때와 위젯에 마우스를 올릴 때 펼친다.\n"
                           "탭을 누르면 이 설정과 관계없이 펼친다.",
                 bg=colors["bg"], fg=colors["dim"], justify="left",
                 font=(BODY_FONT, 8)).pack(anchor="w", pady=(0, SPACE["lg"]))

        buttons = tk.Frame(top, bg=colors["bg"])
        buttons.pack(fill="x")
        dialog_button(buttons, "기본값", self.reset, colors).pack(side="left")
        dialog_button(buttons, "적용", self.apply, colors,
                      primary=True).pack(side="right")
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
        self.widget.auto_expand = bool(self.auto_expand.get())
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
    assert describe("", "", "", {"lastActivityAt": now - 10_000}, now) == "방금"
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

    # Sidebar groups: each row sits under the last header above it.
    assert group_rows([(2, "가"), (5, "나"), (9, "다")],
                      {1: "고정됨", 4: "🔄 우선작업"}) \
        == [(2, "가", "고정됨"), (5, "나", "🔄 우선작업"), (9, "다", "🔄 우선작업")]
    assert group_rows([(0, "가")], {}) == [(0, "가", "")]
    assert is_group_candidate("🔄 우선작업")
    assert not is_group_candidate("중첩된 세션 표시 PhotobookServer")
    assert not is_group_candidate("그룹화 및 정렬")
    assert not is_group_candidate("데스크에 대한 더 많은 옵션")
    # A folded group with a waiting session: the note is not part of the name.
    assert group_name("✔️ 워크스페이스 이 그룹에 입력을 기다리는 세션이 있습니다") \
        == "✔️ 워크스페이스"
    assert group_name("🔷 프로젝트") == "🔷 프로젝트"
    assert group_rows([(3, "가")],
                      {1: "⭕ 프로그램 이 그룹에 입력을 기다리는 세션이 있습니다"}) \
        == [(3, "가", "⭕ 프로그램")]

    # 최근 dividers: within 24 hours, then by whole days, then a week.
    hour = 3_600_000
    assert recent_bucket(0) == recent_bucket(23.9 * hour) == "24시간 이내"
    assert recent_bucket(24 * hour) == recent_bucket(47 * hour) == "1일 전"
    assert recent_bucket(6.9 * 24 * hour) == "6일 전"
    assert recent_bucket(7 * 24 * hour) == "1주 이상"

    # Sessions: sidebar rows by session id, then remembered ones now folded.
    now = 10_000_000_000
    by_id = {
        "local_a": {"sessionId": "local_a", "title": "가", "cwd": "E:\\\\a",
                    "lastActivityAt": now - 1 * hour, "lastTurnEdgeAt": now},
        "local_b": {"sessionId": "local_b", "title": "나", "cwd": "E:\\\\b",
                    "lastActivityAt": now - 30 * hour},
        "local_c": {"sessionId": "local_c", "title": "다", "cwd": "E:\\\\c",
                    "lastActivityAt": now - 2 * hour},
        "local_old": {"sessionId": "local_old", "title": "옛", "isArchived": True},
    }
    by_title = {meta["title"]: meta for meta in by_id.values()}
    rows = [{"title": "가", "status": "입력 대기 중", "folder": "", "summary": "",
             "group": "고정됨", "element": None},
            {"title": "나", "status": "", "folder": "", "summary": "끝",
             "group": "🔄 우선작업", "element": None},
            {"title": "없음", "status": "실행 중", "folder": "x", "summary": "",
             "group": "", "element": None}]
    groups = {"local_c": "프로그램별", "local_old": "보관"}
    sessions = build_sessions(rows, by_id, by_title, groups, now)
    # A folded group that says something waits stands in for that session;
    # an open one, or one without the note, does not.
    header_groups = [
        {"name": "⭕ 프로그램", "collapsed": True, "waiting": True, "element": None},
        {"name": "🔷 프로젝트", "collapsed": True, "waiting": False, "element": None},
        {"name": "고정됨", "collapsed": False, "waiting": True, "element": None}]
    stand_in = build_sessions([], by_id, by_title, {}, now, header_groups)
    assert [(s["key"], s["kind"], s["group_header"]) for s in stand_in] \
        == [("group:⭕ 프로그램", "awaiting", True)]
    assert [(s["key"], s["visible"]) for s in sessions] == [
        ("local_a", True), ("local_b", True), ("title:없음", True),
        ("local_c", False)]
    folded = sessions[-1]
    assert folded["info"] == "프로그램별 (접힘) · c · 2시간 전"
    assert folded["group"] == "프로그램별" and not folded["gone"]

    # Each tab's list.
    def keys(items):
        return [item[1]["key"] if item[0] == "session" else item
                for item in items]
    assert keys(tab_items("attention", sessions, [], set(), by_id, groups, now)) \
        == ["local_a"]
    assert keys(tab_items("attention", sessions, [], {("local_a", "awaiting")},
                          by_id, groups, now)) == [("empty", "확인할 세션이 없음")]
    assert keys(tab_items("running", sessions, [], set(), by_id, groups, now)) \
        == ["title:없음"]
    assert keys(tab_items("recent", sessions, [], set(), by_id, groups, now)) \
        == [("divider", "24시간 이내"), "local_c", ("divider", "1일 전"),
            "local_b"]
    favorites = [{"type": "session", "id": "local_b", "title": "나"},
                 {"type": "session", "id": "local_old", "title": "옛"},
                 {"type": "session", "id": "local_gone", "title": "지운 세션"}]
    fav = tab_items("favorites", sessions, favorites, set(), by_id, groups, now)
    assert [(s["key"], s["title"], s["info"], s["gone"]) for _k, s in fav][1:] \
        == [("local_old", "옛", "보관됨", True),
            ("local_gone", "지운 세션", "찾을 수 없음", True)]
    assert fav[0][1]["visible"]
    assert keys(tab_items("favorites", sessions, [], set(), by_id, groups, now)) \
        == [("empty", "세션에서 오른쪽 버튼 → 즐겨찾기에 추가")]

    class FakeReader:
        """Takes jobs without running them: the check never reads the app."""
        def __init__(self):
            self.jobs = []
            self.results = []

        def submit(self, job, **args):
            self.jobs.append((job, args))

        def drain(self):
            found, self.results = self.results, []
            return found

        def stop(self):
            pass

    root = tk.Tk()
    root.withdraw()
    reader = FakeReader()
    widget = Widget(root, reader)
    # The first read goes to the reader instead of holding up the window.
    assert reader.jobs == [("read", {"auto_expand": widget.auto_expand})]
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

    # Tabs: the 확인 필요 tab wears the waiting colour and counts; the list
    # shows that tab's rows.
    widget.clock = lambda: now
    # Never touch the real app's sidebar from here.
    widget.app_hwnd = 0
    widget.sessions = sessions
    widget.by_id = by_id
    widget.group_cache = dict(groups)
    widget.favorites = []
    widget.dismissed = set()
    widget.tab = "attention"
    widget.relayout()
    cell, label, line = widget.tab_parts["attention"]
    assert label.cget("text") == "확인 필요 1"
    assert label.cget("bg") == KIND_COLORS["awaiting"]["bg"]
    assert widget.frame.cget("highlightbackground") == KIND_COLORS["awaiting"]["bg"]
    assert widget.tab_parts["running"][1].cget("text") == "실행 중 1"
    assert widget.tab_parts["favorites"][1].cget("text") == "즐겨찾기"
    row = widget.rows[0]
    assert row.title.cget("text") == "● 가" and row.close.cget("text") == "✕"
    assert row.title.cget("bg") == KIND_COLORS["awaiting"]["bg"]
    for part in (row, *row.winfo_children()):
        assert not part.bind("<Button-1>"), part
    # ✕ hides the row and the tab goes quiet; the row waits below under
    # 방금 확인함, so a mistaken ✕ can be undone.
    widget.on_click(row.close)
    assert widget.dismissed == {("local_a", "awaiting")}
    assert widget.tab_parts["attention"][1].cget("text") == "확인 필요"
    assert widget.rows[0].winfo_children()[0].cget("text") == "방금 확인함"
    checked_row = widget.rows[1]
    assert checked_row.key == "checked:local_a"
    # The right side says what the session does now: hidden with ✕, it still
    # waits, in its colour.
    assert checked_row.status.cget("text") == "● 입력 대기 중"
    assert checked_row.status.cget("fg") == KIND_COLORS["awaiting"]["bg"]
    assert checked_row.info.cget("text").startswith("방금")

    # 나중에 다시 확인: held on the list, outlined, counted, not coloured.
    widget.hold(widget.by_key["checked:local_a"])
    assert widget.held == [{"key": "local_a", "sid": "local_a", "title": "가"}]
    assert json.loads(STATE_FILE.read_text(encoding="utf-8"))["held"] \
        == widget.held
    held_row = widget.rows[0]
    assert held_row.key == "held:local_a"
    assert held_row.status.cget("text") == "보류"
    assert int(held_row.cget("highlightthickness")) == 1
    assert widget.tab_parts["attention"][1].cget("text") == "확인 필요 1"
    assert widget.tab_parts["attention"][1].cget("bg") == widget.colors["bg"]
    assert all(r.key != "checked:local_a" for r in widget.rows
               if hasattr(r, "key"))   # not twice
    widget.release("local_a")
    assert widget.held == []
    assert widget.rows[1].key == "checked:local_a"
    # Its right-click menu offers to take it off 방금 확인함, which does.
    from types import SimpleNamespace as Click
    shown = []
    saved_popup = widget.popup_menu
    widget.popup_menu = lambda x, y, menu=None: shown.extend(
        menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
        if menu.type(i) == "command")
    widget.on_right_click(Click(widget=widget.rows[1].title, x_root=0, y_root=0))
    widget.popup_menu = saved_popup
    assert "나중에 다시 확인" in shown and "방금 확인함에서 빼기" in shown, shown
    widget.forget_checked("local_a")
    assert widget.checked == []
    assert widget.rows[0].cget("text") == "확인할 세션이 없음"

    # Leaving the list: a ✕ is not a departure (noted when pressed), nor a
    # folded-group stand-in.
    before = {"a": {"key": "a", "kind": "ready"},
              "b": {"key": "b", "kind": "awaiting"},
              "c": {"key": "c", "kind": "awaiting"},
              "group:x": {"key": "group:x", "kind": "awaiting"}}
    assert [s["key"] for s in departures(before, {"c"}, {("b", "awaiting")})] \
        == ["a"]
    assert [s["key"] for s in departures(before, set(), set())] \
        == ["a", "b", "c"]
    assert held_waiting([{"key": "a"}, {"key": "b"}], {"a"}) == [{"key": "b"}]
    # 방금 확인함 keeps the last hour, however many; the right side shows the
    # session's own status now.
    old = [{"key": f"k{n}", "sid": None, "title": f"t{n}",
            "at": now - n * 10 * 60_000} for n in range(8)]
    live = [{"key": f"k{n}", "sid": None, "title": f"t{n}",
             "status": "실행 중" if n == 1 else "", "kind": None, "info": "",
             "element": None, "group": "", "visible": True, "last": 0,
             "gone": False} for n in range(8)]
    items = tab_items("attention", live, [], set(), {}, {}, now, [], old)
    shown = [i[1] for i in items if i[0] == "session"]
    assert [s["key"] for s in shown] == [f"checked:k{n}" for n in range(6)]
    assert [s["status"] for s in shown[:2]] == ["", "실행 중"]
    stale = [dict(old[0], at=now - RECHECK_MS - 1)]
    assert tab_items("attention", live, [], set(), {}, {}, now, [], stale) \
        == [("empty", "확인할 세션이 없음")]

    # A tab click switches the list and is remembered; any tab click, the
    # open one too, asks the reader to unfold the sidebar. The click itself
    # never waits for that, and a second click while it runs adds nothing.
    reader.jobs.clear()
    widget.pending.clear()   # the first read, which this reader never answers
    widget.app_hwnd = 1
    widget.on_click(widget.tab_parts["attention"][1])
    widget.on_click(widget.tab_parts["attention"][1])
    assert widget.tab == "attention"
    assert reader.jobs == [("unfold", {"hwnd": 1})]
    # Nothing unfolded: the job just ends. Unfolded: its read is shown.
    reader.results.append({"job": "unfold", "read": None})
    widget.poll_reader()
    assert widget.pending == set()
    widget.on_click(widget.tab_parts["attention"][1])
    assert len(reader.jobs) == 2
    shown = []
    saved_apply = widget.apply_read
    widget.apply_read = shown.append
    reader.results.append({"job": "unfold", "read": {"active": None}})
    widget.poll_reader()
    assert shown == [{"active": None}] and widget.pending == set()
    widget.apply_read = saved_apply
    # The hover unfolds the same way, at most every HOVER_EXPAND_S.
    reader.jobs.clear()
    widget.auto_expand, widget.groups = True, [{"collapsed": True}]
    widget.last_hover_expand = 0
    widget.on_enter(None)
    widget.on_enter(None)
    assert reader.jobs == [("unfold", {"hwnd": 1})]
    widget.pending.clear()
    widget.groups = []
    # Opening a session goes to the reader too, as plain data.
    reader.jobs.clear()
    widget.open(dict(sessions[0], element="row:가"))
    assert reader.jobs[0][0] == "open"
    assert reader.jobs[0][1]["session"]["element"] == "row:가"
    widget.pending.clear()
    reader.jobs.clear()
    widget.app_hwnd = 0
    widget.on_click(widget.tab_parts["recent"][1])
    assert widget.tab == "recent"
    texts = [r.winfo_children()[0].cget("text") if not hasattr(r, "key")
             else r.title.cget("text") for r in widget.rows]
    assert texts == ["24시간 이내", "다", "1일 전", "나"], texts
    assert json.loads(STATE_FILE.read_text(encoding="utf-8"))["tab"] == "recent"
    widget.on_click(widget.tab_parts["running"][2])  # the underline counts too
    assert widget.tab == "running"

    # A read result: the sidebar's elements stay with the reader; the Tk side
    # gets handles. A title seen twice keeps its first row.
    first, second, header = object(), object(), object()
    read_rows = [{"title": "가", "element": first},
                 {"title": "가", "element": second},
                 {"title": "나", "element": None}]
    read_groups = [{"name": "⭕ 프로그램", "element": header},
                   {"name": "중첩", "element": None}]
    handles = detach_elements(read_rows, read_groups)
    assert handles == {"row:가": first, "group:⭕ 프로그램": header}
    assert [r["element"] for r in read_rows] == ["row:가", "row:가", None]
    assert [g["element"] for g in read_groups] == ["group:⭕ 프로그램", None]

    # A minute passing changes only the info line: the rows stay, their text
    # changes. A new status makes that one row anew.
    widget.show_tab("recent")
    before = list(widget.rows)
    widget.sessions = [dict(s, info=s["info"] + " 더") if s["key"] == "local_b"
                       else s for s in sessions]
    widget.render_list()
    assert widget.rows == before
    assert widget.rows[3].info.cget("text").endswith(" 더")
    widget.sessions = [dict(s, status="실행 중") if s["key"] == "local_c"
                       else s for s in sessions]
    widget.render_list()
    assert [r.key for r in widget.rows if hasattr(r, "key")] == ["local_b"]
    assert widget.rows[1] is before[3]
    widget.sessions = sessions
    widget.render_list()
    # Going to another tab and back reuses that tab's rows: each tab has its
    # own list, hidden while another tab is open.
    recent_rows = list(widget.rows)
    widget.show_tab("running")

    def state_of(tab):
        return widget.canvas.itemcget(widget.list_windows[tab], "state")
    assert state_of("recent") == "hidden" and state_of("running") == "normal"
    assert all(r.master is widget.listboxes["recent"] for r in recent_rows)
    widget.show_tab("recent")
    assert widget.rows == recent_rows
    assert state_of("recent") == "normal" and state_of("running") == "hidden"

    # The real reader: a job runs on its own thread, COM ready there, and
    # the answer comes back through drain().
    live = Reader()

    def probe():   # a UIA client fails to come up on a thread without COM
        return {"thread": threading.current_thread().name,
                "client": uia_client() is not None}
    live.do_probe = probe
    live.submit("probe")
    deadline = time.time() + 5
    answers = []
    while not answers and time.time() < deadline:
        answers = live.drain()
        time.sleep(0.01)
    live.stop()
    assert answers == [{"job": "probe", "read": {"thread": "reader", "client": True}}],         answers
    assert not live.thread.is_alive()

    # The open session, from the window's documents: the app shell's page
    # has no name, the session's says "<title> - Claude Code".
    assert current_title(["", "API 기획 - Claude Code", ""]) == "API 기획"
    assert current_title(["a - b - Claude Code"]) == "a - b"
    assert current_title(["", "Claude"]) is None
    assert current_title([" - Claude Code"]) is None
    assert current_title([]) is None

    # The header menu adds or removes that session; without one it says so.
    def menu_labels():
        return [widget.menu.entrycget(i, "label") if
                widget.menu.type(i) == "command" else "-"
                for i in range(widget.menu.index("end") + 1)]
    widget.favorites = []
    widget.apply_read({"active": (0, (None, None, [], [])), "by_id": by_id,
                       "by_title": by_title, "current": "나"})
    assert widget.current == {"sid": "local_b", "title": "나"}
    widget.build_menu(current=True)
    assert menu_labels()[:2] == ["★ 즐겨찾기에 추가 — 나", "-"]
    widget.menu.invoke(0)
    assert widget.favorites == [{"type": "session", "id": "local_b",
                                 "title": "나"}]
    widget.build_menu(current=True)
    assert menu_labels()[0] == "즐겨찾기에서 빼기 — 나"
    widget.menu.invoke(0)
    assert widget.favorites == []
    widget.apply_read({"active": (0, (None, None, [], [])), "by_id": by_id,
                       "by_title": by_title, "current": "모르는 제목"})
    widget.build_menu(current=True)
    assert menu_labels()[0] == "현재 세션을 찾을 수 없음"
    assert widget.menu.entrycget(0, "state") == "disabled"
    widget.build_menu()   # the tray's menu has no such item
    assert "현재 세션을 찾을 수 없음" not in menu_labels()
    widget.sessions, widget.by_id = sessions, by_id
    widget.group_cache = dict(groups)
    widget.render_list()

    # Favorites by session id: added from any tab, kept in the state file,
    # marked ★ elsewhere; a session without a file cannot be added.
    widget.show_tab("recent")
    session_b = widget.by_key["local_b"]
    widget.toggle_favorite(session_b)
    assert widget.favorites == [{"type": "session", "id": "local_b",
                                 "title": "나"}]
    assert json.loads(STATE_FILE.read_text(encoding="utf-8"))["favorites"] \
        == widget.favorites
    assert [r.title.cget("text") for r in widget.rows if hasattr(r, "key")] \
        == ["다", "★ 나"]
    widget.show_tab("favorites")
    assert [r.title.cget("text") for r in widget.rows] == ["나"]
    widget.toggle_favorite(session_b)
    assert widget.favorites == []
    widget.toggle_favorite(widget.by_key.get("title:없음") or
                           {"sid": None, "title": "없음"})
    assert widget.favorites == []

    # Auto-unfold: only when allowed, something is folded and the app is not
    # the window in front.
    folded = [{"collapsed": True}]
    assert auto_expand_due(True, folded, foreground=5, app_hwnd=9)
    assert not auto_expand_due(True, folded, foreground=9, app_hwnd=9)
    assert not auto_expand_due(False, folded, foreground=5, app_hwnd=9)
    assert not auto_expand_due(True, [{"collapsed": False}], 5, 9)
    assert not auto_expand_due(True, folded, 5, 0)

    # Moving an entry: before the target position, as the list is now.
    assert move_entry(["a", "b", "c", "d"], 0, 2) == ["b", "a", "c", "d"]
    assert move_entry(["a", "b", "c", "d"], 3, 0) == ["d", "a", "b", "c"]
    assert move_entry(["a", "b", "c", "d"], 1, 4) == ["a", "c", "d", "b"]
    assert move_entry(["a", "b", "c"], 1, 1) == ["a", "b", "c"]
    assert move_entry(["a", "b", "c"], 1, 2) == ["a", "b", "c"]

    # Saved favourites: sessions and well-formed dividers only.
    assert valid_favorite({"type": "session", "id": "local_x"})
    assert valid_favorite({"type": "divider", "id": "d1", "name": "급함",
                           "color": "red"})
    assert valid_favorite({"type": "divider", "id": "d1", "name": "",
                           "color": None})
    assert not valid_favorite({"type": "divider", "id": "d1", "name": "x",
                               "color": "gold"})
    assert not valid_favorite({"type": "session"})
    assert not valid_favorite("local_x")

    # 즐겨찾기 dividers: their own row; sessions under a coloured one get its
    # band; drag moves an entry; edit and delete.
    widget.sessions = sessions
    widget.favorites = [
        {"type": "divider", "id": "d1", "name": "급함", "color": "red"},
        {"type": "session", "id": "local_b", "title": "나"},
        {"type": "divider", "id": "d2", "name": "나중", "color": None},
        {"type": "session", "id": "local_c", "title": "다"}]
    widget.tab = "favorites"
    widget.relayout()
    assert [getattr(r, "fav_index", None) for r in widget.rows] == [0, 1, 2, 3]
    assert widget.rows[0].winfo_children()[0].cget("text") == "급함"
    assert widget.rows[0].winfo_children()[0].cget("fg") == DIVIDER_HEX["red"]
    bands = [c for c in widget.rows[1].winfo_children()
             if c not in (widget.rows[1].title, widget.rows[1].status,
                          widget.rows[1].info)]
    assert [b.cget("bg") for b in bands] == [DIVIDER_HEX["red"]]
    assert all(c in (widget.rows[3].title, widget.rows[3].status,
                     widget.rows[3].info)
               for c in widget.rows[3].winfo_children())  # no band: no colour
    widget.save_divider(widget.favorites[2], "나중에", "blue")
    assert widget.rows[2].winfo_children()[0].cget("text") == "나중에"
    widget.save_divider(None, "새 구분선", "green", at=1)
    assert [e.get("name") for e in widget.favorites] \
        == ["급함", "새 구분선", None, "나중에", None]
    widget.delete_divider(widget.favorites[1])
    assert [e["id"] for e in widget.favorites] == ["d1", "local_b", "d2", "local_c"]
    # A drag: press on 나 (index 1), move past 다, release.
    widget.favorites = move_entry(widget.favorites, 1, 4)
    assert [e["id"] for e in widget.favorites] == ["d1", "d2", "local_c", "local_b"]
    assert json.loads(STATE_FILE.read_text(encoding="utf-8"))["favorites"][0] \
        ["name"] == "급함"
    dialog = DividerDialog(widget, None, at=0)
    dialog.name.insert(0, "맨 위")
    dialog.pick("violet")
    dialog.save()
    assert widget.favorites[0]["name"] == "맨 위" \
        and widget.favorites[0]["color"] == "violet"
    widget.favorites = []

    # Only the header moves the window; a press in the list does not.
    from types import SimpleNamespace as Event
    widget.on_press(Event(x_root=-100, y_root=-100, widget=widget.main))
    assert widget.moving
    widget.on_press(Event(x_root=-100, y_root=-100,
                          widget=widget.tab_parts["recent"][1]))
    assert not widget.moving
    widget.on_release(Event(x_root=-100, y_root=-100, widget=widget.listbox))

    # A long list scrolls instead of growing past its share of the screen.
    widget.tab = "recent"
    widget.sessions = [
        {"key": f"k{n}", "sid": f"k{n}", "title": f"세션 {n}", "status": "",
         "kind": None, "info": "desk · 1시간 전", "element": None, "group": "",
         "visible": True, "last": now - n * hour, "gone": False}
        for n in range(80)]
    widget.relayout()
    left, top, right, bottom = work_area(root.winfo_id())
    assert int(widget.canvas.cget("height")) \
        <= max(120, int((bottom - top) * LIST_MAX_SHARE))
    assert widget.listbox.winfo_reqheight() > int(widget.canvas.cget("height"))

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

    def fit_slow(text, width):   # the old one-character-at-a-time cut
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…" if text else ""
    for sample in ("아주 긴 세션 제목 " * 6, "BookPrintAPI A4 소프트커버 페이지"):
        for width in (0, 5, 17, 40, 77, 120, 200, 400):
            assert fit(sample, font, width) == fit_slow(sample, width), width
    widget.sessions = [dict(widget.sessions[0], title="아주 긴 세션 제목 " * 10)]
    widget.width = 500
    widget.relayout()
    wide = widget.rows[1].title.cget("text")
    widget.width = MIN_WIDTH
    widget.relayout()
    narrow = widget.rows[1].title.cget("text")
    assert len(narrow) < len(wide), (narrow, wide)
    widget.width = None
    widget.sessions = []
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
