"""«Приступай»: всё за один запуск — можно оставить на ночь.

1. Мусор и копии ищутся на всех встроенных дисках. Windows, программы, игры и проекты не трогаются;
   на системном диске вне твоей папки пользователя — только отчёт.
2. Кэши и временные файлы удаляются сразу.
3. Проверенное в папках-свалках (Загрузки, Рабочий стол…) удаляется сразу: копия ещё раз сравнивается
   с оригиналом байт в байт, распакованный zip — с папкой по контрольным суммам.
4. Всё остальное переезжает в «Ready for approval» и ждёт утра.
5. Свалки раскладываются по секторам и типам; в остальных папках (Документы) — только отдельные файлы.
6. Отчёт; потом — сон или выключение, если так сказано в правилах.

Всё считается на этом компьютере: ИИ работает только с локальной моделью (см. ai.is_local_url).
"""
from __future__ import annotations

import json
import os
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import analyzers, config, journal, organizer, pipeline, report, review
from .ai import LocalAI
from .analyzers import CheckResult, Finding
from .fsutil import display, human_size, is_under, key_of, long_path
from .i18n import tr
from .index import Index
from .rules import Rules
from .verify import Verdict, check_archive, check_copy
from .winutil import exclusive, fixed_drives, go_to_sleep, idle_seconds, keep_awake, shut_down

Log = Callable[[str], None]
QUIET = 600          # с без мыши и клавиатуры: за компьютером никого нет — ночной запуск по расписанию начинает
QUIET_WAIT = 7200    # с, сколько ждать этого (потом — пропустить ночь)
AFTER = ("nothing", "sleep", "shutdown")
# Что можно удалять ночью без утреннего «утвердить»: содержимое ещё раз сверяется перед удалением.
AUTO_RULES = frozenset({
    "duplicates.same_folder", "duplicates.copy_names", "duplicates.in_downloads", "duplicates.folders",
    "archives.extracted",
})


def _quiet(_: str) -> None:
    pass


# ======================================================================= план
@dataclass
class NightPlan:
    roots: dict[str, Path]
    dumps: list[Path]
    check: CheckResult
    auto: list[Finding] = field(default_factory=list)                # удалить сразу (после сверки)
    morning: list[Finding] = field(default_factory=list)             # «Ready for approval» до утра
    held: list[tuple[Finding, str]] = field(default_factory=list)   # хотелось удалить сразу, но — утро
    sort_folders: list[tuple[Path, bool]] = field(default_factory=list)  # (папка, переносить ли подпапки)


def night_roots(rules: Rules) -> dict[str, Path]:
    """Личные папки (по именам — чтобы работали правила «Загрузки», «Рабочий стол»), свалки и все диски."""
    roots = dict(rules.roots())
    for folder in analyzers.dump_folders(roots, rules):
        if key_of(folder) not in {key_of(r) for r in roots.values()}:
            roots[str(folder)] = folder
    if rules.get("night.drives", True):
        for drive in fixed_drives():
            roots.setdefault(str(drive), drive)
    return roots


def sort_folders(roots: dict[str, Path], rules: Rules, dumps: list[Path]) -> list[tuple[Path, bool]]:
    """Папки для сортировки. Подпапки переносятся только в свалках: в Документах живут папки программ."""
    folders = config.user_folders()
    dump_keys = {key_of(d) for d in dumps}
    result: list[tuple[Path, bool]] = []
    seen: set[str] = set()
    for item in rules.get("night.sort_folders", ["downloads", "desktop", "documents"]) or []:
        path = roots.get(item) or folders.get(item) or Path(item).expanduser()
        if not path.is_dir() or key_of(path) in seen:
            continue
        seen.add(key_of(path))
        result.append((path, key_of(path) in dump_keys))
    return result


def _outside_home_on_system_drive(path: Path) -> bool:
    return path.drive.upper() == config.WINDIR.drive.upper() and not is_under(path, config.HOME)


def guard_system_drive(findings: list[Finding]) -> None:
    """На системном диске вне папки пользователя (C:\\Autodesk, C:\\Projects…) — данные программ: только отчёт."""
    for finding in findings:
        if finding.rule.startswith("junk.") or finding.mode == "report":
            continue
        if _outside_home_on_system_drive(finding.path):
            finding.mode = "report"
            finding.reason += tr(" — на системном диске вне твоей папки: только отчёт")


