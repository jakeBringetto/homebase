from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("homebase")
except PackageNotFoundError:
    __version__ = "dev"
