from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path


MIN_FREE_BYTES = 200 * 1024 * 1024
DAILY_RETENTION_DAYS = 30
MONTHLY_RETENTION_MONTHS = 12
RECENT_RETENTION_COUNT = 16
RECENT_MAX_AGE = timedelta(minutes=30)
_backup_lock = threading.Lock()
_logger = logging.getLogger("billiard.maintenance")


def backup_directory(database: str) -> Path:
    return Path(database).resolve().parent / "backups"


def log_path(database: str) -> Path:
    return Path(database).resolve().parent / "logs" / "app.log"


def configure_file_logging(database: str) -> Path:
    path = log_path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("billiard")
    if not any(isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path)
               for handler in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path


def verify_database(path: Path, *, require_schema: bool = True) -> dict[str, int]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到資料庫：{path}")
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        result = db.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"資料庫完整性檢查失敗：{result[0] if result else '無結果'}")
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"資料庫有 {len(violations)} 筆外鍵錯誤。")
        if not require_schema:
            return {}
        required = ("settings", "customer_tabs", "sessions", "orders", "payments")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = set(required) - tables
        if missing:
            raise RuntimeError(f"備份缺少資料表：{', '.join(sorted(missing))}")
        return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in required}


def _copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".billiard-backup-", suffix=".db",
                                          dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as db:
            with closing(sqlite3.connect(temporary)) as copy:
                db.backup(copy)
        verify_database(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def create_backup(database: str, *, daily: bool = True,
                  backup_root: Path | None = None,
                  backup_date: date | None = None) -> Path:
    source = Path(database).resolve()
    directory = backup_root or backup_directory(database)
    stamp = (backup_date or date.today()).isoformat() if daily else datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = f"billiard-daily-{stamp}.db" if daily else f"billiard-manual-{stamp}.db"
    destination = directory / name
    with _backup_lock:
        if daily and destination.exists():
            verify_database(destination)
            return destination
        verify_database(source)
        _copy_database(source, destination)
        _logger.info("Database backup created: %s", destination)
    return destination


def create_recent_backup(database: str, *, backup_root: Path | None = None,
                         backup_at: datetime | None = None) -> Path:
    source = Path(database).resolve()
    directory = backup_root or backup_directory(database)
    stamp = (backup_at or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    destination = directory / f"billiard-recent-{stamp}.db"
    with _backup_lock:
        verify_database(source)
        _copy_database(source, destination)
        _logger.info("Recent database backup created: %s", destination)
        snapshots = sorted(directory.glob("billiard-recent-*.db"), reverse=True)
        for expired in snapshots[RECENT_RETENTION_COUNT:]:
            expired.unlink()
            _logger.info("Expired recent backup removed: %s", expired)
    return destination


def _backup_date(path: Path, prefix: str, date_format: str) -> date | None:
    try:
        value = path.name.removeprefix(prefix).removesuffix(".db")
        return datetime.strptime(value, date_format).date()
    except ValueError:
        return None


def maintain_backup_retention(directory: Path, current_day: date) -> list[Path]:
    removed: list[Path] = []
    daily_cutoff = current_day - timedelta(days=DAILY_RETENTION_DAYS - 1)
    monthly_cutoff = current_day.year * 12 + current_day.month - MONTHLY_RETENTION_MONTHS

    for path in directory.glob("billiard-daily-*.db"):
        backup_day = _backup_date(path, "billiard-daily-", "%Y-%m-%d")
        if backup_day is not None and backup_day < daily_cutoff:
            path.unlink()
            removed.append(path)
    for path in directory.glob("billiard-monthly-*.db"):
        backup_month = _backup_date(path, "billiard-monthly-", "%Y-%m")
        if backup_month is not None:
            month_number = backup_month.year * 12 + backup_month.month
            if month_number <= monthly_cutoff:
                path.unlink()
                removed.append(path)
    for path in removed:
        _logger.info("Expired automatic backup removed: %s", path)
    return removed


def _maintain_backup_set(daily_backup: Path, current_day: date) -> None:
    monthly = daily_backup.parent / f"billiard-monthly-{current_day:%Y-%m}.db"
    with _backup_lock:
        if monthly.exists():
            verify_database(monthly)
        else:
            _copy_database(daily_backup, monthly)
            _logger.info("Monthly database backup created: %s", monthly)
        maintain_backup_retention(daily_backup.parent, current_day)


def create_daily_backups(database: str, extra_directory: str = "",
                         *, backup_date: date | None = None) -> tuple[Path, Path | None]:
    current_day = backup_date or date.today()
    local = create_backup(database, backup_date=current_day)
    _maintain_backup_set(local, current_day)
    extra = None
    if extra_directory.strip():
        extra = create_backup(
            database,
            backup_root=Path(extra_directory).expanduser().resolve(),
            backup_date=current_day,
        )
        _maintain_backup_set(extra, current_day)
    return local, extra


def create_recent_backups(database: str, extra_directory: str = "") -> tuple[Path, Path | None]:
    local = create_recent_backup(database)
    extra = None
    if extra_directory.strip():
        extra_root = Path(extra_directory).expanduser().resolve()
        extra = (
            local if extra_root == local.parent
            else create_recent_backup(database, backup_root=extra_root)
        )
    return local, extra


def restore_drill(backup: Path) -> dict[str, int]:
    original_counts = verify_database(backup)
    with tempfile.TemporaryDirectory(prefix="billiard-restore-drill-") as directory:
        restored = Path(directory) / "restored.db"
        _copy_database(backup, restored)
        counts = verify_database(restored)
        if counts != original_counts:
            raise RuntimeError("還原後的核心資料表筆數與備份不一致。")
    _logger.info("Restore drill passed: %s", backup)
    return counts


def backup_warning(database: str, extra_directory: str = "") -> str | None:
    day = date.today().isoformat()
    destinations = [backup_directory(database)]
    if extra_directory.strip():
        destinations.append(Path(extra_directory).expanduser().resolve())
    for destination in destinations:
        backup = destination / f"billiard-daily-{day}.db"
        try:
            verify_database(backup)
        except Exception as exc:
            return f"今日備份尚未通過檢查：{destination}（{exc}）"
        snapshots = list(destination.glob("billiard-recent-*.db"))
        if not snapshots:
            return f"15 分鐘快照尚未建立（{destination}），請檢查備份排程。"
        try:
            latest = max(snapshots, key=lambda path: path.stat().st_mtime)
            if datetime.now().timestamp() - latest.stat().st_mtime > RECENT_MAX_AGE.total_seconds():
                return f"15 分鐘快照已超過 30 分鐘未更新（{destination}），請檢查備份排程。"
            verify_database(latest)
        except Exception as exc:
            return f"15 分鐘快照需要檢查（{destination}）：{exc}。"
    return disk_warning(database, extra_directory)


def disk_warning(database: str, extra_directory: str = "") -> str | None:
    paths = [Path(database).resolve().parent]
    if extra_directory.strip():
        extra = Path(extra_directory).expanduser().resolve()
        if extra.exists():
            paths.append(extra)
    for path in paths:
        free = shutil.disk_usage(path).free
        if free < MIN_FREE_BYTES:
            return f"{path} 可用空間僅 {free // (1024 * 1024)} MB，請清理磁碟或更換備份位置。"
    return None


def start_backup_scheduler(database: str, extra_directory: str = "") -> threading.Event:
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                create_daily_backups(database, extra_directory)
            except Exception:
                _logger.exception("Scheduled daily database backup failed")
            try:
                create_recent_backups(database, extra_directory)
            except Exception:
                _logger.exception("Scheduled recent database backup failed")
            try:
                warning = disk_warning(database, extra_directory)
                if warning:
                    _logger.warning(warning)
            except Exception:
                _logger.exception("Scheduled disk check failed")
            if stop.wait(15 * 60):
                break

    threading.Thread(target=run, name="billiard-backup", daemon=True).start()
    return stop