def _is_archive(finding: Finding) -> bool:
    """Распакованный архив: перед удалением он сверяется с папкой — его содержимое остаётся там."""
    return finding.rule == "archives.extracted"


def split(findings: list[Finding], dumps: list[Path], auto_delete: bool = True
          ) -> tuple[list[Finding], list[Finding], list[tuple[Finding, str]]]:
    """(удалить сразу, до утра, отложено до утра с причиной). Ничего не удаляется — только решение."""
    removed = {key_of(f.path): f for f in findings if f.mode in ("review", "delete")}
    removed_dirs = [k for k, f in removed.items() if f.is_dir]
    referenced = {key_of(f.original) for f in findings if f.original and f.mode in ("review", "delete")}

    def gone(key: str) -> Finding | None:
        if key in removed:
            return removed[key]
        return next((removed[d] for d in removed_dirs if key.startswith(d + os.sep)), None)

    def original_survives(finding: Finding) -> str | None:
        """None — оригинал остаётся на месте (сам или его содержимое в распакованной папке)."""
        original = finding.original
        if original is None or not os.path.lexists(long_path(original)):
            return tr("оригинала нет на месте")
        other = gone(key_of(original))
        if other is None:
            return None
        if _is_archive(other) and other.original is not None and gone(key_of(other.original)) is None:
            return None  # оригинал — распакованный архив, его содержимое остаётся в папке
        return tr("оригинал тоже на удаление")

    auto: list[Finding] = []
    morning: list[Finding] = []
    held: list[tuple[Finding, str]] = []
    for finding in findings:
        if finding.mode != "review":
            continue
        if not auto_delete or finding.rule not in AUTO_RULES:
            morning.append(finding)
            continue
        if not any(is_under(finding.path, d) for d in dumps):
            morning.append(finding)  # копии вне свалок — рабочие папки: решаешь утром
            continue
        if key_of(finding.path) in referenced and finding.rule != "archives.extracted":
            held.append((finding, tr("это оригинал для другой копии")))
        elif (why := original_survives(finding)) is not None:
            held.append((finding, why))
        else:
            auto.append(finding)
    return auto, morning, held


def plan_night(rules: Rules, progress: Log = _quiet, now: float | None = None) -> NightPlan:
    roots = night_roots(rules)
    dumps = analyzers.dump_folders(roots, rules)
    with Index() as index:
        index.scan(roots, progress)
        result = analyzers.run_check(index, roots, rules, progress, now)
    guard_system_drive(result.findings)
    auto, morning, held = split(result.findings, dumps, bool(rules.get("night.auto_delete", True)))
    return NightPlan(roots, dumps, result, auto, morning, held, sort_folders(roots, rules, dumps))


# ======================================================================= сверка перед удалением
def verify(finding: Finding) -> Verdict:
    """Архив — с папкой, куда его распаковали; копия — с оригиналом."""
    original = finding.original
    if original is None:
        return Verdict("no_original")
    if finding.rule == "archives.extracted":
        return check_archive(finding.path, original)
    return check_copy(finding.path, original)


def verify_all(plan: NightPlan, progress: Log = _quiet) -> list[Finding]:
    """Что из plan.auto прошло сверку; остальное уходит в plan.held (до утра).

    Копия, чей оригинал сам на удалении (копия распакованного zip), удаляется, только если этот zip
    тоже прошёл сверку — то есть его содержимое точно осталось в папке.
    """
    removed = {key_of(f.path) for f in plan.check.findings if f.mode in ("review", "delete")}
    first = [f for f in plan.auto if key_of(f.original) not in removed]
    chained = [f for f in plan.auto if key_of(f.original) in removed]
    verified: list[Finding] = []
    verified_keys: set[str] = set()
    for group in (first, chained):
        for i, finding in enumerate(group, 1):
            progress(tr("Сверяю с оригиналом {i}/{total}: {name}", i=i, total=len(group), name=finding.path.name))
            if group is chained and key_of(finding.original) not in verified_keys:
                plan.held.append((finding, tr("оригинал — архив, который не прошёл сверку")))
            elif (verdict := verify(finding)).ok:
                verified.append(finding)
                verified_keys.add(key_of(finding.path))
            else:
                plan.held.append((finding, tr("при сверке: {why}", why=verdict.text())))
    return verified


