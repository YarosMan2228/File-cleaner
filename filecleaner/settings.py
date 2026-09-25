"""Настройки для окна программы: что можно менять кнопками и как это записать в rules.toml."""
from __future__ import annotations

import os
import re

from . import config, rules_edit
from .rules import Rules, RulesError, ensure_user_rules, user_rules_path

AFTER = ("nothing", "sleep", "shutdown")
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MODEL = re.compile(r"^[\w.:/\-]+$")
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


class SettingsError(ValueError):
    pass


def read(rules: Rules) -> dict:
    return {
        "ai": {
            "enabled": bool(rules.get("ai.enabled", False)),
            "model": str(rules.get("ai.model", "qwen2.5:7b")),
            "min_confidence": int(rules.get("ai.min_confidence", 80)),
            "about": str(rules.get("ai.about", "") or ""),
        },
        "sectors": [{
            "name": str(s.get("name", "")), "description": str(s.get("description", "") or ""),
            "keywords": [str(k) for k in s.get("keywords", []) or []],
            "sources": [str(k) for k in s.get("sources", []) or []],
            "types": [str(k) for k in s.get("types", []) or []],
            "target": str(s.get("target", "") or ""),
        } for s in rules.sectors],
        "protect": {
            "keep_keywords": [str(k) for k in rules.get("protect.keep_keywords", []) or []],
            "paths": [str(k) for k in rules.get("protect.paths", []) or []],
            "name_keywords": [str(k) for k in rules.get("protect.name_keywords", []) or []],
        },
        "night": {
            "after": str(rules.get("night.after", "nothing")),
            "auto_delete": bool(rules.get("night.auto_delete", True)),
            "drives": bool(rules.get("night.drives", True)),
            "sort_folders": [str(k) for k in rules.get("night.sort_folders", []) or []],
        },
        "types": list(config.TYPES),
        "file": str(user_rules_path()),
    }


def _list(value, what: str, limit: int = 80, lower: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise SettingsError(f"{what}: нужен список.")
    seen: dict[str, str] = {}
    for item in value:
        text = str(item).strip()
        if lower:
            text = text.lower()
        if not text:
            continue
        if len(text) > limit:
            raise SettingsError(f"{what}: «{text[:30]}…» — слишком длинно.")
        seen.setdefault(text.lower(), text)
    return list(seen.values())


def _text(value, what: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise SettingsError(f"{what}: слишком длинно (больше {limit} знаков).")
    return text


def _domain(value: str) -> str:
    text = re.sub(r"^https?://", "", value.strip().lower()).removeprefix("www.").split("/", 1)[0]
    if not _DOMAIN.match(text):
        raise SettingsError(f"Сайт «{value}» — не похоже на адрес сайта (нужно вроде rtu.lv).")
    return text


def _sectors(value) -> list[dict]:
    if not isinstance(value, list):
        raise SettingsError("Секторы: нужен список.")
    reserved = {name.lower() for name in config.TYPES} | {
        config.OTHER_TYPE.lower(), config.REVIEW_DIR_NAME.lower(), config.RETURN_DIR_NAME.lower()}
    result, names = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise SettingsError("Сектор записан неправильно.")
        name = _text(raw.get("name"), "Название сектора", 60)
        if not name:
            raise SettingsError("У сектора должно быть название — это имя папки.")
        if _BAD_NAME.search(name) or name.strip(". ") != name:
            raise SettingsError(f"«{name}»: в названии папки нельзя использовать < > : \" / \\ | ? * и точку в конце.")
        if name.lower() in reserved:
            raise SettingsError(f"«{name}» — так уже называется папка типа файлов; выбери другое название.")
        if name.lower() in names:
            raise SettingsError(f"Сектор «{name}» указан дважды.")
        names.add(name.lower())
        types = _list(raw.get("types", []), f"Типы файлов «{name}»")
        unknown = [t for t in types if t not in config.TYPES]
        if unknown:
            raise SettingsError(f"Типы файлов «{name}»: не знаю тип «{unknown[0]}».")
        result.append({
            "name": name,
            "description": _text(raw.get("description"), f"Описание «{name}»", 600),
            "sources": [_domain(s) for s in _list(raw.get("sources", []), f"Сайты «{name}»", 100)],
            "keywords": _list(raw.get("keywords", []), f"Ключевые слова «{name}»", 40, lower=True),
            "types": types,
            "target": _text(raw.get("target"), f"Папка сектора «{name}»", 260),
        })
    return result


def clean(payload: dict) -> tuple[dict, list[dict]]:
    """Проверяет присланное окном: (значения «секция.ключ», секторы). Ошибка — понятным текстом."""
    ai = payload.get("ai") or {}
    protect = payload.get("protect") or {}
    night = payload.get("night") or {}
    model = _text(ai.get("model"), "Модель", 80)
    if not _MODEL.match(model):
        raise SettingsError("Модель: название вроде qwen2.5:7b.")
    try:
        confidence = int(ai.get("min_confidence", 80))
    except (TypeError, ValueError) as exc:
        raise SettingsError("Уверенность ИИ: нужно число от 0 до 100.") from exc
    if not 0 <= confidence <= 100:
        raise SettingsError("Уверенность ИИ: нужно число от 0 до 100.")
    after = str(night.get("after", "nothing"))
    if after not in AFTER:
        raise SettingsError("«В конце»: можно ничего, усыпить или выключить.")
    values = {
        "ai.enabled": bool(ai.get("enabled")),
        "ai.model": model,
        "ai.min_confidence": confidence,
        "ai.about": _text(ai.get("about"), "О тебе", 1000),
        "protect.keep_keywords": _list(protect.get("keep_keywords", []), "Не трогать — слова", 40, lower=True),
        "protect.paths": _list(protect.get("paths", []), "Не трогать — папки и файлы", 260),
        "protect.name_keywords": _list(protect.get("name_keywords", []), "Не удалять — слова", 40, lower=True),
        "night.after": after,
        "night.auto_delete": bool(night.get("auto_delete")),
        "night.drives": bool(night.get("drives")),
        "night.sort_folders": _list(night.get("sort_folders", []), "Какие папки раскладывать", 260),
    }
    return values, _sectors(payload.get("sectors", []))


def save(payload: dict) -> None:
    """Записывает настройки в твой rules.toml: меняются только эти строки, пояснения остаются."""
    values, sectors = clean(payload)
    path = ensure_user_rules()
    text = path.read_text(encoding="utf-8-sig")
    for dotted, value in values.items():
        text = rules_edit.set_value(text, dotted, value)
    text = rules_edit.replace_sectors(text, sectors)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        Rules.load(tmp)  # файл должен читаться и проходить проверку — иначе старый остаётся как был
    except RulesError as exc:
        tmp.unlink(missing_ok=True)
        raise SettingsError(str(exc)) from exc
    os.replace(tmp, path)
