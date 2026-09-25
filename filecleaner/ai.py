"""Локальная ИИ-модель через Ollama: помогает разложить по секторам то, что не поймали правила.

Всё работает на твоём компьютере — имена и начало текста документов в интернет не уходят.
Включается в правилах: [ai] enabled = true (нужен установленный Ollama и скачанная модель).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import config
from .fsutil import long_path
from .i18n import language
from .rules import Rules

TEXT_EXTS = {"txt", "md", "csv", "tsv", "json", "xml", "html", "htm", "ini", "cfg", "log", "py", "sql"}
OFFICE_PARTS = {
    "docx": ["word/document.xml"],
    "pptx": [f"ppt/slides/slide{i}.xml" for i in range(1, 6)],
    "xlsx": ["xl/sharedStrings.xml"],
}
SNIPPET = 1200
MAX_PDF = 200 * 1024 * 1024
PROMPT_VERSION = 6  # меняется вместе с текстом запроса — старые ответы из кэша не используются
SAVE_EVERY = 20  # ответов модели между записями кэша на диск
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Без системного прокси: запросы к модели не уходят с компьютера, даже если в Windows настроен прокси.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_local_url(url: str) -> bool:
    """Адрес модели на этом компьютере — иначе ИИ не используется: имена и текст файлов не уходят в сеть."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and (host in LOCAL_HOSTS or host.startswith("127."))


def readable_name(name: str) -> str:
    """«%D0%A2%D0%97.docx» (так браузер иногда сохраняет имя) → «ТЗ.docx»."""
    return unquote(name) if re.search(r"%[0-9A-Fa-f]{2}", name) else name


