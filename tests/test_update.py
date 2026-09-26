"""Проверка обновлений: только номер версии с GitHub, не чаще раза в день; в тестах GitHub поддельный."""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from filecleaner import update


class FakeGitHub:
    """Отвечает на /releases/latest тем, что лежит в release, с кодом status."""

    def __init__(self) -> None:
        self.release = {"tag_name": "v9.0.0", "html_url": update.PAGE + "tag/v9.0.0", "body": "Что нового"}
        self.status = 200
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                fake.requests.append(dict(self.headers))
                body = json.dumps(fake.release).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/repos/x/releases/latest"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def github(sandbox, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(update, "API_URL", fake.url)
    monkeypatch.setattr(update, "_OPENER", urllib.request.build_opener(urllib.request.ProxyHandler({})))
    yield fake
    fake.stop()


def test_versions_compare_as_numbers():
    assert update.is_newer("0.2.0", "0.1.0") and update.is_newer("v1.10", "1.9.5")
    assert not update.is_newer("0.1", "0.1.0") and not update.is_newer("0.1.0", "0.2.0")
    assert not update.is_newer("latest", "0.1.0") and not update.is_newer("", "0.1.0")


def test_new_version_found_once_a_day(github, rules):
    latest = update.check(rules)
    assert latest == {"version": "9.0.0", "tag": "v9.0.0", "url": update.PAGE + "tag/v9.0.0", "notes": "Что нового"}
    assert github.requests[0]["User-Agent"].startswith("FileCleaner/")     # о тебе — ничего, только версия
    assert update.check(rules) == latest and len(github.requests) == 1        # второй раз за день — без сети
    assert update.check(rules, force=True) == latest and len(github.requests) == 2   # «Проверить сейчас»
    assert update.known() == latest                                            # консоль и окно — без сети


def test_only_our_release_page_is_opened(github, rules):
    github.release["html_url"] = "https://example.com/download.exe"
    assert update.check(rules)["url"] == update.PAGE + "latest"


def test_no_release_older_release_disabled_offline(github, rules, monkeypatch):
    github.status = 404
    assert update.check(rules, force=True) is None                  # выпусков ещё нет
    github.status = 200
    github.release["tag_name"] = "0.0.1"
    assert update.check(rules, force=True) is None                  # у тебя новее

    rules.data["update"]["check"] = False
    monkeypatch.setattr(update, "EVERY", 0)
    assert update.check(rules) is None and len(github.requests) == 2    # выключено — в сеть не ходим

    rules.data["update"]["check"] = True
    github.release["tag_name"] = "v9.0.0"
    update.check(rules, force=True)
    github.stop()                                                   # интернета нет
    assert update.check(rules)["version"] == "9.0.0"                # что знали — то и показываем
    with pytest.raises(update.Unreachable):
        update.check(rules, force=True)                             # сам нажал «Проверить» — честно говорим


def test_broken_or_future_check_time_does_not_stop_checks(github, rules):
    import time

    from filecleaner import config

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for checked in ([1], "вчера", time.time() + 10 * 86400):   # мусор в файле или часы ушли вперёд
        (config.DATA_DIR / "update.json").write_text(json.dumps({"checked": checked}), encoding="utf-8")
        assert update.check(rules)["version"] == "9.0.0"
    assert len(github.requests) == 3
