"""Правка rules.toml из окна программы: меняются только нужные строки, пояснения остаются.

Записи TOML в стандартной библиотеке нет, а пересоздавать файл целиком нельзя — в нём комментарии
к каждой настройке. Поэтому правим точечно: значение ключа в своей секции и блок секторов [[sector]].
"""
from __future__ import annotations

import json
import re

KEY = re.compile(r"^(\s*)([A-Za-z0-9_\-]+)(\s*=\s*)")


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # строка JSON — это и строка TOML
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"Не умею записать в правила: {value!r}")


def _scan(text: str) -> tuple[int, int]:
    """(баланс скобок [ ], начало комментария или -1) — вне строк."""
    depth, quote, escape = 0, "", False
    for i, ch in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif ch == "\\" and quote == '"':
                escape = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return depth, i
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth, -1


def _header(line: str) -> str | None:
    """«scan» для [scan], «[[sector]]» для [[sector]]; None — не заголовок."""
    s = line.strip()
    if s.startswith("[[") and "]]" in s:
        return "[[" + s[2:s.index("]]")].strip() + "]]"
    if s.startswith("[") and "]" in s:
        return s[1:s.index("]")].strip()
    return None


def _statements(lines: list[str]) -> list[tuple[int, int, str, str | None]]:
    """(первая строка, последняя строка, секция, ключ или None для заголовка)."""
    out: list[tuple[int, int, str, str | None]] = []
    section, i = "", 0
    while i < len(lines):
        match = KEY.match(lines[i])
        if match:
            end = i
            depth = _scan(lines[i][match.end():])[0]
            while depth > 0 and end + 1 < len(lines):  # многострочный массив
                end += 1
                depth += _scan(lines[end])[0]
            out.append((i, end, section, match.group(2)))
            i = end + 1
            continue
        header = _header(lines[i])
        if header is not None:
            section = header
            out.append((i, i, section, None))
        i += 1
    return out


def _split(text: str) -> tuple[list[str], str]:
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n").split("\n"), newline


def set_value(text: str, dotted: str, value) -> str:
    """Ставит значение ключа «секция.ключ»; хвостовой комментарий однострочного значения сохраняется."""
    section, _, key = dotted.rpartition(".")
    lines, newline = _split(text)
    statements = _statements(lines)
    rendered = toml_value(value)
    for start, end, sec, k in statements:
        if sec != section or k != key:
            continue
        match = KEY.match(lines[start])
        prefix = lines[start][:match.end()]
        new = prefix + rendered
        if start == end:
            rest = lines[start][match.end():]
            comment_at = _scan(rest)[1]
            if comment_at >= 0:  # «значение      # пояснение» — пояснение и отступ до него остаются
                new += " " * max(1, comment_at - len(rendered)) + rest[comment_at:]
        lines[start:end + 1] = [new]
        return newline.join(lines)
    # Ключа нет — дописываем в конец его секции (или новой секцией в конец файла).
    in_section = [s for s in statements if s[2] == section]
    if in_section:
        last = in_section[-1][1]
        lines.insert(last + 1, f"{key} = {rendered}")
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"[{section}]", f"{key} = {rendered}", ""]
    return newline.join(lines)


def sector_block(sector: dict) -> list[str]:
    lines = ["[[sector]]"]
    for key in ("name", "description", "sources", "keywords", "types", "target"):
        value = sector.get(key)
        if value in (None, "", []):
            continue
        lines.append(f"{key} = {toml_value(value)}")
    return lines


def replace_sectors(text: str, sectors: list[dict]) -> str:
    """Заменяет все блоки [[sector]] новыми; пояснения до и после блока остаются на месте."""
    lines, newline = _split(text)
    blocks = [i for i, line in enumerate(lines) if _header(line) == "[[sector]]"]
    new = []
    for sector in sectors:
        new += sector_block(sector) + [""]
    if not blocks:
        target = next((i for i, line in enumerate(lines) if _header(line) == "links"), len(lines))
        lines[target:target] = new
        return newline.join(lines)
    start = blocks[0]
    end = next((i for i in range(blocks[-1] + 1, len(lines))
                if _header(lines[i]) not in (None, "[[sector]]")), len(lines))
    # Комментарии прямо перед следующей секцией (отделённые пустой строкой) — её заголовок, не наши.
    cut = end
    while cut > start and lines[cut - 1].lstrip().startswith("#"):
        cut -= 1
    if cut < end and cut > start and not lines[cut - 1].strip():
        end = cut
    lines[start:end] = new
    return newline.join(lines)
