import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filecleaner import config, i18n, review  # noqa: E402
from filecleaner.rules import Rules  # noqa: E402

OLD = time.time() - 40 * 86400  # «давно»: старше всех порогов свежести


def write(path: Path, data: bytes | str, *, old: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if old:
        os.utime(path, (OLD, OLD))
    return path


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Отдельные «домашняя папка», данные программы и Ready for approval — настоящие не трогаются."""
    home = tmp_path / "home"
    for name in ("Downloads", "Documents", "Desktop"):
        (home / name).mkdir(parents=True)
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "REVIEW_HOME", home / config.REVIEW_DIR_NAME)
    monkeypatch.setattr(review, "fixed_drives", lambda: [])  # не искать партии на настоящих дисках
    return home


@pytest.fixture
def rules(tmp_path):
    """Правила по умолчанию, но без системного мусора и без правки реестра/ярлыков."""
    r = Rules.load(tmp_path / "no-user-rules.toml")
    for key in ("temp", "browser_cache", "app_cache", "crash_dumps", "dev_cache", "shader_cache", "recycle_bin"):
        r.data["junk"][key] = "off"
    r.data["scan"]["min_file_age_minutes"] = 0
    r.data["files"]["empty_dirs_min_age_days"] = 0
    r.data["duplicates"]["min_size"] = "1KB"
    r.data["duplicates"]["min_folder_size"] = "1KB"
    r.data["links"]["update_office_recent"] = False
    r.data["links"]["update_shortcuts"] = False
    r.data["ai"]["enabled"] = False
    r.data["ui"]["language"] = "ru"
    return r


@pytest.fixture(autouse=True)
def russian():
    """Тесты по умолчанию — на русском, как исходные фразы; язык не протекает между тестами."""
    i18n.set_language("ru")
    yield
    i18n.set_language("ru")
