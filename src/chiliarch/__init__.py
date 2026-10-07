"""chiliarch: an outbound-only agent orchestration platform."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("chiliarch")
except PackageNotFoundError:
    __version__ = "0+uninstalled"

__all__ = ["__version__"]
