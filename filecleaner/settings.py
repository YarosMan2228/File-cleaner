"""Настройки для окна программы: что можно менять кнопками и как это записать в rules.toml."""
from __future__ import annotations

import os
import re

from . import config, i18n, rules_edit
from .i18n import tr
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
        "ui": {"language": str(rules.get("ui.language", "auto"))},
        "update": {"check": bool(rules.get("update.check", True))},
        "languages": {"auto": tr("Как в Windows"), **i18n.LANGUAGES},
        "types": [{"id": kind, "name": i18n.type_name(kind)} for kind in config.TYPES],
        "file": str(user_rules_path()),
    }


def _list(value, what: str, limit: int = 80, lower: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise SettingsError(tr("{what}: нужен список.", what=what))
    seen: dict[str, str] = {}
    for item in value:
        text = str(item).strip()
        if lower:
            text = text.lower()
        if not text:
            continue
        if len(text) > limit:
            raise SettingsError(tr("{what}: «{text}…» — слишком длинно.", what=what, text=text[:30]))
        seen.setdefault(text.lower(), text)
    return list(seen.values())


def _text(value, what: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise SettingsError(tr("{what}: слишком длинно (больше {limit} знаков).", what=what, limit=limit))
    return text


def _domain(value: str) -> str:
    text = re.sub(r"^https?://", "", value.strip().lower()).removeprefix("www.").split("/", 1)[0]
    if not _DOMAIN.match(text):
        raise SettingsError(tr("Сайт «{value}» — не похоже на адрес сайта (нужно вроде coursera.org).", value=value))
    return text


def _sectors(value) -> list[dict]:
    if not isinstance(value, list):
        raise SettingsError(tr("Секторы: нужен список."))
    reserved = i18n.all_type_names() | {config.REVIEW_DIR_NAME.lower(), config.RETURN_DIR_NAME.lower(), "_return"}
    result, names = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise SettingsError(tr("Сектор записан неправильно."))
        name = _text(raw.get("name"), tr("Название сектора"), 60)
        if not name:
            raise SettingsError(tr("У сектора должно быть название — это имя папки."))
        if _BAD_NAME.search(name) or name.strip(". ") != name:
            raise SettingsError(tr("«{name}»: в названии папки нельзя использовать < > : \" / \\ | ? * и точку в конце.", name=name))
        if name.lower() in reserved:
            raise SettingsError(tr("«{name}» — так уже называется папка типа файлов; выбери другое название.", name=name))
        if name.lower() in names:
            raise SettingsError(tr("Сектор «{name}» указан дважды.", name=name))
        names.add(name.lower())
        types = _list(raw.get("types", []), tr("Типы файлов «{name}»", name=name))
        unknown = [t for t in types if t not in config.TYPES]
        if unknown:
            raise SettingsError(tr("Типы файлов «{name}»: не знаю тип «{kind}».", name=name, kind=unknown[0]))
        result.append({
            "name": name,
            "description": _text(raw.get("description"), tr("Описание «{name}»", name=name), 600),
            "sources": [_domain(s) for s in _list(raw.get("sources", []), tr("Сайты «{name}»", name=name), 100)],
            "keywords": _list(raw.get("keywords", []), tr("Ключевые слова «{name}»", name=name), 40, lower=True),
            "types": types,
            "target": _text(raw.get("target"), tr("Папка сектора «{name}»", name=name), 260),
        })
    return result


def clean(payload: dict) -> tuple[dict, list[dict]]:
    """Проверяет присланное окном: (значения «секция.ключ», секторы). Ошибка — понятным текстом."""
    ai = payload.get("ai") or {}
    protect = payload.get("protect") or {}
    night = payload.get("night") or {}
    model = _text(ai.get("model"), tr("Модель"), 80)
    if not _MODEL.match(model):
        raise SettingsError(tr("Модель: название вроде qwen2.5:7b."))
    try:
        confidence = int(ai.get("min_confidence", 80))
    except (TypeError, ValueError) as exc:
        raise SettingsError(tr("Уверенность ИИ: нужно число от 0 до 100.")) from exc
    if not 0 <= confidence <= 100:
        raise SettingsError(tr("Уверенность ИИ: нужно число от 0 до 100."))
    after = str(night.get("after", "nothing"))
    language = str((payload.get("ui") or {}).get("language", "auto"))
    if language != "auto" and language not in i18n.LANGUAGES:
        raise SettingsError(tr("Язык: можно «как в Windows», русский или английский."))
    if after not in AFTER:
        raise SettingsError(tr("«В конце»: можно ничего, усыпить или выключить."))
    values = {
        "ai.enabled": bool(ai.get("enabled")),
        "ai.model": model,
        "ai.min_confidence": confidence,
        "ai.about": _text(ai.get("about"), tr("О тебе"), 1000),
        "protect.keep_keywords": _list(protect.get("keep_keywords", []), tr("Не трогать — слова"), 40, lower=True),
        "protect.paths": _list(protect.get("paths", []), tr("Не трогать — папки и файлы"), 260),
        "protect.name_keywords": _list(protect.get("name_keywords", []), tr("Не удалять — слова"), 40, lower=True),
        "night.after": after,
        "night.auto_delete": bool(night.get("auto_delete")),
        "night.drives": bool(night.get("drives")),
        "ui.language": language,
        "update.check": bool((payload.get("update") or {}).get("check", True)),
        "night.sort_folders": _list(night.get("sort_folders", []), tr("Какие папки раскладывать"), 260),
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
