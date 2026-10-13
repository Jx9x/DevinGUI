"""Folder browsing must stay on local filesystems behind the app header.

All requests go through the real app so the tests cover the actual route
wiring. Filesystem and drive-type operations are mocked; blocked inputs must
never reach them.
"""

from __future__ import annotations

import os
import stat
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from server.browse_routes import _assert_local_directory, _device_component
from server.main import app

BROWSE_HEADER = {"X-DevinGUI-Browse": "1"}
REPARSE_POINT = 0x400


def client(**kwargs):
    kwargs.setdefault("base_url", "http://localhost:8000")
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    return TestClient(app, **kwargs)


def _drive_types(mapping: dict[str, int]):
    return patch(
        "ctypes.windll.kernel32.GetDriveTypeW",
        side_effect=lambda root: mapping.get(str(root)[:1].upper(), 0),
    )


def _stat_result(reparse: bool = False):
    result = MagicMock(spec=os.stat_result)
    result.st_file_attributes = REPARSE_POINT if reparse else 0
    result.st_mode = stat.S_IFDIR
    return result


def _lstat_components(mapping: dict[str, int]):
    def lstat(path):
        key = str(path).replace("/", "\\").rstrip("\\")
        return _stat_result(reparse=bool(mapping.get(key, 0)))

    return patch("os.lstat", side_effect=lstat)


@pytest.mark.parametrize("headers", [
    {},
    {"X-DevinGUI-Browse": "0"},
    {"X-DevinGUI-Browse": "yes"},
    {"X-DevinGUI-Browse": ""},
])
def test_browse_rejects_missing_or_wrong_header_before_filesystem(headers):
    with (
        patch("server.browse_routes._browse_roots") as roots,
        patch("server.browse_routes._browse_directory") as directory,
    ):
        response = client().get("/api/browse", headers=headers)
        assert response.status_code == 403
        assert response.json() == {
            "detail": "Folder browsing requires the application browse header.",
            "error": "browse",
        }
        with_path = client().get(
            "/api/browse", params={"path": "/tmp"}, headers=headers
        )
        assert with_path.status_code == 403
    roots.assert_not_called()
    directory.assert_not_called()


def test_browse_rejects_duplicate_header_values():
    with patch("server.browse_routes._browse_roots") as roots:
        response = client().get(
            "/api/browse",
            headers=[("X-DevinGUI-Browse", "1"), ("X-DevinGUI-Browse", "1")],
        )
    assert response.status_code == 403
    assert response.json()["error"] == "browse"
    roots.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows lexical path policy")
@pytest.mark.parametrize("path", [
    "\\\\server\\share",
    "//server/share",
    "\\\\?\\C:\\",
    "\\\\.\\PhysicalDrive0",
    "/\\mixed",
    "\\/mixed",
    "C:relative",
    "C:",
    "\\rooted",
    "/rooted",
    "C:\\dir:stream",
    "C:\\dir\\file:stream",
    "C:\\NUL\\x",
    "C:\\con\\x",
    "C:\\LPT1\\x",
    "C:\\com1.txt\\x",
    "C:\\COM¹\\x",
    "C:\\COM²\\x",
    "C:\\LPT³\\x",
    "C:\\CONIN$\\x",
    "C:\\CONOUT$\\x",
    "C:\\CON .txt\\x",
    "C:\\NUL \\x",
    "C:\\foo\0bar",
])
def test_windows_unsafe_paths_rejected_before_filesystem(path):
    with (
        _drive_types({"C": 3}) as drive_type,
        patch("os.lstat") as lstat,
        patch("os.listdir") as listdir,
        patch("os.path.isdir") as isdir,
    ):
        response = client().get(
            "/api/browse", params={"path": path}, headers=BROWSE_HEADER
        )
    assert response.status_code == 403
    assert response.json()["error"] == "browse"
    drive_type.assert_not_called()
    lstat.assert_not_called()
    listdir.assert_not_called()
    isdir.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows drive policy")
def test_remote_drive_is_rejected_before_any_stat():
    with (
        _drive_types({"Z": 4}) as drive_type,
        patch("os.lstat") as lstat,
        patch("os.listdir") as listdir,
        patch("os.path.isdir") as isdir,
    ):
        response = client().get(
            "/api/browse", params={"path": "Z:\\dir"}, headers=BROWSE_HEADER
        )
    assert response.status_code == 403
    assert response.json()["error"] == "browse"
    drive_type.assert_called_once_with("Z:\\")
    lstat.assert_not_called()
    listdir.assert_not_called()
    isdir.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows drive policy")
@pytest.mark.parametrize("drive_type", [0, 1])
def test_unknown_or_unavailable_drives_are_rejected(drive_type):
    with _drive_types({"Q": drive_type}), patch("os.lstat") as lstat:
        response = client().get(
            "/api/browse", params={"path": "Q:\\dir"}, headers=BROWSE_HEADER
        )
    assert response.status_code == 403
    lstat.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows drive policy")
@pytest.mark.parametrize("drive_type", [2, 3, 5, 6])
def test_local_drive_types_list_directories(drive_type):
    with (
        _drive_types({"C": drive_type}),
        _lstat_components({}),
        patch("os.listdir", return_value=["proj"]),
        patch("os.path.isdir", return_value=True),
    ):
        response = client().get(
            "/api/browse", params={"path": "C:\\src"}, headers=BROWSE_HEADER
        )
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "C:\\src"
    assert body["entries"] == [{"name": "proj", "path": "C:\\src\\proj", "is_dir": True}]


