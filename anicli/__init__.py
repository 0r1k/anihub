"""AniCli - Anime CLI and Web Interface"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("anihub")
except PackageNotFoundError:  # running from source without installing
    __version__ = "0.0.0+dev"


def run_cli():
    """Entry point for CLI application"""
    from anicli.main import main  # noqa

    main()
