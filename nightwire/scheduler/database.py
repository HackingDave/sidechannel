"""Database operations for the scheduler system."""

import asyncio
import functools
import json
import sqlite3
import threading
from datetime import datetime
from typing import Optional, List

import structlog

from .models import (
    Schedule,
    ScheduleStatus,
    ScheduleType,
    ScheduleRun,
    RunOutcome,
)

logger = structlog.get_logger()


def _locked(method):
    """Serialize sync database access via threading.Lock."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class SchedulerDatabase:
    """Database operations for scheduled tasks.

    Shares the SQLite connection with the memory/autonomous systems.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock = None):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._lock = lock or threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create scheduler tables if they don't exist."""
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone_number TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_params TEXT NOT NULL,
                    project_name TEXT,
                    project_path TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    description TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_run_at TIMESTAMP,
                    next_run_at TIMESTAMP,
                    last_outcome TEXT,
                    total_runs INTEGER DEFAULT 0,
                    total_notifications INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    completed_at TIMESTAMP,
                    outcome TEXT NOT NULL DEFAULT 'silent',
                    claude_output TEXT,
                    notification_sent TEXT,
                    error TEXT,
                    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_schedules_status
                    ON schedules(status);
                CREATE INDEX IF NOT EXISTS idx_schedules_next_run
                    ON schedules(next_run_at);
                CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule_id
                    ON schedule_runs(schedule_id);
            """)

    def _parse_ts(self, ts_str: Optional[str]) -> Optional[datetime]:
        if not ts_str:
            return None
        try:
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                return datetime.fromisoformat(ts_str)
            except ValueError:
                return None

    def _format_ts(self, dt: Optional[datetime]) -> Optional[str]:
        if not dt:
            return None
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _row_to_schedule(self, row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=row["id"],
            phone_number=row["phone_number"],
            prompt=row["prompt"],
            schedule_type=ScheduleType(row["schedule_type"]),
            schedule_params=json.loads(row["schedule_params"]),
            project_name=row["project_name"],
            project_path=row["project_path"],
            status=ScheduleStatus(row["status"]),
            description=row["description"] or "",
            created_at=self._parse_ts(row["created_at"]),
            last_run_at=self._parse_ts(row["last_run_at"]),
            next_run_at=self._parse_ts(row["next_run_at"]),
            last_outcome=RunOutcome(row["last_outcome"]) if row["last_outcome"] else None,
            total_runs=row["total_runs"],
            total_notifications=row["total_notifications"],
        )

    def _row_to_run(self, row: sqlite3.Row) -> ScheduleRun:
        return ScheduleRun(
            id=row["id"],
            schedule_id=row["schedule_id"],
            started_at=self._parse_ts(row["started_at"]) or datetime.now(),
            completed_at=self._parse_ts(row["completed_at"]),
            outcome=RunOutcome(row["outcome"]),
            claude_output=row["claude_output"],
            notification_sent=row["notification_sent"],
            error=row["error"],
        )

    # ========== Schedule CRUD ==========

    async def create_schedule(
        self,
        phone_number: str,
        prompt: str,
        schedule_type: ScheduleType,
        schedule_params: dict,
        project_name: Optional[str],
        project_path: Optional[str],
        description: str,
        next_run_at: datetime,
    ) -> Schedule:
        return await asyncio.to_thread(
            self._create_schedule_sync,
            phone_number, prompt, schedule_type, schedule_params,
            project_name, project_path, description, next_run_at,
        )

    @_locked
    def _create_schedule_sync(
        self,
        phone_number: str,
        prompt: str,
        schedule_type: ScheduleType,
        schedule_params: dict,
        project_name: Optional[str],
        project_path: Optional[str],
        description: str,
        next_run_at: datetime,
    ) -> Schedule:
        cursor = self._conn.execute(
            """INSERT INTO schedules
               (phone_number, prompt, schedule_type, schedule_params,
                project_name, project_path, description, next_run_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                phone_number, prompt, schedule_type.value,
                json.dumps(schedule_params), project_name, project_path,
                description, self._format_ts(next_run_at),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return self._row_to_schedule(row)

    async def get_schedule(self, schedule_id: int) -> Optional[Schedule]:
        return await asyncio.to_thread(self._get_schedule_sync, schedule_id)

    @_locked
    def _get_schedule_sync(self, schedule_id: int) -> Optional[Schedule]:
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        return self._row_to_schedule(row) if row else None

    async def list_schedules(
        self,
        status: Optional[ScheduleStatus] = None,
        phone_number: Optional[str] = None,
    ) -> List[Schedule]:
        return await asyncio.to_thread(self._list_schedules_sync, status, phone_number)

    @_locked
    def _list_schedules_sync(
        self,
        status: Optional[ScheduleStatus],
        phone_number: Optional[str],
    ) -> List[Schedule]:
        query = "SELECT * FROM schedules WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if phone_number:
            query += " AND phone_number = ?"
            params.append(phone_number)
        query += " ORDER BY id"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    async def get_due_schedules(self, now: datetime) -> List[Schedule]:
        """Get all active schedules whose next_run_at <= now."""
        return await asyncio.to_thread(self._get_due_schedules_sync, now)

    @_locked
    def _get_due_schedules_sync(self, now: datetime) -> List[Schedule]:
        rows = self._conn.execute(
            """SELECT * FROM schedules
               WHERE status = 'active' AND next_run_at <= ?
               ORDER BY next_run_at""",
            (self._format_ts(now),),
        ).fetchall()
        return [self._row_to_schedule(r) for r in rows]

    async def update_schedule_status(
        self, schedule_id: int, status: ScheduleStatus
    ) -> None:
        await asyncio.to_thread(self._update_status_sync, schedule_id, status)

    @_locked
    def _update_status_sync(self, schedule_id: int, status: ScheduleStatus) -> None:
        self._conn.execute(
            "UPDATE schedules SET status = ? WHERE id = ?",
            (status.value, schedule_id),
        )
        self._conn.commit()

    async def update_after_run(
        self,
        schedule_id: int,
        last_run_at: datetime,
        next_run_at: datetime,
        outcome: RunOutcome,
    ) -> None:
        await asyncio.to_thread(
            self._update_after_run_sync, schedule_id, last_run_at, next_run_at, outcome
        )

    @_locked
    def _update_after_run_sync(
        self,
        schedule_id: int,
        last_run_at: datetime,
        next_run_at: datetime,
        outcome: RunOutcome,
    ) -> None:
        notif_incr = 1 if outcome == RunOutcome.NOTIFIED else 0
        self._conn.execute(
            """UPDATE schedules
               SET last_run_at = ?, next_run_at = ?, last_outcome = ?,
                   total_runs = total_runs + 1,
                   total_notifications = total_notifications + ?
               WHERE id = ?""",
            (
                self._format_ts(last_run_at),
                self._format_ts(next_run_at),
                outcome.value,
                notif_incr,
                schedule_id,
            ),
        )
        self._conn.commit()

    async def delete_schedule(self, schedule_id: int) -> bool:
        return await asyncio.to_thread(self._delete_schedule_sync, schedule_id)

    @_locked
    def _delete_schedule_sync(self, schedule_id: int) -> bool:
        # Delete runs first, then the schedule
        self._conn.execute(
            "DELETE FROM schedule_runs WHERE schedule_id = ?", (schedule_id,)
        )
        cursor = self._conn.execute(
            "DELETE FROM schedules WHERE id = ?", (schedule_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ========== Run Records ==========

    async def record_run_start(self, schedule_id: int, started_at: datetime) -> int:
        return await asyncio.to_thread(self._record_run_start_sync, schedule_id, started_at)

    @_locked
    def _record_run_start_sync(self, schedule_id: int, started_at: datetime) -> int:
        cursor = self._conn.execute(
            "INSERT INTO schedule_runs (schedule_id, started_at) VALUES (?, ?)",
            (schedule_id, self._format_ts(started_at)),
        )
        self._conn.commit()
        return cursor.lastrowid

    async def complete_run(
        self,
        run_id: int,
        outcome: RunOutcome,
        claude_output: Optional[str] = None,
        notification_sent: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        await asyncio.to_thread(
            self._complete_run_sync, run_id, outcome, claude_output, notification_sent, error
        )

    @_locked
    def _complete_run_sync(
        self,
        run_id: int,
        outcome: RunOutcome,
        claude_output: Optional[str] = None,
        notification_sent: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            """UPDATE schedule_runs
               SET completed_at = ?, outcome = ?, claude_output = ?,
                   notification_sent = ?, error = ?
               WHERE id = ?""",
            (
                self._format_ts(datetime.now()),
                outcome.value,
                claude_output[:5000] if claude_output else None,
                notification_sent[:2000] if notification_sent else None,
                error[:2000] if error else None,
                run_id,
            ),
        )
        self._conn.commit()

    async def get_recent_runs(
        self, schedule_id: int, limit: int = 5
    ) -> List[ScheduleRun]:
        return await asyncio.to_thread(self._get_recent_runs_sync, schedule_id, limit)

    @_locked
    def _get_recent_runs_sync(self, schedule_id: int, limit: int) -> List[ScheduleRun]:
        rows = self._conn.execute(
            """SELECT * FROM schedule_runs
               WHERE schedule_id = ?
               ORDER BY started_at DESC LIMIT ?""",
            (schedule_id, limit),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]
