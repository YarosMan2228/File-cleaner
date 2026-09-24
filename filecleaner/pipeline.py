"""Выполнение проверки: кэши удаляются, остальное переезжает в «Ready for approval»."""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from . import review
from .analyzers import RECYCLE_BIN, CheckResult, Finding
from .fsutil import display, long_path
from .journal import Session
from .winutil import empty_recycle_bin

Progress = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


@dataclass
class ApplyResult:
    deleted: int = 0
    freed: int = 0
    staged: int = 0
    staged_bytes: int = 0
    batches: list[review.Batch] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply_check(result: CheckResult, session: Session, skip_groups: set[str] | None = None,
                progress: Progress = _quiet) -> ApplyResult:
    skip_groups = skip_groups or set()
    out = ApplyResult()
    chosen = [f for f in result.findings if f.group not in skip_groups]
    to_delete = [f for f in chosen if f.mode == "delete"]
    for i, finding in enumerate(to_delete, 1):
        if i % 200 == 0:
            progress(f"Удаляю кэши и временные файлы: {i}/{len(to_delete)}")
        try:
            if finding.path == RECYCLE_BIN:
                if empty_recycle_bin():
                    session.record("delete", path="Корзина", size=finding.size, rule=finding.rule)
                else:
                    raise OSError("Windows не дала очистить Корзину")
            elif finding.rule == "files.empty_dirs":
                session.rmdir(finding.path)
            else:
                session.delete(finding.path, finding.size, is_dir=finding.is_dir, rule=finding.rule)
            out.deleted += 1
            out.freed += finding.size
        except OSError as exc:
            if len(out.errors) < 50:
                out.errors.append(f"{display(finding.path)}: {exc.strerror or exc}")
    # Папки Temp, которые опустели после удаления файлов.
    for folder in sorted(result.prune_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            os.rmdir(long_path(folder))
        except OSError:
            pass

    to_stage: list[Finding] = [f for f in chosen if f.mode == "review"]
    if to_stage:
        progress(f"Переношу {len(to_stage)} объектов в «Ready for approval»…")
        out.batches, errors = review.stage(to_stage, session)
        out.errors += errors
        out.staged = sum(len(b.entries) for b in out.batches)
        out.staged_bytes = sum(e["size"] for b in out.batches for e in b.entries)
    return out
