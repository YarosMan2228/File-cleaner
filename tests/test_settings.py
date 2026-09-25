"""Настройки из окна: точечная правка rules.toml — пояснения остаются, ошибка не портит файл."""
import tomllib

import pytest

from filecleaner import rules_edit, settings
from filecleaner.rules import Rules, user_rules_path

TEXT = """[ai]
enabled = false
min_confidence = 80       # насколько уверена;
                          # продолжение пояснения
about = ""

[protect]
paths = []
name_keywords = [
    "паспорт", "passport",
    "договор",  # и ещё
]

# ─── Сортировка ───
[sort]
folders = ["downloads"]

# Секторы — пояснение.

[[sector]]
name = "Учёба"
keywords = ["rtu"]

[[sector]]
name = "Виртуалки"
types = ["Образы дисков"]
# target = "B:/Виртуалки"

# ─── Ссылки ───
[links]
update_office_recent = true
"""


def test_value_changes_keep_comments_and_alignment():
    text = rules_edit.set_value(TEXT, "ai.min_confidence", 75)
    assert "min_confidence = 75       # насколько уверена;\n                          # продолжение пояснения" in text
    text = rules_edit.set_value(text, "protect.name_keywords", ["x", "y"])
    assert 'name_keywords = ["x", "y"]\n\n# ─── Сортировка ───' in text
    text = rules_edit.set_value(text, "protect.keep_keywords", ["pw2"])      # ключа не было — в свою секцию
    text = rules_edit.set_value(text, "night.after", "sleep")                # секции не было — в конец
    text = rules_edit.set_value(text, "ai.about", 'Он сказал "да" \\ C:\\путь')
    data = tomllib.loads(text)
    assert data["ai"]["min_confidence"] == 75 and data["ai"]["about"] == 'Он сказал "да" \\ C:\\путь'
    assert data["protect"] == {"paths": [], "name_keywords": ["x", "y"], "keep_keywords": ["pw2"]}
    assert data["night"]["after"] == "sleep" and data["sort"] == {"folders": ["downloads"]}


def test_sectors_are_replaced_between_their_comments():
    new = [{"name": "Работа", "description": "автоматизация", "keywords": ["dkn"], "sources": [], "types": []}]
    text = rules_edit.replace_sectors(TEXT, new)
    assert "# Секторы — пояснение.\n\n[[sector]]\nname = \"Работа\"" in text
    assert "# ─── Ссылки ───\n[links]" in text
    data = tomllib.loads(text)
    assert data["sector"] == [{"name": "Работа", "description": "автоматизация", "keywords": ["dkn"]}]
    assert data["links"] == {"update_office_recent": True}


def test_save_writes_user_rules_and_refuses_bad_input(sandbox):
    current = settings.read(Rules.load())
    current["ai"]["min_confidence"] = 85
    current["protect"]["keep_keywords"] = ["pw2", "PW2", " "]                  # повтор и пустое — убираются
    current["sectors"].append({"name": "Хобби", "description": "музыка и фото", "keywords": ["Guitar"],
                               "sources": ["https://www.example.com/page"], "types": [], "target": ""})
    settings.save(current)

    rules = Rules.load()
    assert rules.get("ai.min_confidence") == 85 and rules.get("protect.keep_keywords") == ["pw2"]
    hobby = rules.sectors[-1]
    assert hobby["name"] == "Хобби" and hobby["keywords"] == ["guitar"] and hobby["sources"] == ["example.com"]
    saved = user_rules_path().read_text(encoding="utf-8")
    assert "# ─── Сортировка" in saved and "Режимы:" in saved                     # пояснения по умолчанию на месте

    for bad in ({"name": "Документы"}, {"name": "a/b"}, {"name": "Хобби"}, {"name": ""}):
        broken = settings.read(Rules.load())
        broken["sectors"].append({"description": "", "keywords": [], "sources": [], "types": [], **bad})
        with pytest.raises(settings.SettingsError):
            settings.save(broken)
    assert user_rules_path().read_text(encoding="utf-8") == saved                # файл не тронут
