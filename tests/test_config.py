from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from anihub.common import config


def test_import_does_not_scan_extractors():
    # the scan was only for the text of a config.toml that anihub never reads
    code = "import sys, anihub.common.config; print('anihub.common.extractors' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)  # noqa: S603
    assert done.stdout.strip() == "False"


def test_no_config_toml(user_dirs):
    assert not hasattr(config, "get_config_path")
    config.get_config_dir()
    assert not list(user_dirs.rglob("config.toml"))


def test_old_directory_is_moved(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIHUB_TEST_BASE", str(tmp_path))
    old = tmp_path / config.OLD_APP_NAME
    old.mkdir()
    (old / "history.json").write_text("[]", encoding="utf-8")

    path = config._get_base_dir("ANIHUB_TEST_BASE", tmp_path / "unused")

    assert path == tmp_path / config.APP_NAME
    assert (path / "history.json").read_text(encoding="utf-8") == "[]"
    assert not old.exists()


def test_busy_old_directory_stays_in_use(tmp_path, monkeypatch):
    monkeypatch.setenv("ANIHUB_TEST_BASE", str(tmp_path))
    monkeypatch.setattr(config, "_not_moved", set())
    old = tmp_path / config.OLD_APP_NAME
    old.mkdir()
    attempts = []

    def busy(self: Path, *_: object) -> Path:
        attempts.append(self)
        raise PermissionError(self)

    monkeypatch.setattr(Path, "rename", busy)

    assert config._get_base_dir("ANIHUB_TEST_BASE", tmp_path) == old
    # the rest of this run uses it too, without new attempts: all files stay together
    assert config._get_base_dir("ANIHUB_TEST_BASE", tmp_path) == old
    assert attempts == [old]
    assert not (tmp_path / config.APP_NAME).exists()
