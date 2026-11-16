from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from anihub.common import storage
from anihub.common.config import get_config_dir, get_data_dir


def test_user_dirs_are_temporary(user_dirs):
    assert get_config_dir().is_relative_to(user_dirs)
    assert get_data_dir().is_relative_to(user_dirs)


def test_write_and_read(tmp_path):
    path = tmp_path / "history.json"
    storage.write_json(path, [{"title": "Дюрарара!!"}])
    assert storage.read_json(path, []) == [{"title": "Дюрарара!!"}]
    assert "Дюрарара" in path.read_text(encoding="utf-8")  # readable by hand, not \\u escapes
    assert [p.name for p in tmp_path.iterdir()] == ["history.json"]  # no temporary files left


def test_missing_file_is_default(tmp_path):
    assert storage.read_json(tmp_path / "none.json", {}) == {}


@pytest.mark.parametrize("content", ['[{"title": "trunc', "", "\xff\xfe broken", '{"a": 1}'])
def test_broken_file_is_moved_aside(tmp_path, content):
    path = tmp_path / "history.json"
    path.write_bytes(content.encode("latin-1"))
    before = len(storage.broken_files)
    assert storage.read_json(path, []) == []
    assert not path.exists()
    (moved,) = tmp_path.glob("history.broken-*.json")
    assert moved.read_bytes() == content.encode("latin-1")
    assert storage.broken_files[before:] == [(path, moved)]


def test_failed_write_keeps_the_old_file(tmp_path):
    path = tmp_path / "settings.json"
    storage.write_json(path, {"quality": 1080})
    with pytest.raises(TypeError):
        storage.write_json(path, {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"quality": 1080}
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_interrupted_replace_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    storage.write_json(path, [1, 2, 3])

    def power_loss(*_args):
        raise OSError("power loss")

    monkeypatch.setattr(storage.os, "replace", power_loss)
    with pytest.raises(OSError, match="power loss"):
        storage.write_json(path, [4])
    assert storage.read_json(path, []) == [1, 2, 3]
    assert [p.name for p in tmp_path.iterdir()] == ["history.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_modes(tmp_path):
    token = tmp_path / "mal_token.json"
    storage.write_json(token, {"access_token": "secret"}, private=True)
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    shared = tmp_path / "settings.json"
    shared.write_text("{}", encoding="utf-8")
    os.chmod(shared, 0o640)
    storage.write_json(shared, {"quality": 720})
    assert stat.S_IMODE(shared.stat().st_mode) == 0o640
    new = tmp_path / "ongoing.json"
    storage.write_json(new, {})
    assert stat.S_IMODE(new.stat().st_mode) == 0o666 & ~storage._UMASK


def test_read_only_mode_leaves_a_broken_file_in_place(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    assert storage.read_json(path, {}, set_aside=False) == {}
    assert path.read_text(encoding="utf-8") == "{broken"
    assert not list(tmp_path.glob("*.broken-*"))
    assert path in storage.unusable_files
    # the TUI reads it normally afterwards and moves it aside
    assert storage.read_json(path, {}) == {}
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="chmod 000")
def test_unreadable_file_is_not_overwritten(tmp_path):
    path = tmp_path / "history.json"
    storage.write_json(path, [1])
    path.chmod(0)
    try:
        assert storage.read_json(path, []) == []
        with pytest.raises(OSError, match="could not be read"):
            storage.write_json(path, [])
    finally:
        path.chmod(0o600)
    assert path.read_text(encoding="utf-8") == "[\n    1\n]"
    # readable again: writing works
    assert storage.read_json(path, []) == [1]
    storage.write_json(path, [2])
    assert storage.read_json(path, []) == [2]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
def test_symlink_is_kept_and_its_target_written(tmp_path):
    real_dir = tmp_path / "dotfiles"
    real_dir.mkdir()
    real = real_dir / "settings.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    storage.write_json(link, {"a": 1})
    assert link.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8")) == {"a": 1}
    assert sorted(p.name for p in real_dir.iterdir()) == ["settings.json"]


def test_stale_temp_files_are_removed_fresh_ones_kept(tmp_path):
    path = tmp_path / "history.json"
    stale = tmp_path / ".history.json.abc123.tmp"
    fresh = tmp_path / ".history.json.def456.tmp"
    other = tmp_path / ".settings.json.old999.tmp"
    for f in (stale, fresh, other):
        f.write_text("x", encoding="utf-8")
    old = stale.stat().st_mtime - 7200
    os.utime(stale, (old, old))
    os.utime(other, (old, old))
    storage.write_json(path, [])
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()  # another file's temp is not touched
