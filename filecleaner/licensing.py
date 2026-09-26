"""Лицензия: 30 дней пробного периода, потом — ключ.

Ключ — имя покупателя и дата, подписанные закрытым ключом продавца (Ed25519). Программа проверяет
подпись открытым ключом у себя, без интернета. Выпускает ключи packaging/keygen.py.

После пробного периода без ключа по-прежнему можно смотреть, что будет сделано, историю,
отменять и возвращать файлы на место — не работает только то, что чистит, раскладывает и удаляет.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import re
import struct
from dataclasses import dataclass

from . import config, ed25519
from .i18n import tr

# Открытый ключ продавца (закрытый — у продавца, в программу и в репозиторий не попадает).
PUBLIC_KEY = bytes.fromhex("8a2294401a483b533ec94714743564fd45892193d290453b614a619ee71b9fef")
BUY_URL = ""        # страница магазина (Gumroad, Lemon Squeezy…) — кнопка «Купить ключ» появится, когда задана
TRIAL_DAYS = 30
PREFIX = "FC1"
_CONTEXT = b"File Cleaner license v1\n"   # подпись годится только для ключей этой программы
_EPOCH = dt.date(2026, 1, 1)
_FORMAT = 1


class LicenseError(ValueError):
    pass


@dataclass(frozen=True)
class License:
    name: str
    issued: dt.date
    edition: int
    key: str            # в каноническом виде: FC1-XXXXX-XXXXX-…


def _encode(raw: bytes) -> str:
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    return "-".join([PREFIX] + [text[i:i + 5] for i in range(0, len(text), 5)])


def issue(secret: bytes, name: str, issued: dt.date | None = None, edition: int = 1) -> str:
    """Ключ для покупателя (нужен закрытый ключ продавца)."""
    data = " ".join(name.split()).encode("utf-8")
    if not data or len(data) > 60:
        raise ValueError("имя покупателя — от 1 до 60 байт")
    days = ((issued or dt.date.today()) - _EPOCH).days
    payload = struct.pack(">BBH", _FORMAT, edition, days) + data
    return _encode(payload + ed25519.sign(secret, _CONTEXT + payload))


def parse(key: str) -> License:
    """Проверяет ключ; ошибка — понятным текстом."""
    text = re.sub(r"[\s\-]", "", str(key)).upper()
    if not text.startswith(PREFIX):
        raise LicenseError(tr("Это не ключ File Cleaner: он начинается с FC1-."))
    text = text[len(PREFIX):]
    try:
        raw = base64.b32decode(text + "=" * (-len(text) % 8))
    except (binascii.Error, ValueError) as exc:
        raise LicenseError(tr("Ключ повреждён — скопируй его из письма целиком.")) from exc
    payload, signature = raw[:-64], raw[-64:]
    if len(payload) < 5 or not ed25519.verify(PUBLIC_KEY, _CONTEXT + payload, signature):
        raise LicenseError(tr("Ключ не подходит — скопируй его из письма целиком."))
    fmt, edition, days = struct.unpack(">BBH", payload[:4])
    if fmt != _FORMAT:
        raise LicenseError(tr("Этот ключ — для другой версии программы. Обнови File Cleaner."))
    return License(payload[4:].decode("utf-8", "replace"), _EPOCH + dt.timedelta(days=days), edition, _encode(raw))


# ------------------------------------------------------------------ состояние
def _path():
    return config.DATA_DIR / "license.json"


def _load() -> dict:
    try:
        state = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save(state: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def status(today: dt.date | None = None) -> dict:
    """{"state": "licensed" | "trial" | "expired", "name", "days_left", "buy_url"}; первый вызов начинает пробный период."""
    today = today or dt.date.today()
    state = _load()
    if state.get("key"):
        try:
            found = parse(state["key"])
            return {"state": "licensed", "name": found.name, "days_left": None, "buy_url": BUY_URL}
        except LicenseError:
            pass  # испорченный ключ — как будто его нет
    try:
        started = dt.date.fromisoformat(str(state.get("trial_started")))
    except ValueError:
        started = today
        try:
            _save({**state, "trial_started": today.isoformat()})
        except OSError:
            pass
    left = min(TRIAL_DAYS, TRIAL_DAYS - (today - started).days)
    return {"state": "trial" if left > 0 else "expired", "name": None, "days_left": max(0, left), "buy_url": BUY_URL}


def activate(key: str) -> dict:
    found = parse(key)
    _save({**_load(), "key": found.key})
    return status()


def require() -> None:
    """Чистить, раскладывать и удалять можно в пробный период и с ключом."""
    if status()["state"] == "expired":
        raise LicenseError(tr("Пробный период закончился. Смотреть, что будет сделано, можно и дальше, "
                              "а чтобы чистить, раскладывать и удалять, нужен ключ (Настройки → Лицензия)."))
