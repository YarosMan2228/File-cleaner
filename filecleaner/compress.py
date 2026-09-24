"""Прозрачное сжатие Windows (compact /exe): файлы остаются на местах и открываются как обычно.

Подходит для «холодных» папок: документы, текст, программы, образы. Уже сжатые форматы
(zip, jpg, mp4, docx…) пропускаются — выигрыша там почти нет. Файл, который потом изменят,
Windows сама распакует; отмена (undo) распаковывает всё обратно.
"""
from __future__ import annotations

import os
import subprocess
import time
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .fsutil import (
    FILE_ATTRIBUTE_COMPRESSED, age_days, attributes, is_cloud_only, is_hidden_system, long_path, walk,
)
from .journal import Session
from .rules import Rules
from .winutil import NO_WINDOW, disk_size

Progress = Callable[[str], None]
SAMPLE = 64 * 1024
PIECE = 32 * 1024          # WOF сжимает кусками по 32 КБ
CLUSTER = 4096             # сжатый кусок всё равно занимает целые кластеры
MIN_FILE = 64 * 1024
ALGORITHMS = {"LZX", "XPRESS4K", "XPRESS8K", "XPRESS16K"}
_COMMAND_LIMIT = 24_000


def _quiet(_: str) -> None:
    pass


@dataclass
class Candidate:
    path: Path
    size: int
    saving: float   # доля, на которую файл станет меньше (оценка по выборке)


@dataclass
class CompressPlan:
    folder: Path
    candidates: list[Candidate]
    skipped: Counter
    vm_disks: list[Candidate] = field(default_factory=list)

    @property
    def expected(self) -> int:
        return int(sum(c.size * c.saving for c in self.candidates))


def estimate_saving(path: Path, size: int) -> float:
    """Сжимает несколько кусков файла (как это сделает Windows) и оценивает выигрыш."""
    points = 8 if size < 2**30 else 48
    raw = packed = 0
    try:
        with open(long_path(path), "rb") as fh:
            if size <= SAMPLE * points:
                offsets, length = [0], size
            else:
                offsets = [int(k * (size - SAMPLE) / (points - 1)) for k in range(points)]
                length = SAMPLE
            for offset in offsets:
                fh.seek(offset)
                data = fh.read(length)
                for i in range(0, len(data), PIECE):
                    piece = data[i:i + PIECE]
                    compressed = len(zlib.compress(piece, 6))
                    raw += len(piece)
                    packed += min(len(piece), -(-compressed // CLUSTER) * CLUSTER)
    except OSError:
        return 0.0
    return 1 - packed / raw if raw else 0.0


def plan_compress(folder: Path, rules: Rules, progress: Progress = _quiet, now: float | None = None) -> CompressPlan:
    now = now or time.time()
    min_saving = float(rules.get("compress.min_saving", 0.15))
    recent = float(rules.get("compress.skip_recent_days", 30))
    include_vm = bool(rules.get("compress.include_vm_disks", False))
    plan = CompressPlan(folder, [], Counter())
    checked = 0
    for entry, _ in walk(folder):
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        ext = Path(entry.name.lower()).suffix.lstrip(".")
        if is_cloud_only(st):
            plan.skipped["облачные файлы OneDrive"] += 1
        elif is_hidden_system(st):
            plan.skipped["системные"] += 1
        elif st.st_size < MIN_FILE:
            plan.skipped["маленькие (меньше 64 КБ)"] += 1
        elif ext in config.INCOMPRESSIBLE_EXTS:
            plan.skipped["уже сжатые форматы (zip, jpg, mp4, docx…)"] += 1
        elif age_days(st.st_mtime, now) < recent:
            plan.skipped[f"менялись за последние {recent:g} дн."] += 1
        elif attributes(st) & FILE_ATTRIBUTE_COMPRESSED or disk_size(entry.path) < st.st_size * 0.9:
            plan.skipped["уже сжаты"] += 1
        else:
            saving = estimate_saving(Path(entry.path), st.st_size)
            candidate = Candidate(Path(entry.path), st.st_size, saving)
            checked += 1
            if checked % 100 == 0:
                progress(f"Оцениваю сжатие: {checked} файлов…")
            if ext in config.VM_DISK_EXTS and not include_vm:
                if saving >= min_saving:
                    plan.vm_disks.append(candidate)
                else:
                    plan.skipped["сжимаются плохо (меньше порога)"] += 1
            elif saving >= min_saving:
                plan.candidates.append(candidate)
            else:
                plan.skipped["сжимаются плохо (меньше порога)"] += 1
    return plan


def _chunks(paths: list[Path]) -> list[list[Path]]:
    chunks, current, length = [], [], 0
    for path in paths:
        text = str(path)
        if current and length + len(text) + 3 > _COMMAND_LIMIT:
            chunks.append(current)
            current, length = [], 0
        current.append(path)
        length += len(text) + 3
    if current:
        chunks.append(current)
    return chunks


def _compact(args: list[str], paths: list[Path]) -> None:
    exe = str(config.WINDIR / "System32" / "compact.exe")
    subprocess.run([exe, *args, "/i", "/q", *[str(p) for p in paths]], capture_output=True,
                   timeout=24 * 3600, creationflags=NO_WINDOW)


@dataclass
class CompressResult:
    files: int = 0
    before: int = 0
    after: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)


def apply_compress(plan: CompressPlan, rules: Rules, session: Session, progress: Progress = _quiet) -> CompressResult:
    algorithm = str(rules.get("compress.algorithm", "LZX")).upper()
    if algorithm not in ALGORITHMS:
        algorithm = "LZX"
    result = CompressResult()
    items = plan.candidates + (plan.vm_disks if rules.get("compress.include_vm_disks", False) else [])
    chunks = _chunks([c.path for c in items])
    done = 0
    for chunk in chunks:
        before = sum(disk_size(p) for p in chunk if os.path.exists(long_path(p)))
        try:
            _compact(["/c", f"/exe:{algorithm}"], chunk)
        except (OSError, subprocess.SubprocessError) as exc:
            result.errors.append(str(exc))
            continue
        after = sum(disk_size(p) for p in chunk if os.path.exists(long_path(p)))
        session.record("compress", paths=[str(p) for p in chunk], algorithm=algorithm, before=before, after=after)
        result.files += len(chunk)
        result.before += before
        result.after += after
        done += len(chunk)
        progress(f"Сжато {done}/{len(items)} файлов")
    return result


def decompress(paths: list[Path]) -> None:
    for chunk in _chunks(paths):
        _compact(["/u", "/exe"], chunk)