# ======================================================================= выполнение
@dataclass
class NightResult:
    started: datetime
    finished: datetime | None = None
    junk_deleted: int = 0
    junk_freed: int = 0
    auto_deleted: list[Finding] = field(default_factory=list)
    auto_freed: int = 0
    held: list[tuple[Finding, str]] = field(default_factory=list)
    batches: list = field(default_factory=list)
    waiting: int = 0
    waiting_bytes: int = 0
    sorted: list[tuple[Path, list[organizer.SortMove], organizer.SortResult]] = field(default_factory=list)
    sort_skipped: list[tuple[Path, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report: Path | None = None


def run_night(rules: Rules, log: Log = print, progress: Log = _quiet) -> NightResult:
    with exclusive():  # окно, консоль и Планировщик не работают с файлами одновременно (winutil.Busy — занято)
        out = NightResult(datetime.now())
        keep_awake(True)
        try:
            _run(rules, out, log, progress)
        finally:
            keep_awake(False)
            out.finished = datetime.now()
            out.report = report.save(tr("ночь"), render(out, rules))
            _save_last(out)
        return out


def wait_for_quiet(log: Log = print, need: int = QUIET, limit: int = QUIET_WAIT) -> bool:
    """Запуск по расписанию: ждём, пока за компьютером никого нет, чтобы файлы не переезжали у тебя на глазах.

    False — так и не дождались (засиделся допоздна): эту ночь пропускаем. Считаем по часам, а не по сну потока:
    если компьютер уснул, пока ждали, днём после пробуждения ничего не начнётся.
    """
    deadline = time.time() + limit
    told = False
    keep_awake(True)  # пока ждём, Windows не уснёт — иначе проснётся уже утром
    try:
        while idle_seconds() < need:
            if time.time() >= deadline:
                log(tr("За компьютером так и работали — эту ночь пропускаю."))
                return False
            if not told:
                log(tr("За компьютером работают — начну, когда {minutes} мин никого не будет.", minutes=need // 60))
                told = True
            time.sleep(30)
        return True
    finally:
        keep_awake(False)


def _stage(title: str, log: Log, out: NightResult, action: Callable[[], None]) -> None:
    """Шаг ночи: сбой одного шага записывается и не останавливает остальные."""
    log(f"{datetime.now():%H:%M} {title}")
    try:
        action()
    except Exception as exc:  # noqa: BLE001 — ночью никто не ответит на ошибку, записываем и идём дальше
        out.errors.append(f"{title}: {exc}")
        log(f"   ! {exc}")
        (config.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        with open(config.DATA_DIR / "logs" / "night-errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"--- {datetime.now():%Y-%m-%d %H:%M} {title}\n{traceback.format_exc()}\n")


def _run(rules: Rules, out: NightResult, log: Log, progress: Log) -> None:
    ai = LocalAI(rules)
    if ai.enabled and not ai.local:
        out.notes.append(tr("ИИ выключен: адрес модели {url} не на этом компьютере — файлы в сеть не отправляю.", url=ai.url))
    elif ai.enabled and not ai.available():
        out.notes.append(tr("ИИ недоступен (запущен ли Ollama? модель {model}) — раскладываю по правилам.", model=ai.model))
    rules.data.setdefault("ai", {})["max_items"] = int(rules.get("night.ai_max_items", 2000))

    state: dict = {}

    def check() -> None:
        plan = plan_night(rules, progress)
        state["plan"] = plan
        out.notes += plan.check.notes
        log(tr("   найдено: удалить сразу {auto}, до утра {waiting}, кэши {junk}", auto=len(plan.auto),
               waiting=len(plan.morning) + len(plan.held), junk=len(plan.check.by_mode("delete"))))

    def delete_verified() -> None:
        plan: NightPlan = state["plan"]
        verified = verify_all(plan, progress)
        log(tr("   сверено с оригиналами: {done} из {total}", done=len(verified), total=len(plan.auto)))
        with journal.Session("night", tr("Ночь: проверенные копии")) as session:
            for finding in verified:
                try:
                    session.delete(finding.path, finding.size, is_dir=finding.is_dir, rule=finding.rule,
                                   original=str(finding.original))
                    out.auto_deleted.append(finding)
                    out.auto_freed += finding.size
                except OSError as exc:
                    out.errors.append(f"{display(finding.path)}: {exc.strerror or exc}")
        out.held = plan.held
        log(tr("   удалено проверенных копий: {count} ({size})", count=len(out.auto_deleted), size=human_size(out.auto_freed)))

    def junk_and_morning() -> None:
        plan: NightPlan = state["plan"]
        findings = plan.check.by_mode("delete") + plan.morning + [f for f, _ in plan.held]
        result = CheckResult(findings, plan.check.notes, plan.check.prune_dirs)
        with journal.Session("check", tr("Ночь: проверка")) as session:
            applied = pipeline.apply_check(result, session, progress=progress)
        out.junk_deleted, out.junk_freed = applied.deleted, applied.freed
        out.batches = applied.batches
        out.waiting, out.waiting_bytes = applied.staged, applied.staged_bytes
        out.errors += applied.errors
        if applied.busy:
            out.notes.append(tr("{count} временных файлов и кэшей заняты программами "
                                "или требуют прав администратора — пропущены.", count=applied.busy))
        log(tr("   кэши: {junk} ({junk_size}); до утра: {waiting} ({waiting_size})", junk=applied.deleted,
               junk_size=human_size(applied.freed), waiting=applied.staged, waiting_size=human_size(applied.staged_bytes)))

    def check_waiting() -> None:
        """Утром у отложенных архивов и копий уже написано, можно ли их удалять."""
        counts = review.check_batches(getattr(out, "batches", []), progress)
        log(tr("   сверено отложенное: можно удалять {ok}, нельзя {bad}, не проверить {unknown}", **counts))

    def sort_all() -> None:
        plan: NightPlan | None = state.get("plan")
        dumps = plan.dumps if plan else analyzers.dump_folders(rules.roots(), rules)
        folders = plan.sort_folders if plan else sort_folders(rules.roots(), rules, dumps)
        for folder, with_subfolders in folders:
            log(tr("   раскладываю {folder}", folder=display(folder)) + ("" if with_subfolders else tr(" (только файлы)")))
            sort_plan = organizer.plan_sort(folder, rules, progress, move_folders=with_subfolders)
            if not sort_plan.moves:
                reason = next(iter(sort_plan.skipped), "") if sort_plan.skipped else tr("всё уже разложено")
                out.sort_skipped.append((folder, reason))
                continue
            with journal.Session("sort", tr("Ночь: сортировка {folder}", folder=folder.name)) as session:
                result = organizer.apply_sort(sort_plan, rules, session, progress)
            out.sorted.append((folder, sort_plan.moves, result))
            out.errors += result.errors
            if result.duplicates_staged:
                out.notes.append(tr("{folder}: {count} копий, найденных при сортировке, ждут утра в «{review}»",
                                    folder=display(folder), count=result.duplicates_staged,
                                    review=config.REVIEW_DIR_NAME))
            log(tr("      разложено: {count}", count=result.moved))

    _stage(tr("Ищу мусор и копии на всех дисках…"), log, out, check)
    if "plan" in state:
        _stage(tr("Сверяю и удаляю проверенные копии…"), log, out, delete_verified)
        _stage(tr("Удаляю кэши, остальное — в «Ready for approval»…"), log, out, junk_and_morning)
        _stage(tr("Сверяю отложенное до утра: архивы и копии…"), log, out, check_waiting)
    _stage(tr("Раскладываю файлы…"), log, out, sort_all)


# ======================================================================= после
def after(action: str, log: Log = print, delay: int = 60, scheduled: bool = False) -> None:
    """Сон или выключение через delay секунд (Ctrl+C — отменить).

    scheduled — запуск по расписанию, окна нет: если за компьютером сидят или тронули мышь и клавиатуру
    во время отсчёта — ни сна, ни выключения.
    """
    if action not in ("sleep", "shutdown"):
        return
    if scheduled and idle_seconds() < QUIET:
        log(tr("За компьютером работают — не усыпляю и не выключаю."))
        return
    try:
        for left in range(delay, 0, -10):
            log(tr("Через {left} с усыплю компьютер (Ctrl+C — отменить)…", left=left) if action == "sleep"
                else tr("Через {left} с выключу компьютер (Ctrl+C — отменить)…", left=left))
            time.sleep(min(10, left))
            if scheduled and idle_seconds() < delay - left + 10:
                log(tr("Кто-то тронул мышь или клавиатуру — компьютер остаётся включённым."))
                return
    except KeyboardInterrupt:
        log(tr("Отменено — компьютер остаётся включённым."))
        return
    ok = go_to_sleep() if action == "sleep" else shut_down()
    if not ok:
        log(tr("Windows не дала усыпить компьютер.") if action == "sleep" else tr("Windows не дала выключить компьютер."))


def _save_last(out: NightResult) -> None:
    data = {
        "finished": (out.finished or datetime.now()).isoformat(timespec="minutes"),
        "freed": out.junk_freed + out.auto_freed, "sorted": sum(r.moved for _, _, r in out.sorted),
        "waiting": out.waiting_bytes, "errors": len(out.errors), "report": str(out.report or ""),
    }
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / "night-last.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def last_run() -> dict | None:
    try:
        return json.loads((config.DATA_DIR / "night-last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ======================================================================= отчёт
def render(out: NightResult, rules: Rules) -> str:
    took = (out.finished or datetime.now()) - out.started
    minutes = int(took.total_seconds() // 60)
    moved = sum(r.moved for _, _, r in out.sorted)
    sections = [
        report.Section(
            tr("Удалено сразу: проверенные копии — {size}", size=human_size(out.auto_freed)),
            [tr("Что"), tr("Размер"), tr("Почему"), tr("Что осталось")],
            [[display(f.path), human_size(f.size), f.reason, display(f.original or "")]
             for f in sorted(out.auto_deleted, key=lambda f: -f.size)],
            note=tr("{count} шт. — содержимое сверено с оригиналом перед удалением", count=len(out.auto_deleted)), open=True,
        ),
        report.Section(
            tr("Ждёт утра — «{review}»: {size}", review=config.REVIEW_DIR_NAME, size=human_size(out.waiting_bytes)),
            [tr("Папка"), tr("Объектов"), tr("Размер")],
            [[str(b.path), str(len(b.entries)), human_size(sum(e['size'] for e in b.entries))] for b in out.batches],
            note=tr("реши в окне программы: «На решение» — удалить или вернуть на место"), open=True,
        ),
        report.Section(
            tr("Хотел удалить сразу, но оставил до утра"),
            [tr("Что"), tr("Размер"), tr("Почему не сразу")],
            [[display(f.path), human_size(f.size), why] for f, why in out.held],
            note=tr("{count} шт.", count=len(out.held)),
        ),
    ]
    for folder, moves, result in out.sorted:
        sections.append(report.Section(
            tr("Разложено: {folder} — {count}", folder=display(folder), count=result.moved),
            [tr("Откуда"), tr("Куда"), tr("Почему")], [[display(m.src), display(m.dst), m.reason] for m in moves],
            note=tr("«Отменить действие» вернёт всё как было"),
        ))
    if out.sort_skipped:
        sections.append(report.Section(tr("Не раскладывал"), [tr("Папка"), tr("Почему")],
                                       [[display(p), why] for p, why in out.sort_skipped]))
    if out.notes or out.errors:
        sections.append(report.Section(tr("Замечания и ошибки"), ["", ""],
                                       [[tr("замечание"), n] for n in out.notes] + [[tr("ошибка"), e] for e in out.errors],
                                       open=bool(out.errors)))
    return report.render(
        tr("Ночь {date}", date=f"{out.started:%d.%m.%Y}"),
        tr("Началось в {start}, заняло {hours} ч {minutes} мин. Всё считалось на этом компьютере.",
           start=f"{out.started:%H:%M}", hours=minutes // 60, minutes=minutes % 60),
        [(tr("Освобождено"), human_size(out.junk_freed + out.auto_freed)), (tr("Кэши и временное"), human_size(out.junk_freed)),
         (tr("Проверенные копии"), human_size(out.auto_freed)), (tr("Разложено"), str(moved)),
         (tr("Ждёт утра"), human_size(out.waiting_bytes))],
        sections,
    )
