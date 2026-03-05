"""Central coordinator for the autonomous task system.

Provides the high-level API used by Signal command handlers to
manage PRDs, stories, tasks, learnings, and the processing loop.
Wires together the database, executor, quality runner, learning
extractor, and autonomous loop into a single facade.

Classes:
    AutonomousManager: Facade over all autonomous subsystem
        components. Created once in ``SignalBot.__init__`` and
        exposed to command handlers via ``BotContext``.
"""

import sqlite3
from typing import Awaitable, Callable, List, Optional

import structlog

from .database import AutonomousDatabase
from .executor import TaskExecutor
from .learnings import LearningExtractor
from .loop import AutonomousLoop
from .models import (
    PRD,
    Learning,
    LearningCategory,
    LoopStatus,
    PRDStatus,
    Story,
    StoryStatus,
    Task,
    TaskStatus,
)
from .quality_gates import QualityGateRunner

logger = structlog.get_logger("nightwire.autonomous")


class AutonomousManager:
    """Central coordinator for all autonomous system components.

    Facade that composes the database, executor, quality gates,
    learning extractor, and autonomous loop. All public methods
    delegate to the appropriate subsystem component.
    """

    def __init__(
        self,
        db_connection: sqlite3.Connection,
        progress_callback: Optional[Callable[[str, str], Awaitable[None]]] = None,
        poll_interval: int = 30,
        run_quality_gates: bool = True,
        max_parallel: int = 3,
        usage_recorder: Optional[Callable[..., Awaitable[None]]] = None,
    ):
        """
        Initialize the autonomous manager.

        Args:
            db_connection: SQLite connection from memory system
            progress_callback: Async callback(phone_number, message) for notifications
            poll_interval: Seconds between queue polls
            run_quality_gates: Whether to run tests/typecheck after tasks
            max_parallel: Maximum concurrent task workers (default 3, max 10)
            usage_recorder: Async callback to record usage data per task.
        """
        self.db = AutonomousDatabase(db_connection)
        self.quality_runner = QualityGateRunner()
        self.learning_extractor = LearningExtractor()
        self.executor = TaskExecutor(
            db=self.db,
            quality_runner=self.quality_runner,
            learning_extractor=self.learning_extractor,
            run_quality_gates=run_quality_gates,
        )
        self.loop = AutonomousLoop(
            db=self.db,
            executor=self.executor,
            progress_callback=progress_callback,
            poll_interval=poll_interval,
            max_parallel=max_parallel,
            usage_recorder=usage_recorder,
        )

        self._progress_callback = progress_callback

    # ========== Loop Control ==========

    async def start_loop(self) -> None:
        """Start the autonomous processing loop."""
        await self.loop.start()

    async def stop_loop(self) -> None:
        """Stop the autonomous processing loop."""
        await self.loop.stop()

    async def pause_loop(self) -> None:
        """Pause the autonomous processing loop."""
        await self.loop.pause()

    async def resume_loop(self) -> None:
        """Resume the autonomous processing loop."""
        await self.loop.resume()

    async def get_loop_status(self) -> LoopStatus:
        """Get current loop status."""
        return await self.loop.get_status()

    # ========== PRD Management ==========

    async def create_prd(
        self,
        phone_number: str,
        project_name: str,
        title: str,
        description: str,
    ) -> PRD:
        """Create a new PRD in DRAFT status.

        Args:
            phone_number: Owner's phone number (E.164).
            project_name: Associated project name.
            title: PRD title.
            description: Full PRD description.

        Returns:
            The created PRD with its assigned database ID.
        """
        prd = await self.db.create_prd(
            phone_number=phone_number,
            project_name=project_name,
            title=title,
            description=description,
            status=PRDStatus.DRAFT,
        )
        logger.info("prd_created", prd_id=prd.id, title=title)
        return prd

    async def get_prd(self, prd_id: int) -> Optional[PRD]:
        """Get a PRD by ID."""
        return await self.db.get_prd(prd_id)

    async def list_prds(
        self,
        phone_number: str,
        project_name: Optional[str] = None,
    ) -> List[PRD]:
        """List PRDs for a user."""
        return await self.db.list_prds(phone_number, project_name)

    async def activate_prd(self, prd_id: int) -> None:
        """Activate a PRD for processing."""
        await self.db.update_prd_status(prd_id, PRDStatus.ACTIVE)
        logger.info("prd_activated", prd_id=prd_id)

    async def archive_prd(self, prd_id: int) -> None:
        """Archive a PRD."""
        await self.db.update_prd_status(prd_id, PRDStatus.ARCHIVED)
        logger.info("prd_archived", prd_id=prd_id)

    # ========== Story Management ==========

    async def create_story(
        self,
        prd_id: int,
        phone_number: str,
        title: str,
        description: str,
        acceptance_criteria: Optional[List[str]] = None,
        priority: int = 0,
    ) -> Story:
        """Create a new story in a PRD.

        Args:
            prd_id: Parent PRD database ID.
            phone_number: Owner's phone number.
            title: Story title.
            description: Story description.
            acceptance_criteria: Verification checklist.
            priority: Execution priority (higher = first).

        Returns:
            The created Story with its assigned database ID.
        """
        story = await self.db.create_story(
            prd_id=prd_id,
            phone_number=phone_number,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            priority=priority,
        )
        logger.info("story_created", story_id=story.id, prd_id=prd_id, title=title)
        return story

    async def get_story(self, story_id: int) -> Optional[Story]:
        """Get a story by ID."""
        return await self.db.get_story(story_id)

    async def list_stories(
        self,
        prd_id: Optional[int] = None,
        phone_number: Optional[str] = None,
    ) -> List[Story]:
        """List stories with optional filters."""
        return await self.db.list_stories(prd_id=prd_id, phone_number=phone_number)

    # ========== Task Management ==========

    async def create_task(
        self,
        story_id: int,
        phone_number: str,
        project_name: str,
        title: str,
        description: str,
        priority: int = 0,
        depends_on: Optional[list] = None,
    ) -> Task:
        """Create a new task in a story.

        Args:
            story_id: Parent story database ID.
            phone_number: Owner's phone number.
            project_name: Project to execute in.
            title: Task title.
            description: Detailed task description.
            priority: Execution priority (higher = first).
            depends_on: Task IDs this task depends on.

        Returns:
            The created Task with its assigned database ID.
        """
        task = await self.db.create_task(
            story_id=story_id,
            phone_number=phone_number,
            project_name=project_name,
            title=title,
            description=description,
            priority=priority,
            depends_on=depends_on,
        )
        logger.info("task_created", task_id=task.id, story_id=story_id, title=title)
        return task

    async def get_task(self, task_id: int) -> Optional[Task]:
        """Get a task by ID."""
        return await self.db.get_task(task_id)

    async def list_tasks(
        self,
        story_id: Optional[int] = None,
        phone_number: Optional[str] = None,
        project_name: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> List[Task]:
        """List tasks with optional filters."""
        return await self.db.list_tasks(
            story_id=story_id,
            phone_number=phone_number,
            project_name=project_name,
            status=status,
        )

    async def queue_story(self, story_id: int) -> int:
        """Queue all pending tasks for a story.

        Also transitions the story to IN_PROGRESS status.

        Args:
            story_id: Database ID of the story.

        Returns:
            Number of tasks queued.
        """
        count = await self.db.queue_tasks_for_story(story_id)

        # Update story status to in_progress
        await self.db.update_story_status(story_id, StoryStatus.IN_PROGRESS)

        logger.info("story_queued", story_id=story_id, tasks_queued=count)
        return count

    async def queue_prd(self, prd_id: int) -> int:
        """Queue all pending tasks across all stories in a PRD.

        Activates the PRD if it is still in DRAFT status.

        Args:
            prd_id: Database ID of the PRD.

        Returns:
            Number of tasks queued.
        """
        count = await self.db.queue_tasks_for_prd(prd_id)

        # Update PRD status to active if not already
        prd = await self.db.get_prd(prd_id)
        if prd and prd.status == PRDStatus.DRAFT:
            await self.db.update_prd_status(prd_id, PRDStatus.ACTIVE)

        logger.info("prd_queued", prd_id=prd_id, tasks_queued=count)
        return count

    async def get_task_stats(
        self,
        phone_number: str,
        project_name: Optional[str] = None,
    ) -> dict:
        """Get task statistics for a user."""
        return await self.db.get_task_stats(phone_number, project_name)

    # ========== Learning Management ==========

    async def add_learning(
        self,
        phone_number: str,
        category: LearningCategory,
        title: str,
        content: str,
        project_name: Optional[str] = None,
    ) -> int:
        """Manually add a learning with full confidence.

        Keywords are auto-extracted from content.

        Args:
            phone_number: Owner's phone number.
            category: Learning category.
            title: Brief learning title.
            content: Full learning content.
            project_name: Associated project (optional).

        Returns:
            Database ID of the new learning.
        """
        learning = Learning(
            phone_number=phone_number,
            project_name=project_name,
            category=category,
            title=title,
            content=content,
            relevance_keywords=self.learning_extractor._extract_keywords(content),
            confidence=1.0,  # Manual learnings have high confidence
        )
        learning_id = await self.db.store_learning(learning)
        logger.info("learning_added", learning_id=learning_id, category=category.value)
        return learning_id

    async def get_learnings(
        self,
        phone_number: str,
        project_name: Optional[str] = None,
        category: Optional[LearningCategory] = None,
        limit: int = 50,
    ) -> List[Learning]:
        """Get learnings with optional filters."""
        return await self.db.get_learnings(
            phone_number=phone_number,
            project_name=project_name,
            category=category,
            limit=limit,
        )

    async def search_learnings(
        self,
        phone_number: str,
        query: str,
        project_name: Optional[str] = None,
        limit: int = 10,
    ) -> List[Learning]:
        """Search learnings by keyword relevance.

        Args:
            phone_number: Owner's phone number.
            query: Free-text search query.
            project_name: Filter by project (optional).
            limit: Maximum results to return.

        Returns:
            Learnings sorted by relevance score descending.
        """
        return await self.db.get_relevant_learnings(
            phone_number=phone_number,
            project_name=project_name,
            query=query,
            limit=limit,
        )

    async def decay_learnings(self, days_threshold: int = 30) -> int:
        """Decay confidence of unused learnings by 10%.

        Args:
            days_threshold: Days of inactivity before decay.

        Returns:
            Number of learnings whose confidence was reduced.
        """
        count = await self.db.decay_unused_learnings(days_threshold)
        if count > 0:
            logger.info("learnings_decayed", count=count, days_threshold=days_threshold)
        return count
