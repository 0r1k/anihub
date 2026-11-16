"""Tests never touch the files of the user: config and data directories point to temporary ones.

Set before anihub is imported (modules read them at import) and again for every test (a clean directory).
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_DIR_VARIABLES = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA")


def _point_user_dirs(path: str, setenv=os.environ.__setitem__) -> None:
    for var in _DIR_VARIABLES:
        setenv(var, path)
    # macOS has no variable for Application Support: it is under the home directory
    if sys.platform == "darwin":
        setenv("HOME", path)


_import_dir = tempfile.mkdtemp(prefix="anihub-tests-")
_point_user_dirs(_import_dir)
# best effort: on Windows the log file anihub opens on import can still be open at exit
atexit.register(shutil.rmtree, _import_dir, ignore_errors=True)


def pytest_configure(config: pytest.Config) -> None:
    # CI tests the built wheel (ANIHUB_TEST_INSTALLED=1): the source checkout must not shadow it on sys.path
    if os.environ.get("ANIHUB_TEST_INSTALLED"):
        import anihub

        package = Path(anihub.__file__).resolve().parent
        if package == config.rootpath.resolve() / "anihub":
            msg = f"anihub is imported from the source checkout {package}, not from the installed wheel"
            raise pytest.UsageError(msg)


def pytest_report_header(config: pytest.Config) -> str:
    import anihub

    return f"anihub {anihub.__version__}: {Path(anihub.__file__).parent}"


@pytest.fixture(autouse=True)
def user_dirs(tmp_path, monkeypatch):
    """A clean config and data directory for every test."""
    _point_user_dirs(str(tmp_path), monkeypatch.setenv)
    return tmp_path


@pytest.fixture(autouse=True)
def english():
    """Every test starts in English: the language is a module global, a TUI test leaves it switched."""
    from anihub.tui import i18n

    i18n.set_language(i18n.DEFAULT_LANGUAGE)
