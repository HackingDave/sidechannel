"""Pydantic models for the autonomous task system.

Defines the domain models for the PRD-Story-Task hierarchy, execution
results, quality gate outputs, verification results, and Claude
structured output schemas.

Domain models (stored in DB):
    PRD, Story, Task, Learning, QualityGateResult, VerificationResult,
    TaskExecutionResult, AutonomousContext, LoopStatus

Enums:
    PRDStatus, StoryStatus, TaskStatus, EffortLevel, TaskType,
    LearningCategory

Structured output schemas (intermediate, not stored):
    PRDBreakdown, StoryBreakdown, TaskBreakdown, VerificationOutput,
    LearningExtraction, ExtractedLearning, PytestJsonReport,
    PytestTestResult, JestJsonReport
"""

from datetime import datetime
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class PRDStatus(str, Enum):
    """Lifecycle status of a Product Requirements Document.

    Flow: DRAFT -> ACTIVE -> COMPLETED or ARCHIVED.
    """
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class StoryStatus(str, Enum):
    """Status of a user story."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """Lifecycle status of an autonomous task.

    Flow: PENDING -> QUEUED -> IN_PROGRESS -> RUNNING_TESTS ->
    VERIFYING -> COMPLETED | FAILED | BLOCKED | CANCELLED.
    """
    PENDING = "pending"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    RUNNING_TESTS = "running_tests"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class EffortLevel(str, Enum):
    """Claude effort level for adaptive thinking.

    Maps to the ``thinking.budget_tokens`` parameter in the
    Anthropic SDK. Higher effort = more thinking tokens.
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class TaskType(str, Enum):
    """Task type used to auto-select the effort level.

    Detected from task title/description keywords in
    ``executor.detect_task_type()``.
    """
    PRD_BREAKDOWN = "prd_breakdown"
    IMPLEMENTATION = "implementation"
    BUG_FIX = "bug_fix"
    REFACTOR = "refactor"
    TESTING = "testing"
    VERIFICATION = "verification"


class LearningCategory(str, Enum):
    """Category of extracted learning.

    Used to classify learnings for relevance scoring and
    context injection into future task prompts.
    """
    PATTERN = "pattern"
    PITFALL = "pitfall"
    BEST_PRACTICE = "best_practice"
    PROJECT_CONTEXT = "project_context"
    DEBUGGING = "debugging"
    ARCHITECTURE = "architecture"
    TESTING = "testing"
    TOOL_USAGE = "tool_usage"


class PRD(BaseModel):
    """Product Requirements Document.

    Top-level container in the PRD -> Story -> Task hierarchy.
    Contains computed story counts populated by DB joins.
    """

    id: Optional[int] = None
    phone_number: str = Field(..., description="Owner's phone number (E.164)")
    project_name: str = Field(..., description="Associated project name")
    title: str = Field(..., description="PRD title")
    description: str = Field(..., description="Full PRD description")
    status: PRDStatus = Field(default=PRDStatus.DRAFT)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    metadata: Optional[dict[str, Any]] = None

    # Computed/joined fields (not stored in DB)
    stories: List["Story"] = Field(default_factory=list)
    total_stories: int = 0
    completed_stories: int = 0
    failed_stories: int = 0


class Story(BaseModel):
    """User story within a PRD.

    Groups related tasks under acceptance criteria. Contains
    computed task counts populated by DB joins.
    """

    id: Optional[int] = None
    prd_id: int = Field(..., description="Parent PRD ID")
    phone_number: str = Field(..., description="Owner's phone number")
    title: str = Field(..., description="Story title")
    description: str = Field(..., description="Story description")
    acceptance_criteria: Optional[List[str]] = Field(
        default=None, description="List of acceptance criteria"
    )
    priority: int = Field(default=0, description="Execution priority (higher = first)")
    story_order: int = Field(default=0, description="Order within PRD")
    status: StoryStatus = Field(default=StoryStatus.PENDING)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    embedding_id: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None

    # Computed/joined fields
    tasks: List["Task"] = Field(default_factory=list)
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0


class Task(BaseModel):
    """Atomic task for autonomous execution.

    The smallest unit of work. Executed in a fresh Claude context
    with git safety, optional verification, and quality gates.
    Supports dependency-aware parallel scheduling.
    """

    id: Optional[int] = None
    story_id: int = Field(..., description="Parent story ID")
    phone_number: str = Field(..., description="Owner's phone number")
    project_name: str = Field(..., description="Project to execute in")
    title: str = Field(..., description="Task title")
    description: str = Field(..., description="Detailed task description")
    task_order: int = Field(default=0, description="Order within story")
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    priority: int = Field(default=0, description="Execution priority")
    retry_count: int = Field(default=0, description="Current retry count")
    max_retries: int = Field(default=2, description="Maximum retry attempts")
    effort_level: Optional[EffortLevel] = Field(
        default=None, description="Claude effort level override (auto-detected if None)"
    )
    task_type: Optional[TaskType] = Field(
        default=None, description="Task type for effort mapping (auto-detected if None)"
    )
    depends_on: Optional[List[int]] = Field(
        default=None, description="Task IDs this task depends on (for parallel execution)"
    )
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    claude_output: Optional[str] = None
    files_changed: Optional[List[str]] = Field(
        default=None, description="List of modified file paths"
    )
    quality_gate_results: Optional[dict[str, Any]] = None
    verification_result: Optional[dict[str, Any]] = None
    embedding_id: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None


