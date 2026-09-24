"""Модуль ИИ на поддельном сервере Ollama (настоящая модель для тестов не нужна)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import write

from filecleaner import organizer
from filecleaner.ai import LocalAI, text_snippet


class FakeOllama(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *args):
        pass

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply({"models": [{"name": "qwen2.5:7b"}]})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append(request)
        content = request["prompt"].split("Beginning of the content:", 1)[1]
        folder = "Работа" if "BACnet" in content else ""
        self._reply({"response": json.dumps({"folder": folder, "why": "про автоматизацию зданий"})})


@pytest.fixture
def ollama():
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeOllama.calls = []
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_ai_sorts_what_rules_missed(sandbox, rules, ollama):
    rules.data["ai"].update(enabled=True, url=ollama)
    dl = sandbox / "Downloads"
    write(dl / "notes_q3.txt", "BACnet objects and PX controllers overview")
    write(dl / "shopping.txt", "milk, bread")

    plan = organizer.plan_sort(dl, rules, check_references=False)
    where = {m.src.name: m for m in plan.moves}
    assert where["notes_q3.txt"].label == "Работа / Документы"
    assert where["notes_q3.txt"].reason.startswith("ИИ:")
    assert where["shopping.txt"].label == "Документы"          # модель не уверена — обычная сортировка по типу
    assert len(FakeOllama.calls) == 2

    organizer.plan_sort(dl, rules, check_references=False)
    assert len(FakeOllama.calls) == 2                          # ответы запомнены — модель не спрашивается снова


def test_ai_off_or_unreachable(sandbox, rules):
    rules.data["ai"].update(enabled=True, url="http://127.0.0.1:9")
    assert not LocalAI(rules).available()
    rules.data["ai"]["enabled"] = False
    assert not LocalAI(rules).available()


def test_text_snippet_from_docx(tmp_path):
    import zipfile

    doc = tmp_path / "a.docx"
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:t>Отчёт по лабораторной</w:t></w:document>")
    assert text_snippet(doc) == "Отчёт по лабораторной"


def test_prompt_has_descriptions_and_readable_names(sandbox, rules, ollama):
    rules.data["ai"].update(enabled=True, url=ollama)
    write(sandbox / "Downloads" / "%D0%A2%D0%97.docx", b"not a real docx")
    organizer.plan_sort(sandbox / "Downloads", rules, check_references=False)
    [call] = FakeOllama.calls
    assert "File name: ТЗ.docx" in call["prompt"]
    assert "автоматизация зданий" in call["prompt"]            # описание сектора «Работа»


def test_failed_answer_is_not_cached(sandbox, rules):
    rules.data["ai"].update(enabled=True, url="http://127.0.0.1:9")
    ai = LocalAI(rules)
    sector, _ = ai.classify("x.pdf", rules.sectors, [], "", "key")
    assert sector is None and "key" not in ai._cache
