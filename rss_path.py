"""Register this installation in the current Windows user's PATH."""

import ntpath
from pathlib import Path
import sys


def normalized_path(value):
    return ntpath.normcase(ntpath.normpath(ntpath.expandvars(value.strip().strip('"'))))


def update_path_value(current, directory, remove=False):
    """Preserve every unrelated PATH segment, including unexpanded variables."""
    if not directory or ";" in directory or "\0" in directory:
        raise ValueError("The RSS directory must not be empty or contain semicolons or NUL characters")
    target = normalized_path(directory)
    segments = current.split(";") if current else []
    matches = [bool(part.strip()) and normalized_path(part) == target for part in segments]
    if remove:
        return ";".join(part for part, matches_target in zip(segments, matches) if not matches_target)
    if any(matches):
        return current
    separator = "" if not current or current.endswith(";") else ";"
    return current + separator + directory


def broadcast_environment_change():
    import ctypes
    from ctypes import wintypes

    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                     wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    # Notify Explorer so newly launched terminals inherit the updated user PATH.
    if not send(0xFFFF, 0x001A, 0, "Environment", 2, 5000, ctypes.byref(result)):
        raise OSError("Windows environment-change notification was not acknowledged")


def register_user_path(directory: Path, remove=False):
    if sys.platform != "win32":
        raise RuntimeError("Run add_to_path.cmd / remove_from_path.cmd in Windows, not WSL")
    import winreg

    directory = directory.resolve()
    if not remove and not all((directory / name).is_file() for name in ("rss.cmd", "rss-tui.cmd", "rss_update.py")):
        raise ValueError("RSS command files are missing from this directory")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        try:
            old, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            old, kind = "", winreg.REG_EXPAND_SZ
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(old, str):
            raise ValueError("The existing user PATH is not a string; no changes made")
        new = update_path_value(old, str(directory), remove)
        changed = new != old
        if changed:
            # Keep the original registry value type and percent-variable expressions.
            winreg.SetValueEx(key, "Path", 0, kind, new)
    warning = ""
    if changed:
        try:
            broadcast_environment_change()
        except (OSError, AttributeError) as error:
            warning = str(error)
    return changed, warning