class Learning(BaseModel):
    """Persistent learning extracted from task execution.

    Learnings are semantically searchable and injected into
    future task prompts for context continuity. Confidence
    decays over time for unused learnings.
    """

    id: Optional[int] = None
    phone_number: str = Field(..., description="Owner's phone number")
    project_name: Optional[str] = Field(
        default=None, description="Associated project (optional)"
    )
    task_id: Optional[int] = Field(
        default=None, description="Source task ID (nullable for manual)"
    )
    category: LearningCategory = Field(..., description="Learning category")
    title: str = Field(..., description="Brief learning title")
    content: str = Field(..., description="Full learning content")
    relevance_keywords: Optional[List[str]] = Field(
        default=None, description="Keywords for search matching"
    )
    usage_count: int = Field(default=0, description="Times used in context")
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Confidence score"
    )
    created_at: datetime = Field(default_factory=datetime.now)
    last_used: Optional[datetime] = None
    embedding_id: Optional[int] = None
    is_active: bool = Field(default=True, description="Whether learning is active")
    metadata: Optional[dict[str, Any]] = None


class QualityGateResult(BaseModel):
    """Results from quality gate checks.

    Captures test counts, typecheck/lint pass/fail, and whether
    new regressions were introduced vs a pre-task baseline.
    """

    passed: bool = Field(..., description="Overall pass/fail")
    tests_run: int = Field(default=0, description="Total tests executed")
    tests_passed: int = Field(default=0, description="Tests that passed")
    tests_failed: int = Field(default=0, description="Tests that failed")
    test_output: Optional[str] = Field(
        default=None, description="Test runner output (truncated)"
    )
    typecheck_passed: Optional[bool] = Field(
        default=None, description="Type check result"
    )
    typecheck_output: Optional[str] = Field(
        default=None, description="Type checker output"
    )
    lint_passed: Optional[bool] = Field(default=None, description="Linter result")
    lint_output: Optional[str] = Field(default=None, description="Linter output")
    execution_time_seconds: float = Field(
        default=0.0, description="Total gate execution time"
    )
    regression_detected: bool = Field(
        default=False, description="Whether new test failures were introduced vs baseline"
    )


class VerificationResult(BaseModel):
    """Result from independent verification agent.

    Fail-closed: if security_concerns or logic_errors are
    non-empty, passed is forced False regardless of what the
    verifier returned.
    """

    passed: bool = Field(..., description="Whether verification passed")
    issues: List[str] = Field(
        default_factory=list, description="Issues found during verification"
    )
    security_concerns: List[str] = Field(
        default_factory=list, description="Security issues found"
    )
    logic_errors: List[str] = Field(
        default_factory=list, description="Logic errors found"
    )
    suggestions: List[str] = Field(
        default_factory=list, description="Improvement suggestions"
    )
    verification_output: str = Field(
        default="", description="Full verification output"
    )
    execution_time_seconds: float = Field(
        default=0.0, description="Verification execution time"
    )
    usage_data: Optional[dict] = Field(
        default=None, description="Token/cost usage from verification run"
    )


class TaskExecutionResult(BaseModel):
    """Result of autonomous task execution.

    Aggregates Claude output, files changed, quality gate
    results, verification results, and extracted learnings
    into a single result object.
    """

    task_id: int = Field(..., description="Executed task ID")
    success: bool = Field(..., description="Overall success status")
    claude_output: str = Field(..., description="Claude's response")
    files_changed: List[str] = Field(
        default_factory=list, description="Files modified"
    )
    quality_gate: Optional[QualityGateResult] = Field(
        default=None, description="Quality gate results"
    )
    verification: Optional[VerificationResult] = Field(
        default=None, description="Independent verification results"
    )
    learnings_extracted: List[Learning] = Field(
        default_factory=list, description="Extracted learnings"
    )
    error_message: Optional[str] = Field(default=None, description="Error if failed")
    execution_time_seconds: float = Field(
        default=0.0, description="Total execution time"
    )
    usage_data: Optional[list] = Field(
        default=None, description="List of usage dicts from all Claude calls"
    )


class AutonomousContext(BaseModel):
    """Context assembled for task execution.

    Injected into the Claude prompt to provide learnings from
    previous tasks, parent story/PRD context, and completed
    sibling tasks for continuity.
    """

    learnings: List[Learning] = Field(
        default_factory=list, description="Relevant learnings"
    )
    story: Optional[Story] = Field(default=None, description="Parent story")
    prd: Optional[PRD] = Field(default=None, description="Parent PRD")
    previous_tasks: List[Task] = Field(
        default_factory=list, description="Previous tasks in story"
    )
    token_count: int = Field(default=0, description="Approximate token count")


