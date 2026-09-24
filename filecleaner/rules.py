"""Правила «что считать ненужным»: значения по умолчанию + твой rules.toml поверх них."""
from __future__ import annotations

import shutil
import tomllib
from pathlib import Path
from typing import Any

from . import config
from .fsutil import parse_size

MODES = ("delete", "review", "report", "off")
DEFAULT_RULES = Path(__file__).with_name("default_rules.toml")
# Какие ключи — режимы, и какие режимы для них разрешены.
MODE_KEYS = {
    "junk.temp": MODES, "junk.browser_cache": MODES, "junk.app_cache": MODES,
    "junk.crash_dumps": MODES, "junk.dev_cache": MODES, "junk.shader_cache": MODES,
    "junk.recycle_bin": ("delete", "report", "off"),
    "files.thumbs": MODES, "files.office_locks": MODES, "files.partial_downloads": MODES,
    "files.empty_dirs": ("delete", "report", "off"),
    "duplicates.same_folder": MODES, "duplicates.copy_names": MODES, "duplicates.in_downloads": MODES,
    "duplicates.other": MODES, "duplicates.folders": MODES,
    "archives.extracted": MODES,
    "installers.installed": MODES, "installers.old_versions": MODES, "installers.other": MODES,
    "vm_disks.unregistered": MODES,
    "old_files": MODES,
}
# Свои файлы удалять без проверки нельзя: для них режим delete превращается в review.
USER_FILE_PREFIXES = ("duplicates.", "archives.", "installers.", "vm_disks.", "old_files", "files.office",
                      "files.partial")


class RulesError(ValueError):
    pass


def user_rules_path() -> Path:
    return config.DATA_DIR / "rules.toml"


def _merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


class Rules:
    def __init__(self, data: dict, source: Path | None = None) -> None:
        self.data = data
        self.source = source

    @classmethod
    def load(cls, path: Path | None = None) -> "Rules":
        try:
            data = tomllib.loads(DEFAULT_RULES.read_text(encoding="utf-8"))
            source = Path(path) if path else user_rules_path()
            used = None
            if source.exists():
                _merge(data, tomllib.loads(source.read_text(encoding="utf-8-sig")))
                used = source
        except tomllib.TOMLDecodeError as exc:
            raise RulesError(f"Ошибка в файле правил: {exc}") from exc
        rules = cls(data, used)
        rules.validate()
        return rules

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def mode(self, dotted: str) -> str:
        value = self.get(dotted, "off")
        if isinstance(value, dict):
            value = value.get("mode", "off")
        return value

    def size(self, dotted: str, default: str = "0") -> int:
        return parse_size(self.get(dotted, default))

    @property
    def sectors(self) -> list[dict]:
        return [s for s in self.get("sector", []) or [] if isinstance(s, dict) and s.get("name")]

    def roots(self) -> dict[str, Path]:
        """Папки для скана: имена вроде "downloads" превращаются в настоящие пути."""
        folders = config.user_folders()
        result: dict[str, Path] = {}
        for item in self.get("scan.roots", []):
            if item in folders:
                result[item] = folders[item]
            else:
                path = Path(item).expanduser()
                if path.is_dir():
                    result[str(path)] = path
        return result

    def sort_folders(self) -> list[Path]:
        folders = config.user_folders()
        result = []
        for item in self.get("sort.folders", []):
            path = folders.get(item) or Path(item).expanduser()
            if path.is_dir():
                result.append(path)
        return result

    def validate(self) -> None:
        for key, allowed in MODE_KEYS.items():
            value = self.mode(key)
            if value not in allowed:
                raise RulesError(f"В правилах {key} = {value!r}: можно только {', '.join(allowed)}.")
            if value == "delete" and key.startswith(USER_FILE_PREFIXES):
                raise RulesError(
                    f"В правилах {key} = \"delete\": твои файлы без проверки не удаляются — "
                    f"поставь \"review\" (перенос в «{config.REVIEW_DIR_NAME}»)."
                )
        for key in ("duplicates.min_size", "duplicates.min_folder_size", "old_files.min_size"):
            try:
                self.size(key)
            except ValueError as exc:
                raise RulesError(f"В правилах {key}: {exc}") from exc


def ensure_user_rules() -> Path:
    """Создаёт твою копию правил (если её ещё нет) и возвращает путь к ней."""
    path = user_rules_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DEFAULT_RULES, path)
    return path