def _pdf_text(path: Path) -> str:
    """Текст первых страниц PDF, если установлен pypdf. У сканов без текстового слоя текста нет."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        if os.path.getsize(long_path(path)) > MAX_PDF:
            return ""
        reader = PdfReader(long_path(path))
        if reader.is_encrypted:
            return ""
        parts: list[str] = []
        for page in reader.pages[:2]:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) >= SNIPPET:
                break
    except Exception:  # pypdf по-разному падает на битых файлах — это не повод останавливать сортировку
        return ""
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:SNIPPET]


def text_snippet(path: Path) -> str:
    """Начало текста документа (txt, docx, pptx, xlsx, pdf) — чтобы модель поняла, о чём он."""
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext == "pdf":
            return _pdf_text(path)
        if ext in TEXT_EXTS:
            with open(long_path(path), "rb") as fh:
                return fh.read(SNIPPET * 2).decode("utf-8", "ignore")[:SNIPPET]
        if ext in OFFICE_PARTS:
            chunks = []
            with zipfile.ZipFile(long_path(path)) as archive:
                for part in OFFICE_PARTS[ext]:
                    if part in archive.namelist():
                        xml = archive.read(part)[:200_000].decode("utf-8", "ignore")
                        chunks.append(re.sub(r"<[^>]+>", " ", xml))
            return re.sub(r"\s+", " ", " ".join(chunks)).strip()[:SNIPPET]
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return ""
    return ""


class LocalAI:
    def __init__(self, rules: Rules) -> None:
        self.enabled = bool(rules.get("ai.enabled", False))
        self.url = str(rules.get("ai.url", "http://localhost:11434")).rstrip("/")
        self.model = str(rules.get("ai.model", "qwen2.5:7b"))
        self.min_confidence = int(rules.get("ai.min_confidence", 80))
        self.about = str(rules.get("ai.about", "") or "").strip()
        self._available: bool | None = None
        self._cache_path = config.DATA_DIR / "ai_cache.json"
        try:
            self._cache: dict[str, dict] = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = {}
        self._unsaved = 0

    @property
    def local(self) -> bool:
        return is_local_url(self.url)

    def available(self) -> bool:
        if not self.enabled or not self.local:
            return False
        if self._available is None:
            try:
                with _OPENER.open(self.url + "/api/tags", timeout=3) as resp:
                    models = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
                self._available = any(m == self.model or m.split(":")[0] == self.model for m in models)
            except (OSError, ValueError, urllib.error.URLError):
                self._available = False
        return self._available

    def _ask(self, prompt: str) -> dict:
        if not self.local:
            return {}
        body = json.dumps({
            "model": self.model, "prompt": prompt, "stream": False, "format": "json",
            # Короткий контекст — модель целиком помещается в видеокарту на 6 ГБ и отвечает быстрее.
            "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 120},
        }).encode("utf-8")
        request = urllib.request.Request(self.url + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with _OPENER.open(request, timeout=180) as resp:
                data = json.loads(resp.read())
            answer = json.loads(data.get("response") or "{}")
            return answer if isinstance(answer, dict) else {}
        except (OSError, ValueError, urllib.error.URLError):
            return {}

    def save(self) -> None:
        if self._cache:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Через временный файл: если процесс убьют посреди записи, старый кэш останется целым.
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._cache_path)
        self._unsaved = 0

    def classify(self, name: str, sectors: list[dict], sources: list[str], snippet: str,
                 cache_key: str, kind: str | None = None) -> tuple[str | None, str]:
        """Сектор для файла или папки — или None, если модель не уверена."""
        # Сектор, привязанный к типам файлов («Виртуалки» — образы дисков), для других типов не предлагаем.
        sectors = [s for s in sectors if not s.get("types") or kind in s["types"]]
        names = [s["name"] for s in sectors]
        if not names:
            return None, ""
        # В ключе — отпечаток описаний: поправил about или описание сектора — модель спросят заново.
        context = self.about + "\n" + "\n".join(_describe(s) for s in sectors)
        if language() != "ru":  # объяснение на другом языке — другой ответ (русские ключи кэша не меняются)
            context += f"\nlang={language()}"
        key = f"{cache_key}|v{PROMPT_VERSION}|{hashlib.sha1(context.encode('utf-8')).hexdigest()[:10]}"
        cached = self._cache.get(key)
        if cached is None:
            prompt = (
                "You help a person sort their files into topic folders.\n"
                + (f"About the person: {self.about}\n" if self.about else "")
                + "Folders:\n" + "\n".join(_describe(s) for s in sectors) + "\n\n"
                f"File name: {readable_name(name)}\n"
                f"Downloaded from: {', '.join(sources) or 'unknown'}\n"
                f"Beginning of the content: {snippet or '(not available)'}\n\n"
                "Decide which folder this file belongs to.\n"
                "- Choose a folder only if the file name or content relates to its topic.\n"
                "- If the name is generic and there is no telling content (like \"Admin.docx\" or "
                "\"scan_001.pdf\"), answer with an empty folder.\n"
                "- confidence: 90-100 = the topic is named explicitly in the name or content; "
                "60-89 = a strong hint; below 60 = a guess.\n"
                "Answer strictly as JSON: {\"folder\": \"<exact folder name from the list or empty>\", "
                f"\"confidence\": <0-100>, \"why\": \"<up to 10 words in {'English' if language() == 'en' else 'Russian'}>\"}}"
            )
            cached = self._ask(prompt)
            if "folder" not in cached:
                return None, ""  # модель не ответила — не запоминаем, спросим в следующий раз
            self._cache[key] = cached
            self._unsaved += 1
            if self._unsaved >= SAVE_EVERY:
                self.save()  # прерванный прогон не теряет уже полученные ответы
        folder = _match_folder(str(cached.get("folder", "")), names)
        try:
            confidence = int(float(cached.get("confidence", 0)))
        except (TypeError, ValueError):
            confidence = 0
        if folder and confidence >= self.min_confidence:
            return folder, str(cached.get("why", "")).strip()
        return None, ""


def _match_folder(answer: str, names: list[str]) -> str | None:
    """Модель иногда отвечает «Учёба: учёба в университете…» вместо «Учёба» — берём имя до двоеточия."""
    text = answer.strip().lstrip("-• ").strip()
    candidate = text.split(":", 1)[0].strip().casefold()
    return next((name for name in names if name.casefold() == candidate), None)


def _describe(sector: dict) -> str:
    """Строка о секторе для модели: описание, ключевые слова, сайты, типы файлов."""
    parts = []
    if sector.get("description"):
        parts.append(str(sector["description"]))
    if sector.get("keywords"):
        parts.append("keywords: " + ", ".join(map(str, sector["keywords"][:15])))
    if sector.get("sources"):
        parts.append("sites: " + ", ".join(map(str, sector["sources"][:6])))
    if sector.get("types"):
        parts.append("file types: " + ", ".join(map(str, sector["types"])))
    return f"- {sector['name']}: " + "; ".join(parts)
