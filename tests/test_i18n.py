"""Английский язык: у каждой фразы есть перевод, и английское окно не показывает русских букв."""
import ast
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from conftest import write

from filecleaner import i18n, journal, organizer, review
from filecleaner.analyzers import Finding
from filecleaner.i18n_en import EN, PLURALS

ROOT = Path(__file__).resolve().parents[1] / "filecleaner"
STATIC = ROOT / "gui" / "static"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
PLACEHOLDER_PY = re.compile(r"\{(\w+)\}")
PLACEHOLDER_JS = re.compile(r"\{(\d+)\}")


def python_phrases() -> tuple[set[str], set[str]]:
    """(фразы из tr("…"), русские формы «один» из plural(n, "…", …)) во всём коде программы."""
    phrases, plurals = set(), set()
    for path in ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args):
                continue
            if node.func.id == "tr" and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                phrases.add(node.args[0].value)
            if node.func.id == "plural" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                plurals.add(node.args[1].value)
    return phrases, plurals


def test_every_python_phrase_has_english():
    phrases, plurals = python_phrases()
    assert len(phrases) > 200
    missing = sorted(p for p in phrases if p not in EN)
    assert not missing, f"нет перевода: {missing[:5]}"
    for ru in phrases:
        assert set(PLACEHOLDER_PY.findall(ru)) == set(PLACEHOLDER_PY.findall(EN[ru])), ru
        assert not CYRILLIC.search(EN[ru]), ru
    assert plurals <= set(PLURALS), plurals - set(PLURALS)


def _js_strings(source: str, start: int) -> tuple[str, int]:
    """Строка из литералов '…' + '…' начиная с start; возвращает (текст, конец)."""
    parts, i = [], start
    literal = re.compile(r"\s*'((?:[^'\\]|\\.)*)'\s*")
    while True:
        match = literal.match(source, i)
        if not match:
            return "".join(parts), i
        parts.append(re.sub(r"\\(.)", r"\1", match.group(1)))
        i = match.end()
        if source.startswith("+", i):
            i += 1
        else:
            return "".join(parts), i


def window_phrases() -> set[str]:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    phrases = {_js_strings(source, m.end())[0] for m in re.finditer(r"\bt\(", source)}

    class Html(HTMLParser):
        def handle_starttag(self, tag, attrs):
            phrases.update(v for k, v in attrs if k in ("placeholder", "aria-label", "title") and v and CYRILLIC.search(v))

        def handle_data(self, data):
            text = re.sub(r"\s+", " ", data).strip()
            if CYRILLIC.search(text):
                phrases.add(text)

    Html().feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return {p for p in phrases if p}


def window_dictionary() -> dict[str, str]:
    source = (STATIC / "i18n.js").read_text(encoding="utf-8")
    body = source[source.index("window.I18N_EN"):]
    pairs = re.findall(r"^\s*'((?:[^'\\]|\\.)*)':\s*\n?\s*'((?:[^'\\]|\\.)*)',", body, re.M)
    unescape = lambda s: re.sub(r"\\(.)", r"\1", s)  # noqa: E731
    return {unescape(k): unescape(v) for k, v in pairs}


def test_every_window_phrase_has_english():
    phrases, dictionary = window_phrases(), window_dictionary()
    assert len(phrases) > 150
    missing = sorted(p for p in phrases if p not in dictionary)
    assert not missing, f"нет перевода в i18n.js: {missing[:5]}"
    for ru in phrases:
        assert set(PLACEHOLDER_JS.findall(ru)) == set(PLACEHOLDER_JS.findall(dictionary[ru])), ru
        assert not CYRILLIC.search(dictionary[ru]), ru


# ======================================================================= английское окно
def russian_left(data, skip: set[str] = frozenset()) -> list[str]:
    """Строки с русскими буквами в ответе (кроме пользовательских данных в полях skip)."""
    found = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key not in skip:
                found += russian_left(value, skip)
    elif isinstance(data, list):
        for value in data:
            found += russian_left(value, skip)
    elif isinstance(data, str) and CYRILLIC.search(data):
        found.append(data)
    return found


def test_english_window_speaks_english(sandbox, rules, monkeypatch):
    from test_gui import call, wait_task

    from filecleaner import config
    from filecleaner.gui.app import App, Server

    rules.data["ui"]["language"] = "en"
    rules.data["night"]["drives"] = False
    folders = {name.lower(): sandbox / name for name in ("Downloads", "Documents", "Desktop")}
    monkeypatch.setattr(config, "user_folders", lambda: folders)
    dl = sandbox / "Downloads"
    write(dl / "report.pdf", b"r" * 5000)
    write(dl / "report (1).pdf", b"r" * 5000)
    write(dl / "notes.txt", b"n" * 3000)
    copy = write(dl / "photo (1).jpg", b"p" * 4000)

    server = Server(App(lambda: rules))
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert i18n.language() == "en"
        assert b'<html lang="en">' in call(server, "/")[1]
        with journal.Session("check", "Check") as session:
            review.stage([Finding("duplicates.copy_names", i18n.tr("Дубликаты - имя-копия"), copy, 4000, "review",
                                  i18n.tr("имя похоже на копию, оригинал: {original}", original="photo.jpg"))], session)

        assert not russian_left(call(server, "/api/status")[1])
        assert not russian_left(call(server, "/api/review")[1])
        assert not russian_left(call(server, "/api/history")[1])
        settings = call(server, "/api/settings")[1]
        assert not russian_left(settings, skip={"sectors", "protect", "about", "languages", "id"})

        call(server, "/api/preview", {})
        task = wait_task(server)
        assert task["error"] is None and task["result"]["auto"]["count"] == 1
        assert not russian_left(task), russian_left(task)
    finally:
        server.shutdown()
        server.server_close()

    plan = organizer.plan_sort(dl, rules, check_references=False)
    where = {m.src.name: m for m in plan.moves}
    assert where["notes.txt"].dst.parent.name == "Documents" and where["notes.txt"].reason == "type .txt"
    assert (sandbox / "Ready for approval").is_dir()
    batch = next((sandbox / "Ready for approval").iterdir())
    assert (batch / "REPORT.html").exists() and (batch / "_RETURN").is_dir()
    assert json.loads((batch / ".manifest.json").read_text(encoding="utf-8"))["entries"][0]["group"] == "Duplicates - copy names"


def test_texts_saved_in_russian_are_shown_in_english():
    i18n.set_language("en")
    folder = "рядом папка «ШАК-3 прим 207» — похоже, архив распакован (содержимое не проверял)"
    assert i18n.retranslate(folder) == "folder «ШАК-3 прим 207» is next to it — the archive looks extracted (contents not checked)"
    assert i18n.retranslate("Распакованные архивы") == "Extracted archives"
    assert i18n.retranslate("Ночь: сортировка Downloads") == "Night: sorting Downloads"
    assert i18n.retranslate(r"точно такая же папка: ~\X (3 файла)") == r"an identical folder: ~\X (3 files)"
    assert i18n.retranslate("Мой сектор «Учёба»") == "Мой сектор «Учёба»"      # не из программы — как есть
    i18n.set_language("ru")
    assert i18n.retranslate(folder) == folder
