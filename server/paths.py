"""Per-user paths for the current official Devin CLI and GUI-owned data."""

from __future__ import annotations

import ctypes
import os
import shutil
from pathlib import Path
from uuid import UUID

_IS_WINDOWS = os.name == "nt"
_DOCUMENTS_FOLDER_ID = UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")


def devin_config_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "devin" if appdata else Path.home() / ".config" / "devin"


def cli_data_dir() -> str:
    local_appdata = os.environ.get("LOCALAPPDATA")
    root = Path(local_appdata) if local_appdata else Path.home() / ".local" / "share"
    return str(root / "devin" / "cli")


def gui_data_dir() -> str:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / ".config"
    return str(root / "devingui")


def default_db_path() -> str:
    return str(devin_config_dir() / "cli" / "sessions.db")


def default_transcripts_dir() -> str:
    return str(devin_config_dir() / "cli" / "transcripts")


def default_credentials_path() -> Path:
    return devin_config_dir() / "credentials.toml"


def user_config_path() -> Path:
    return devin_config_dir() / "config.json"


def find_devin_binary() -> str:
    """Resolve the override, PATH entry, or official Windows install.

    An invalid explicit override never falls back to a different executable.
    Missing executable: FileNotFoundError with setup instructions.
    """
    configured = os.environ.get("DEVINGUI_DEVIN_BIN")
    if configured:
        executable = Path(configured).expanduser()
        if not executable.is_absolute() or not executable.is_file():
            raise FileNotFoundError(
                "DEVINGUI_DEVIN_BIN must be an absolute path to the Devin executable "
                f"(without command arguments or surrounding quotes): {configured}"
            )
        return str(executable)

    binary = shutil.which("devin.exe") if _IS_WINDOWS else None
    binary = binary or shutil.which("devin")
    if binary:
        return str(Path(binary).absolute())

    locations = "on PATH"
    if _IS_WINDOWS:
        candidate = Path(cli_data_dir()) / "bin" / "devin.exe"
        if candidate.is_file():
            return str(candidate)
        locations += f" or at {candidate}"
    raise FileNotFoundError(
        f"Devin CLI not found {locations}. "
        "Install the official stable Devin CLI and restart the server, "
        "or set DEVINGUI_DEVIN_BIN to its absolute executable path."
    )


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


def _windows_documents_dir() -> Path:
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    get_folder = shell32.SHGetKnownFolderPath
    get_folder.argtypes = [ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_void_p)]
    get_folder.restype = ctypes.c_int32
    free = ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    folder_id = _GUID.from_buffer_copy(_DOCUMENTS_FOLDER_ID.bytes_le)
    result = ctypes.c_void_p()
    try:
        status = get_folder(ctypes.byref(folder_id), 0, None, ctypes.byref(result))
        if status < 0 or not result.value:
            raise OSError(f"Windows Documents folder lookup failed (HRESULT {status}).")
        return Path(ctypes.wstring_at(result))
    finally:
        if result.value:
            free(result)


def documents_dir() -> Path:
    if _IS_WINDOWS:
        try:
            return _windows_documents_dir()
        except OSError:
            pass
    return Path.home() / "Documents"


def default_archive_root() -> Path:
    configured = os.environ.get("DEVIN_GUI_ARCHIVE_DIR")
    if configured:
        return Path(configured).expanduser()
    legacy = Path.home() / "Documents" / "Devin_Chat"
    # Keep the archive index with existing exports instead of unhiding old sessions.
    if legacy.is_dir():
        return legacy
    return documents_dir() / "Devin_Chat"
