"""Locate read-only application assets separately from writable desktop data."""

import os
import sys
from pathlib import Path


def application_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def data_dir() -> Path:
    configured = os.environ.get("MPT_DATA_DIR")
    return Path(configured).expanduser().resolve() if configured else application_dir()


def desktop_data_dir() -> Path:
    if os.environ.get("MPT_DATA_DIR"):
        return data_dir()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "MoneyPrinterTurbo"


def prepare_desktop_data(directory: str | None = None) -> Path:
    import shutil

    target = Path(directory).expanduser().resolve() if directory else desktop_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    os.environ["MPT_DATA_DIR"] = str(target)
    # Never overwrite a user's config or assets when upgrading the application.
    config_file = target / "config.toml"
    if not config_file.exists():
        shutil.copyfile(application_dir() / "config.example.toml", config_file)
        config_file.chmod(0o600)
    resources = application_dir() / "resource"
    for source in resources.rglob("*"):
        destination = target / "resource" / source.relative_to(resources)
        if source.is_file() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    for folder in ("storage", "models", "resource/fonts", "resource/songs"):
        (target / folder).mkdir(parents=True, exist_ok=True)
    return target
