"""Язык программы. Фразы пишутся по-русски и проходят через tr(); для английского берётся перевод.

    tr("не менялся {days} дн.", days=5)  →  «не менялся 5 дн.» или «unchanged for 5 days»

Имена типов («Документы») — это и идентификаторы в правилах, и имена папок. Идентификаторы не меняются,
а имена папок и подписи — через type_name(): «Документы» или «Documents».
"""
from __future__ import annotations

import ctypes
import re

LANGUAGES = {"ru": "Русский", "en": "English"}
_RU_FAMILY = {0x19, 0x22, 0x23, 0x3F}  # Windows на русском, украинском, белорусском, казахском
_current = "ru"


def detect() -> str:
    """Язык по языку интерфейса Windows."""
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return "ru"
    return "ru" if (langid & 0x3FF) in _RU_FAMILY else "en"


def resolve(setting: object) -> str:
    """«auto» (или ничего) — по Windows; иначе ru или en."""
    value = str(setting or "auto").lower()
    return value if value in LANGUAGES else detect()


def set_language(lang: str) -> None:
    global _current
    _current = lang if lang in LANGUAGES else "ru"


def language() -> str:
    return _current


def _catalog() -> dict[str, str]:
    from .i18n_en import EN
    return EN


def tr(text: str, /, **params: object) -> str:
    """Фраза на языке программы; {имена} подставляются из params."""
    if _current == "en":
        text = _catalog().get(text, text)
    return text.format(**params) if params else text


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_patterns: list[tuple[re.Pattern[str], str]] | None = None


def _templates() -> list[tuple[re.Pattern[str], str]]:
    """Фразы с подстановками как образцы: «рядом папка «{folder}» — …» → регулярное выражение."""
    global _patterns
    if _patterns is None:
        items = []
        for ru, en in _catalog().items():
            pieces = re.split(r"(\{\w+\})", ru)
            if len(pieces) == 1:
                continue
            seen: set[str] = set()
            regex = []
            for piece in pieces:
                name = piece[1:-1] if re.fullmatch(r"\{\w+\}", piece) else None
                if name is None:
                    regex.append(re.escape(piece))
                elif name in seen:
                    regex.append(f"(?P={name})")
                else:
                    seen.add(name)
                    regex.append(f"(?P<{name}>.+?)")
            literal = len(re.sub(r"\{\w+\}", "", ru))
            items.append((literal, re.compile("".join(regex), re.S), en))
        _patterns = [(pattern, en) for _, pattern, en in sorted(items, key=lambda x: -x[0])]
    return _patterns


def retranslate(text: str) -> str:
    """Текст, сохранённый раньше по-русски (причина в партии, название операции), — на язык программы."""
    if _current != "en" or not text or not _CYRILLIC.search(text):
        return text
    catalog = _catalog()
    if text in catalog:
        return catalog[text]
    for pattern, english in _templates():
        match = pattern.fullmatch(text)
        if match:
            return english.format(**{k: _retranslate_value(v) for k, v in match.groupdict().items()})
    return text


def _retranslate_value(value: str) -> str:
    """Подставленное значение: «3 объекта» → «3 objects»; вложенная фраза — тоже переводится."""
    from .i18n_en import PLURAL_FORMS, PLURALS
    match = re.fullmatch(r"([\d\s]+) (\w+)", value)
    if match and match.group(2) in PLURAL_FORMS:
        number = int(match.group(1).replace(" ", ""))
        singular, many = PLURALS[PLURAL_FORMS[match.group(2)]]
        return f"{match.group(1)} {singular if number == 1 else many}"
    return retranslate(value)


def plural_words(one: str, few: str, many: str) -> tuple[str, ...]:
    """Формы слова для числа: русские три или английские две (единственное, множественное)."""
    if _current == "en":
        from .i18n_en import PLURALS
        if one in PLURALS:
            return PLURALS[one]
    return one, few, many


def type_name(kind: str | None) -> str:
    """Имя папки и подпись для типа файлов («Документы» → «Documents»); None — «Прочее»."""
    from . import config
    kind = kind or config.OTHER_TYPE
    if _current == "en":
        from .i18n_en import TYPE_NAMES
        return TYPE_NAMES.get(kind, kind)
    return kind


def all_type_names() -> set[str]:
    """Все имена папок типов на всех языках — чтобы сами эти папки не раскладывались."""
    from . import config
    from .i18n_en import TYPE_NAMES
    names = set(config.TYPES) | {config.OTHER_TYPE}
    return {n.lower() for n in names | set(TYPE_NAMES.values())}
