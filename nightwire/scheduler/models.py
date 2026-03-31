"""Data models for the scheduler system."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List


class ScheduleType(str, Enum):
    """Type of schedule recurrence."""
    INTERVAL = "interval"
    DAILY = "daily"
    WEEKLY = "weekly"
    WEEKDAY = "weekday"
    WEEKEND = "weekend"


class ScheduleStatus(str, Enum):
    """Status of a schedule."""
    ACTIVE = "active"
    PAUSED = "paused"


class RunOutcome(str, Enum):
    """Outcome of a scheduled run."""
    SILENT = "silent"       # Ran successfully, nothing noteworthy
    NOTIFIED = "notified"   # Ran and sent a notification
    FAILED = "failed"       # Execution failed
    QUEUED = "queued"       # Waiting for a slot
    SKIPPED = "skipped"     # Skipped (e.g., bot was down)


@dataclass
class Schedule:
    """A scheduled task definition."""
    id: int
    phone_number: str
    prompt: str
    schedule_type: ScheduleType
    schedule_params: dict          # e.g. {"minutes": 360} or {"times": ["05:00"]}
    project_name: Optional[str]    # None = global
    project_path: Optional[str]    # Captured at creation time
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    created_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    last_outcome: Optional[RunOutcome] = None
    total_runs: int = 0
    total_notifications: int = 0
    description: str = ""          # Human-readable schedule description


@dataclass
class ScheduleRun:
    """Record of a single scheduled execution."""
    id: int
    schedule_id: int
    started_at: datetime
    completed_at: Optional[datetime] = None
    outcome: RunOutcome = RunOutcome.SILENT
    claude_output: Optional[str] = None
    notification_sent: Optional[str] = None
    error: Optional[str] = None
