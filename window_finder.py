"""Window finder — locates any app/game window by title using Win32 API."""

import subprocess
from typing import Optional

import win32con
import win32gui


def list_windows() -> list[str]:
    """Return titles of all visible, non-empty windows, sorted alphabetically.

    Includes a PowerShell fallback for UWP/Microsoft Store apps that are
    invisible to win32gui (e.g. Xbox Game Bar, Microsoft Store games).
    """
    windows: list[str] = []

    def callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title.strip():
                windows.append(title)

    win32gui.EnumWindows(callback, None)

    # PowerShell fallback for UWP apps not visible to win32gui
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object {$_.MainWindowTitle} | "
             "Select-Object -ExpandProperty MainWindowTitle"],
            capture_output=True, text=True, timeout=3
        )
        for line in result.stdout.splitlines():
            title = line.strip()
            if title and title not in windows:
                windows.append(title)
    except Exception:
        pass  # PowerShell unavailable — win32gui list is sufficient

    return sorted(set(windows))


# Backward-compat alias
list_open_windows = list_windows


def get_window_region(
    window_title: str,
    fuzzy: bool = True,
) -> Optional[dict]:
    """Find a window by title and return its screen region dict.

    If fuzzy=True, matches any window whose title CONTAINS window_title
    (case-insensitive). Prefers foreground window on multiple matches.
    Returns None if not found or if window rect is invalid/zero-area.
    """
    foreground_hwnd = win32gui.GetForegroundWindow()
    candidates: list[tuple[int, str, bool]] = []  # (hwnd, title, is_foreground)

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        match = (
            window_title.lower() in title.lower() if fuzzy
            else title == window_title
        )
        if match:
            candidates.append((hwnd, title, hwnd == foreground_hwnd))

    win32gui.EnumWindows(callback, None)

    if not candidates:
        return None

    # Prefer foreground window; otherwise take first match
    candidates.sort(key=lambda c: (not c[2],))  # foreground first
    hwnd, title, _ = candidates[0]

    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None

    x, y, x2, y2 = rect
    w, h = x2 - x, y2 - y

    # Reject zero-area or fully-negative (minimized to taskbar) windows
    if w <= 0 or h <= 0:
        return None

    return {
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "window_title": title,
    }


def get_window_hwnd(window_title: str) -> Optional[int]:
    """Return the HWND for a window, or None if not found."""
    region = get_window_region(window_title)
    if not region:
        return None
    return win32gui.FindWindow(None, region["window_title"]) or None


def focus_window(window_title: str) -> bool:
    """Bring a window to the foreground. Returns True if successful."""
    region = get_window_region(window_title)
    if not region:
        return False
    try:
        hwnd = win32gui.FindWindow(None, region["window_title"])
        if not hwnd:
            return False
        placement = win32gui.GetWindowPlacement(hwnd)
        if placement[1] == win32con.SW_SHOWMINIMIZED:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False
