"""Ключи лицензий File Cleaner — для продавца.

    python packaging/keygen.py init                  # один раз: пара ключей
    python packaging/keygen.py issue "Имя Фамилия"   # ключ для покупателя (или адрес почты)
    python packaging/keygen.py batch 100             # 100 ключей «License #0001…» списком для магазина

Закрытый ключ лежит вне репозитория: %USERPROFILE%\\.filecleaner-seller\\license-signing.key
(или путь из FILECLEANER_SELLER_KEY). Сохрани его копию в надёжном месте: без него новые ключи для уже
выпущенных версий программы не сделать, а попади он к чужим — они смогут выпускать ключи сами.
Открытый ключ init сам вписывает в filecleaner/licensing.py. Выданные ключи записываются в issued.csv
рядом с закрытым — чтобы переслать покупателю, если он потеряет письмо.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from filecleaner import ed25519, licensing  # noqa: E402

KEY_FILE = Path(os.environ.get("FILECLEANER_SELLER_KEY") or Path.home() / ".filecleaner-seller" / "license-signing.key")
ISSUED = KEY_FILE.parent / "issued.csv"
MODULE = ROOT / "filecleaner" / "licensing.py"


def init() -> int:
    if KEY_FILE.exists():
        sys.exit(f"Закрытый ключ уже есть: {KEY_FILE}\nНовый сделал бы недействительными все выданные ключи.")
    secret = secrets.token_bytes(32)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(secret.hex() + "\n", encoding="ascii")
    public = ed25519.public_key(secret).hex()
    text = MODULE.read_text(encoding="utf-8")
    text, count = re.subn(r'PUBLIC_KEY = bytes\.fromhex\("[0-9a-f]{64}"\)', f'PUBLIC_KEY = bytes.fromhex("{public}")', text)
    if count != 1:
        sys.exit(f"Не нашёл PUBLIC_KEY в {MODULE} — впиши вручную: {public}")
    MODULE.write_text(text, encoding="utf-8", newline="")
    print(f"Закрытый ключ: {KEY_FILE} — сохрани копию в надёжном месте и никому не показывай.")
    print(f"Открытый ключ вписан в {MODULE.relative_to(ROOT)}: {public}")
    return 0


def secret_key() -> bytes:
    try:
        secret = bytes.fromhex(KEY_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        sys.exit(f"Нет закрытого ключа {KEY_FILE} — сначала: python packaging/keygen.py init")
    if ed25519.public_key(secret) != licensing.PUBLIC_KEY:
        sys.exit("Открытый ключ в filecleaner/licensing.py — не от этого закрытого: такие ключи программа не примет.")
    return secret


def issue_keys(names: list[str]) -> list[str]:
    secret = secret_key()
    keys = []
    with open(ISSUED, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for name in names:
            key = licensing.issue(secret, name)
            writer.writerow([dt.date.today().isoformat(), licensing.parse(key).name, key])  # проверка тем же кодом
            keys.append(key)
    return keys


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    command, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if command == "init" and not rest:
        return init()
    if command == "issue" and len(rest) == 1:
        print(issue_keys(rest)[0])
        return 0
    if command == "batch" and len(rest) == 1 and rest[0].isdigit():
        done = sum(1 for _ in open(ISSUED, encoding="utf-8")) if ISSUED.exists() else 0
        keys = issue_keys([f"License #{done + i:04d}" for i in range(1, int(rest[0]) + 1)])
        out = KEY_FILE.parent / f"batch-{dt.datetime.now():%Y%m%d-%H%M%S}.txt"
        out.write_text("\n".join(keys) + "\n", encoding="utf-8")
        print(f"{len(keys)} ключей: {out}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