@pytest.mark.skipif(os.name != "nt", reason="Windows drive policy")
def test_junction_traversal_is_denied_before_target_operations():
    lstat_map = {
        "C:\\safe": 0,
        "C:\\safe\\junction": REPARSE_POINT,
        "C:\\safe\\junction\\deeper": 0,
    }
    with (
        _drive_types({"C": 3}),
        _lstat_components(lstat_map) as lstat,
        patch("os.listdir") as listdir,
        patch("os.path.isdir") as isdir,
    ):
        response = client().get(
            "/api/browse",
            params={"path": "C:\\safe\\junction\\deeper"},
            headers=BROWSE_HEADER,
        )
    assert response.status_code == 403
    assert response.json()["error"] == "browse"
    walked = [call.args[0].replace("/", "\\") for call in lstat.call_args_list]
    assert "C:\\safe\\junction\\deeper" not in walked
    listdir.assert_not_called()
    isdir.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows drive policy")
def test_junction_children_are_skipped_before_isdir():
    lstat_map = {"C:\\src\\junction": REPARSE_POINT}

    def is_directory(path):
        assert "junction" not in str(path)
        return str(path).endswith("plain")

    with (
        _drive_types({"C": 3}),
        _lstat_components(lstat_map),
        patch("os.listdir", return_value=["junction", "plain"]),
        patch("os.path.isdir", side_effect=is_directory),
    ):
        response = client().get(
            "/api/browse", params={"path": "C:\\src"}, headers=BROWSE_HEADER
        )
    assert response.status_code == 200
    assert response.json()["entries"] == [
        {"name": "plain", "path": "C:\\src\\plain", "is_dir": True}
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows drive policy")
def test_drive_roots_exclude_remote_drives_without_stat():
    isdir = MagicMock(return_value=True)
    with (
        _drive_types({"C": 3, "D": 2, "Z": 4, "Q": 0}),
        patch("os.path.isdir", isdir),
    ):
        response = client().get("/api/browse", headers=BROWSE_HEADER)
    assert response.status_code == 200
    paths = {entry["path"] for entry in response.json()["roots"]}
    assert "C:\\" in paths and "D:\\" in paths
    assert "Z:\\" not in paths and "Q:\\" not in paths
    stated = {call.args[0] for call in isdir.call_args_list}
    assert "Z:\\" not in stated and "Q:\\" not in stated


def test_missing_local_directory_keeps_200_error_body(tmp_path):
    missing = tmp_path / "does-not-exist"
    response = client().get(
        "/api/browse", params={"path": str(missing)}, headers=BROWSE_HEADER
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]
    assert body["entries"] == []


def test_roots_listing_needs_no_filesystem_when_header_missing():
    response = client().get("/api/browse")
    assert response.status_code == 403


@pytest.mark.parametrize("component", [
    "CON",
    "con",
    "NUL",
    "PRN",
    "AUX",
    "COM1",
    "com9",
    "LPT1",
    "lpt9",
    "COM¹",
    "COM²",
    "COM³",
    "LPT¹",
    "LPT²",
    "LPT³",
    "CONIN$",
    "CONOUT$",
    "conin$",
    "con.txt",
    "NUL.txt",
    "com1.txt",
    "aux.txt",
    "CON .txt",
    "NUL ",
    "LPT1 ",
    "COM1.",
    " ",
    ".",
    "...",
])
def test_windows_device_components_rejected_lexically(component):
    assert _device_component(component)


@pytest.mark.parametrize("component", [
    "normal",
    "project",
    "project.txt",
    "my-folder",
    "x",
    "x.",
    "COM10",
    "LPT10",
])
def test_ordinary_components_are_not_devices(component):
    assert not _device_component(component)


@pytest.mark.parametrize("path", [
    "//server/share",
    "\\\\server\\share",
    "/\\mixed",
    "\\/mixed",
])
def test_network_path_prefixes_rejected_lexically_on_any_platform(path):
    with patch("os.lstat") as lstat:
        with pytest.raises(ValueError, match="network"):
            _assert_local_directory(path, path)
    lstat.assert_not_called()


def test_posix_absolute_path_walks_each_component():
    stats = []

    def lstat(path):
        stats.append(str(path))
        return _stat_result()

    with (
        patch("server.browse_routes._on_windows", return_value=False),
        patch("os.lstat", side_effect=lstat),
    ):
        _assert_local_directory("/safe/dir", "/safe/dir")
    assert len(stats) == 2


def test_posix_symlink_component_stops_walk_before_children():
    stats = []

    def lstat(path):
        stats.append(str(path))
        result = MagicMock(spec=os.stat_result)
        result.st_mode = stat.S_IFLNK if "link" in str(path) else stat.S_IFDIR
        return result

    with (
        patch("server.browse_routes._on_windows", return_value=False),
        patch("os.lstat", side_effect=lstat),
    ):
        with pytest.raises(ValueError, match="link"):
            _assert_local_directory("/a/link/dir", "/a/link/dir")
    assert len(stats) == 2


def test_posix_relative_path_is_rejected_before_stat():
    with (
        patch("server.browse_routes._on_windows", return_value=False),
        patch("os.lstat") as lstat,
    ):
        with pytest.raises(ValueError):
            _assert_local_directory("relative/dir", "relative/dir")
    lstat.assert_not_called()