class LoopStatus(BaseModel):
    """Status snapshot of the autonomous processing loop.

    Returned by ``AutonomousLoop.get_status()`` for the
    ``/autonomous status`` Signal command.
    """

    is_running: bool = Field(default=False, description="Whether loop is active")
    is_paused: bool = Field(default=False, description="Whether loop is paused")
    current_task_id: Optional[int] = Field(
        default=None, description="Currently executing task"
    )
    parallel_task_ids: List[int] = Field(
        default_factory=list, description="Tasks executing in parallel"
    )
    max_parallel: int = Field(default=1, description="Max parallel workers")
    tasks_queued: int = Field(default=0, description="Tasks in queue")
    tasks_completed_today: int = Field(default=0, description="Tasks completed today")
    tasks_failed_today: int = Field(default=0, description="Tasks failed today")
    last_task_completed_at: Optional[datetime] = None
    uptime_seconds: float = Field(default=0.0, description="Loop uptime")


# ---------------------------------------------------------------------------
# Claude Structured Output Schemas
# These define the JSON schema Claude returns via run_claude_structured().
# They are NOT stored in the database — they are intermediate parsing models
# that get mapped to domain models (PRD/Story/Task/etc.) for persistence.
# ---------------------------------------------------------------------------


class TaskBreakdown(BaseModel):
    """A single task in a PRD breakdown from Claude.

    Intermediate schema -- mapped to ``Task`` for DB storage.
    """

    title: str = Field(..., max_length=80, description="Task title")
    description: str = Field(..., description="Detailed task description")
    priority: int = Field(
        default=5, ge=1, le=100, description="Execution priority (higher=first)"
    )


class StoryBreakdown(BaseModel):
    """A single story in a PRD breakdown from Claude.

    Intermediate schema -- mapped to ``Story`` for DB storage.
    """

    title: str = Field(..., max_length=80, description="Story title")
    description: str = Field(..., description="What this story accomplishes")
    tasks: List[TaskBreakdown] = Field(
        ..., min_length=1, description="Tasks within this story"
    )


class PRDBreakdown(BaseModel):
    """Complete PRD breakdown returned by Claude structured output.

    Used by ``run_claude_structured()`` in the PRD creation
    flow. Mapped to PRD + Story + Task domain models.
    """

    prd_title: str = Field(..., max_length=100, description="Brief PRD title")
    prd_description: str = Field(..., description="One paragraph summary")
    stories: List[StoryBreakdown] = Field(
        ..., min_length=1, description="Stories within this PRD"
    )


class VerificationOutput(BaseModel):
    """Claude's raw verification response schema.

    Note: The 'passed' field from Claude is OVERRIDDEN by fail-closed logic.
    If security_concerns or logic_errors are non-empty, passed is forced False.
    """

    passed: bool = Field(..., description="Whether verification passed")
    issues: List[str] = Field(default_factory=list, description="Issues found")
    security_concerns: List[str] = Field(
        default_factory=list, description="Security issues"
    )
    logic_errors: List[str] = Field(
        default_factory=list, description="Logic errors"
    )
    suggestions: List[str] = Field(
        default_factory=list, description="Improvement suggestions"
    )


class ExtractedLearning(BaseModel):
    """A single learning extracted by Claude from task output.

    Intermediate schema -- mapped to ``Learning`` for DB storage.
    """

    category: str = Field(
        ...,
        description="One of: pattern, pitfall, best_practice, "
        "project_context, debugging, architecture, testing, tool_usage",
    )
    title: str = Field(..., max_length=80, description="Brief learning title")
    content: str = Field(..., description="Full learning content")
    relevance_keywords: List[str] = Field(
        default_factory=list, description="Keywords for search matching"
    )
    confidence: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Confidence score"
    )


class LearningExtraction(BaseModel):
    """Claude's structured extraction of learnings from task output.

    Wrapper for the list of ``ExtractedLearning`` items returned
    by ``run_claude_structured()``.
    """

    learnings: List[ExtractedLearning] = Field(
        default_factory=list, description="Extracted learnings (0-5)"
    )


class PytestTestResult(BaseModel):
    """Parsed pytest JSON report summary.

    Subset of the ``pytest-json-report`` plugin output fields.
    """

    total: int = 0
    passed: int = 0
    failed: int = 0
    error: int = 0
    skipped: int = 0


class PytestJsonReport(BaseModel):
    """Top-level pytest-json-report output.

    Only the fields needed for quality gate parsing.
    """

    summary: PytestTestResult = Field(default_factory=PytestTestResult)
    exitcode: int = 0


class JestJsonReport(BaseModel):
    """Top-level Jest ``--json`` output.

    Only the fields needed for quality gate parsing.
    """

    numTotalTests: int = 0
    numPassedTests: int = 0
    numFailedTests: int = 0
    success: bool = False
