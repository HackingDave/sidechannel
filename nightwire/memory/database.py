"""SQLite database layer for the memory subsystem.

Manages all persistent storage for users, sessions, conversations,
preferences, explicit memories, and the autonomous system (PRDs,
stories, tasks, learnings). Optionally loads the sqlite-vec
extension for vector-based embedding search.

Key class:
    DatabaseConnection -- wraps a SQLite connection with async
        CRUD methods for every table. Uses threading.Lock for
        write safety and asyncio.to_thread for non-blocking I/O.

Module-level functions:
    get_database() -- returns the global singleton connection.
    initialize_database() -- creates, initializes, and returns
        the global singleton.

Schema version: 4 (auto-migrated on startup).
"""

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

import structlog

from .models import (
    Conversation,
    ExplicitMemory,
    Preference,
    SearchResult,
    Session,
    User,
)

logger = structlog.get_logger("nightwire.memory")

# Schema version for migrations
SCHEMA_VERSION = 5


class DatabaseConnection:
    """Manages a SQLite database connection and all CRUD operations.

    All public methods are async and delegate to synchronous helpers
    via ``asyncio.to_thread``. A ``threading.Lock`` guards write
    operations that require atomicity.

    Args:
        db_path: Filesystem path to the SQLite database file.
            Parent directories are created automatically.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._has_vec: bool = False
        self._lock = threading.Lock()

    async def initialize(self) -> None:
        """Initialize the database connection and create/migrate schema."""
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        """Synchronous initialization."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

        # Load sqlite-vec via the Python package API (works on all platforms
        # including aarch64 where raw load_extension("vec0") fails)
        try:
            import sqlite_vec
            sqlite_vec.load(self._conn)
            self._has_vec = True
            logger.info("sqlite_vec_loaded")
        except ImportError:
            logger.info("sqlite_vec_not_installed",
                        hint="pip install sqlite-vec for semantic search")
            self._has_vec = False
        except Exception as e:
            logger.warning("sqlite_vec_not_available", error=str(e))
            self._has_vec = False

        self._create_schema()
        logger.info("database_initialized", path=str(self.db_path), has_vec=self._has_vec)

    def _create_schema(self) -> None:
        """Create database tables."""
        cursor = self._conn.cursor()

        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                phone_number TEXT PRIMARY KEY,
                display_name TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                total_messages INTEGER DEFAULT 0
            )
        """)

        # Sessions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                phone_number TEXT NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                project_name TEXT,
                summary TEXT,
                message_count INTEGER DEFAULT 0,
                FOREIGN KEY (phone_number) REFERENCES users(phone_number)
            )
        """)

        # Conversations table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                project_name TEXT,
                command_type TEXT,
                metadata TEXT,
                embedding_id INTEGER,
                FOREIGN KEY (phone_number) REFERENCES users(phone_number),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        """)

        # Preferences table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                source_conversation_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP,
                use_count INTEGER DEFAULT 0,
                UNIQUE(phone_number, category, key),
                FOREIGN KEY (phone_number) REFERENCES users(phone_number),
                FOREIGN KEY (source_conversation_id) REFERENCES conversations(id)
            )
        """)

        # Explicit memories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS explicit_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                memory_text TEXT NOT NULL,
                tags TEXT,
                project_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                embedding_id INTEGER,
                FOREIGN KEY (phone_number) REFERENCES users(phone_number)
            )
        """)

        # Indexes for common queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_phone_time
            ON conversations(phone_number, timestamp DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_project
            ON conversations(phone_number, project_name)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_preferences_phone_category
            ON preferences(phone_number, category)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_phone
            ON sessions(phone_number)
        """)

        # Create vector table if sqlite-vec is available
        if self._has_vec:
            try:
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS embeddings USING vec0(
                        embedding float[384]
                    )
                """)
            except Exception as e:
                logger.warning("vec_table_creation_failed", error=str(e))

        # Schema version tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)

        # Check current version and migrate if needed
        cursor.execute("SELECT MAX(version) FROM schema_version")
        row = cursor.fetchone()
        current_version = row[0] if row and row[0] else 0

        if current_version < 2:
            self._migrate_to_v2(cursor)

        if current_version < 3:
            self._migrate_to_v3(cursor)

        if current_version < 4:
            self._migrate_to_v4(cursor)

        if current_version < 5:
            self._migrate_to_v5(cursor)

        # Update schema version
        cursor.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,)
        )

        self._conn.commit()

    def _migrate_to_v2(self, cursor: sqlite3.Cursor) -> None:
        """Migrate to schema version 2 - add autonomous task system tables."""
        logger.info("migrating_to_schema_v2")

        # PRDs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS prds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                project_name TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft', 'active', 'completed', 'archived')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                metadata TEXT,
                FOREIGN KEY (phone_number) REFERENCES users(phone_number)
            )
        """)

        # Stories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prd_id INTEGER NOT NULL,
                phone_number TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                acceptance_criteria TEXT,
                priority INTEGER DEFAULT 0,
                story_order INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'in_progress', 'completed', 'blocked', 'failed')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                embedding_id INTEGER,
                metadata TEXT,
                FOREIGN KEY (prd_id) REFERENCES prds(id) ON DELETE CASCADE,
                FOREIGN KEY (phone_number) REFERENCES users(phone_number)
            )
        """)

        # Tasks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id INTEGER NOT NULL,
                phone_number TEXT NOT NULL,
                project_name TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                task_order INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'queued', 'in_progress', 'running_tests',
                                     'completed', 'failed', 'blocked', 'cancelled')),
                priority INTEGER DEFAULT 0,
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 2,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT,
                claude_output TEXT,
                files_changed TEXT,
                quality_gate_results TEXT,
                embedding_id INTEGER,
                metadata TEXT,
                FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
                FOREIGN KEY (phone_number) REFERENCES users(phone_number)
            )
        """)

        # Learnings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                project_name TEXT,
                task_id INTEGER,
                category TEXT NOT NULL
                    CHECK(category IN ('pattern', 'pitfall', 'best_practice', 'project_context',
                                       'debugging', 'architecture', 'testing', 'tool_usage')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                relevance_keywords TEXT,
                usage_count INTEGER DEFAULT 0,
                confidence REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP,
                embedding_id INTEGER,
                is_active INTEGER DEFAULT 1,
                metadata TEXT,
                FOREIGN KEY (phone_number) REFERENCES users(phone_number),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
            )
        """)

        # Indexes for autonomous system
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_prds_phone_project
            ON prds(phone_number, project_name)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_stories_prd
            ON stories(prd_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_stories_status
            ON stories(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_story
            ON tasks(story_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status
            ON tasks(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_queued
            ON tasks(status, priority DESC, task_order ASC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_learnings_phone_project
            ON learnings(phone_number, project_name)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_learnings_category
            ON learnings(category)
        """)

        logger.info("schema_v2_migration_complete")

    def _migrate_to_v3(self, cursor: sqlite3.Cursor) -> None:
        """Migrate to schema version 3 - add project_name to explicit_memories."""
        logger.info("migrating_to_schema_v3")

        # Add project_name column to explicit_memories if it doesn't exist
        try:
            cursor.execute("ALTER TABLE explicit_memories ADD COLUMN project_name TEXT")
        except sqlite3.OperationalError:
            # Column already exists
            pass

        # Add index for project-specific memory queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_phone_project
            ON explicit_memories(phone_number, project_name)
        """)

        logger.info("schema_v3_migration_complete")

    def _migrate_to_v4(self, cursor: sqlite3.Cursor) -> None:
        """Migrate to schema version 4 - add parallel execution fields to tasks."""
        logger.info("migrating_to_schema_v4")

        for col, col_type in [
            ("effort_level", "TEXT"),
            ("task_type", "TEXT"),
            ("depends_on", "TEXT"),
            ("verification_result", "TEXT"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        logger.info("schema_v4_migration_complete")

    def _migrate_to_v5(self, cursor: sqlite3.Cursor) -> None:
        """Migrate to schema version 5 - add usage tracking table."""
        logger.info("migrating_to_schema_v5")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usage_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                project_name TEXT,
                model TEXT NOT NULL DEFAULT 'unknown',
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                source TEXT NOT NULL CHECK(source IN (
                    'do', 'ask', 'complex', 'summary', 'autonomous',
                    'verification', 'haiku', 'nightwire'
                )),
                session_id TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # -- No FK on phone_number: analytics table, referential integrity
        # -- not critical. REAL for cost_usd matches CLI's total_cost_usd
        # -- precision; acceptable for analytics.

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_phone_time
            ON usage_records(phone_number, timestamp DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_phone_project
            ON usage_records(phone_number, project_name)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_source
            ON usage_records(source, timestamp DESC)
        """)

        logger.info("schema_v5_migration_complete")

    @property
    def has_vector_search(self) -> bool:
        """Check if vector search is available."""
        return self._has_vec

    def _parse_sqlite_timestamp(self, ts_str: Optional[str]) -> datetime:
        """Parse SQLite CURRENT_TIMESTAMP format."""
        if not ts_str:
            logger.debug("timestamp_null_fallback",
                         note="using datetime.now() for null timestamp")
            return datetime.now()
        try:
            # Try SQLite format first: 'YYYY-MM-DD HH:MM:SS'
            return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # Fall back to ISO format
            return datetime.fromisoformat(ts_str)

    def _format_sqlite_timestamp(self, dt: datetime) -> str:
        """Format datetime for SQLite comparison."""
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            def _close_with_lock():
                with self._lock:
                    self._conn.close()
            await asyncio.to_thread(_close_with_lock)
            self._conn = None

    # User operations
    async def ensure_user(self, phone_number: str) -> User:
        """Get an existing user or create a new one.

        Args:
            phone_number: E.164 phone number (primary key).

        Returns:
            The existing or newly created User record.
        """
        return await asyncio.to_thread(self._ensure_user_sync, phone_number)

    def _ensure_user_sync(self, phone_number: str) -> User:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT * FROM users WHERE phone_number = ?",
                (phone_number,)
            )
            row = cursor.fetchone()

            if row:
                return User(
                    phone_number=row["phone_number"],
                    display_name=row["display_name"],
                    first_seen=self._parse_sqlite_timestamp(row["first_seen"]),
                    last_active=self._parse_sqlite_timestamp(row["last_active"]),
                    total_messages=row["total_messages"]
                )

            # Create new user
            cursor.execute(
                "INSERT INTO users (phone_number) VALUES (?)",
                (phone_number,)
            )
            self._conn.commit()
            return User(phone_number=phone_number)

    async def update_user_activity(self, phone_number: str) -> None:
        """Update user's last activity and message count."""
        await asyncio.to_thread(self._update_user_activity_sync, phone_number)

    def _update_user_activity_sync(self, phone_number: str) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("""
                UPDATE users
                SET last_active = CURRENT_TIMESTAMP, total_messages = total_messages + 1
                WHERE phone_number = ?
            """, (phone_number,))
            self._conn.commit()

    # Session operations
    async def get_or_create_session(
        self,
        phone_number: str,
        project_name: Optional[str] = None,
        timeout_minutes: int = 30
    ) -> Session:
        """Get the active session or create a new one if expired.

        A session expires after ``timeout_minutes`` of inactivity.

        Args:
            phone_number: User's phone number.
            project_name: Current project context.
            timeout_minutes: Inactivity threshold for session
                expiration.

        Returns:
            The active or newly created Session.
        """
        return await asyncio.to_thread(
            self._get_or_create_session_sync,
            phone_number,
            project_name,
            timeout_minutes
        )

    def _get_or_create_session_sync(
        self,
        phone_number: str,
        project_name: Optional[str],
        timeout_minutes: int
    ) -> Session:
        with self._lock:
            cursor = self._conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)

            # Find active session
            cursor.execute("""
                SELECT * FROM sessions
                WHERE phone_number = ? AND ended_at IS NULL AND started_at > ?
                ORDER BY started_at DESC LIMIT 1
            """, (phone_number, self._format_sqlite_timestamp(cutoff)))
            row = cursor.fetchone()

            if row:
                return Session(
                    id=row["id"],
                    phone_number=row["phone_number"],
                    started_at=self._parse_sqlite_timestamp(row["started_at"]),
                    project_name=row["project_name"],
                    message_count=row["message_count"]
                )

            # Create new session
            session_id = str(uuid.uuid4())
            cursor.execute("""
                INSERT INTO sessions (id, phone_number, project_name)
                VALUES (?, ?, ?)
            """, (session_id, phone_number, project_name))
            self._conn.commit()

            return Session(
                id=session_id,
                phone_number=phone_number,
                project_name=project_name
            )

    async def update_session_count(self, session_id: str) -> None:
        """Increment session message count."""
        await asyncio.to_thread(self._update_session_count_sync, session_id)

    def _update_session_count_sync(self, session_id: str) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,)
            )
            self._conn.commit()

    # Conversation operations
    async def store_conversation(
        self,
        phone_number: str,
        session_id: str,
        role: str,
        content: str,
        project_name: Optional[str] = None,
        command_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None
    ) -> int:
        """Store a conversation message.

        Args:
            phone_number: User's phone number.
            session_id: UUID of the owning session.
            role: ``'user'`` or ``'assistant'``.
            content: Message text.
            project_name: Project context, if any.
            command_type: Command that produced this message.
            metadata: Extra data (tokens, latency, etc.).

        Returns:
            The auto-incremented conversation ID.
        """
        return await asyncio.to_thread(
            self._store_conversation_sync,
            phone_number,
            session_id,
            role,
            content,
            project_name,
            command_type,
            metadata
        )

    def _store_conversation_sync(
        self,
        phone_number: str,
        session_id: str,
        role: str,
        content: str,
        project_name: Optional[str],
        command_type: Optional[str],
        metadata: Optional[dict[str, Any]]
    ) -> int:
        with self._lock:
            cursor = self._conn.cursor()
            metadata_json = json.dumps(metadata) if metadata else None

            cursor.execute("""
                INSERT INTO conversations
                (phone_number, session_id, role, content, project_name, command_type, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                phone_number, session_id, role, content,
                project_name, command_type, metadata_json,
            ))
            self._conn.commit()

            return cursor.lastrowid

    async def get_history(
        self,
        phone_number: str,
        limit: int = 20,
        before: Optional[datetime] = None,
        project_name: Optional[str] = None
    ) -> List[Conversation]:
        """Get conversation history for a user."""
        return await asyncio.to_thread(
            self._get_history_sync,
            phone_number,
            limit,
            before,
            project_name
        )

    def _get_history_sync(
        self,
        phone_number: str,
        limit: int,
        before: Optional[datetime],
        project_name: Optional[str]
    ) -> List[Conversation]:
        cursor = self._conn.cursor()

        query = "SELECT * FROM conversations WHERE phone_number = ?"
        params: list = [phone_number]

        if before:
            query += " AND timestamp < ?"
            params.append(self._format_sqlite_timestamp(before))

        if project_name:
            query += " AND project_name = ?"
            params.append(project_name)

        # Sort by timestamp DESC, id DESC to get most recent first (id breaks ties)
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        conversations = []
        for row in rows:
            metadata = json.loads(row["metadata"]) if row["metadata"] else None
            conversations.append(Conversation(
                id=row["id"],
                phone_number=row["phone_number"],
                session_id=row["session_id"],
                timestamp=self._parse_sqlite_timestamp(row["timestamp"]),
                role=row["role"],
                content=row["content"],
                project_name=row["project_name"],
                command_type=row["command_type"],
                metadata=metadata,
                embedding_id=row["embedding_id"]
            ))

        # Return in chronological order (reverse the DESC order)
        return list(reversed(conversations))

    # Preference operations
    async def store_preference(
        self,
        phone_number: str,
        category: str,
        key: str,
        value: str,
        source_conversation_id: Optional[int] = None,
        confidence: float = 1.0
    ) -> int:
        """Store or update a user preference (upsert).

        Args:
            phone_number: User's phone number.
            category: One of 'style', 'project', 'personal',
                'technical'.
            key: Preference key (unique per user+category).
            value: Preference value.
            source_conversation_id: Conversation that sourced
                this preference.
            confidence: Confidence score (0.0 to 1.0).

        Returns:
            The preference row ID.
        """
        return await asyncio.to_thread(
            self._store_preference_sync,
            phone_number,
            category,
            key,
            value,
            source_conversation_id,
            confidence
        )

    def _store_preference_sync(
        self,
        phone_number: str,
        category: str,
        key: str,
        value: str,
        source_conversation_id: Optional[int],
        confidence: float
    ) -> int:
        with self._lock:
            cursor = self._conn.cursor()

            cursor.execute("""
                INSERT INTO preferences
                (phone_number, category, key, value, source_conversation_id, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone_number, category, key) DO UPDATE SET
                    value = excluded.value,
                    confidence = excluded.confidence,
                    last_used = CURRENT_TIMESTAMP,
                    use_count = use_count + 1
            """, (phone_number, category, key, value, source_conversation_id, confidence))
            self._conn.commit()

            return cursor.lastrowid

    async def get_preferences(
        self,
        phone_number: str,
        category: Optional[str] = None
    ) -> List[Preference]:
        """Get user preferences."""
        return await asyncio.to_thread(
            self._get_preferences_sync,
            phone_number,
            category
        )

    def _get_preferences_sync(
        self,
        phone_number: str,
        category: Optional[str]
    ) -> List[Preference]:
        cursor = self._conn.cursor()

        if category:
            cursor.execute(
                "SELECT * FROM preferences WHERE phone_number = ? AND category = ?",
                (phone_number, category)
            )
        else:
            cursor.execute(
                "SELECT * FROM preferences WHERE phone_number = ?",
                (phone_number,)
            )

        rows = cursor.fetchall()
        return [
            Preference(
                id=row["id"],
                phone_number=row["phone_number"],
                category=row["category"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                source_conversation_id=row["source_conversation_id"],
                created_at=self._parse_sqlite_timestamp(row["created_at"]),
                last_used=(
                    self._parse_sqlite_timestamp(row["last_used"])
                    if row["last_used"] else None
                ),
                use_count=row["use_count"]
            )
            for row in rows
        ]

    # Explicit memory operations
    async def store_memory(
        self,
        phone_number: str,
        memory_text: str,
        tags: Optional[List[str]] = None,
        project_name: Optional[str] = None
    ) -> int:
        """Store an explicit memory from the /remember command.

        Args:
            phone_number: User's phone number.
            memory_text: The text to remember.
            tags: Optional categorization tags (stored as JSON).
            project_name: Optional project association.

        Returns:
            The memory row ID.
        """
        return await asyncio.to_thread(
            self._store_memory_sync,
            phone_number,
            memory_text,
            tags,
            project_name
        )

    def _store_memory_sync(
        self,
        phone_number: str,
        memory_text: str,
        tags: Optional[List[str]],
        project_name: Optional[str]
    ) -> int:
        with self._lock:
            cursor = self._conn.cursor()
            tags_json = json.dumps(tags) if tags else None

            cursor.execute("""
                INSERT INTO explicit_memories (phone_number, memory_text, tags, project_name)
                VALUES (?, ?, ?, ?)
            """, (phone_number, memory_text, tags_json, project_name))
            self._conn.commit()

            return cursor.lastrowid

    async def get_memories(
        self,
        phone_number: str,
        limit: int = 50,
        project_name: Optional[str] = None
    ) -> List[ExplicitMemory]:
        """Get explicit memories for a user, optionally filtered by project."""
        return await asyncio.to_thread(
            self._get_memories_sync,
            phone_number,
            limit,
            project_name
        )

    def _get_memories_sync(
        self,
        phone_number: str,
        limit: int,
        project_name: Optional[str]
    ) -> List[ExplicitMemory]:
        cursor = self._conn.cursor()

        if project_name:
            cursor.execute("""
                SELECT * FROM explicit_memories
                WHERE phone_number = ? AND project_name = ?
                ORDER BY created_at DESC LIMIT ?
            """, (phone_number, project_name, limit))
        else:
            cursor.execute("""
                SELECT * FROM explicit_memories
                WHERE phone_number = ?
                ORDER BY created_at DESC LIMIT ?
            """, (phone_number, limit))

        rows = cursor.fetchall()
        return [
            ExplicitMemory(
                id=row["id"],
                phone_number=row["phone_number"],
                memory_text=row["memory_text"],
                tags=json.loads(row["tags"]) if row["tags"] else None,
                created_at=self._parse_sqlite_timestamp(row["created_at"]),
                embedding_id=row["embedding_id"],
                project_name=row["project_name"] if "project_name" in row.keys() else None
            )
            for row in rows
        ]

    # Deletion operations
    async def delete_all_user_data(self, phone_number: str) -> int:
        """Delete all data for a user across all tables.

        Removes conversations, preferences, memories, autonomous
        data (learnings, tasks, stories, PRDs), sessions, and the
        user record itself.

        Args:
            phone_number: User's phone number.

        Returns:
            Total number of deleted rows across all tables.
        """
        return await asyncio.to_thread(self._delete_all_user_data_sync, phone_number)

    def _delete_all_user_data_sync(self, phone_number: str) -> int:
        with self._lock:
            cursor = self._conn.cursor()
            total = 0

            # Delete orphaned embeddings BEFORE deleting conversations/memories
            # (embeddings are referenced by embedding_id in both tables)
            if self._has_vec:
                cursor.execute("""
                    DELETE FROM embeddings WHERE rowid IN (
                        SELECT embedding_id FROM conversations
                        WHERE phone_number = ? AND embedding_id IS NOT NULL
                        UNION
                        SELECT embedding_id FROM explicit_memories
                        WHERE phone_number = ? AND embedding_id IS NOT NULL
                    )
                """, (phone_number, phone_number))
                total += cursor.rowcount

            cursor.execute(
                "DELETE FROM conversations WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount

            cursor.execute(
                "DELETE FROM preferences WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount

            cursor.execute(
                "DELETE FROM explicit_memories WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount

            # Delete autonomous system data
            cursor.execute(
                "DELETE FROM learnings WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount
            cursor.execute(
                "DELETE FROM tasks WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount
            cursor.execute(
                "DELETE FROM stories WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount
            cursor.execute(
                "DELETE FROM prds WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount

            cursor.execute(
                "DELETE FROM sessions WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount

            cursor.execute(
                "DELETE FROM users WHERE phone_number = ?", (phone_number,)
            )
            total += cursor.rowcount

            self._conn.commit()
            return total

    async def delete_preferences(self, phone_number: str) -> int:
        """Delete all preferences for a user."""
        return await asyncio.to_thread(self._delete_preferences_sync, phone_number)

    def _delete_preferences_sync(self, phone_number: str) -> int:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("DELETE FROM preferences WHERE phone_number = ?", (phone_number,))
            self._conn.commit()
            return cursor.rowcount

    async def delete_today_conversations(self, phone_number: str) -> int:
        """Delete today's conversations for a user."""
        return await asyncio.to_thread(self._delete_today_sync, phone_number)

    def _delete_today_sync(self, phone_number: str) -> int:
        with self._lock:
            return self._delete_today_sync_inner(phone_number)

    def _delete_today_sync_inner(self, phone_number: str) -> int:
        cursor = self._conn.cursor()
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        cursor.execute("""
            DELETE FROM conversations
            WHERE phone_number = ? AND timestamp >= ?
        """, (phone_number, self._format_sqlite_timestamp(today)))
        self._conn.commit()
        return cursor.rowcount

    # Embedding operations (for vector search)
    async def store_embedding(self, embedding: List[float]) -> Optional[int]:
        """Store an embedding vector in the sqlite-vec table.

        Args:
            embedding: Float vector (384 dimensions for MiniLM).

        Returns:
            The embedding row ID, or None if sqlite-vec is not
            available.
        """
        if not self._has_vec:
            return None
        return await asyncio.to_thread(self._store_embedding_sync, embedding)

    def _store_embedding_sync(self, embedding: List[float]) -> int:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "INSERT INTO embeddings (embedding) VALUES (?)",
                (json.dumps(embedding),)
            )
            self._conn.commit()
            return cursor.lastrowid

    async def update_conversation_embedding(
        self,
        conversation_id: int,
        embedding_id: int
    ) -> None:
        """Link a conversation to its embedding."""
        await asyncio.to_thread(
            self._update_conversation_embedding_sync,
            conversation_id,
            embedding_id
        )

    def _update_conversation_embedding_sync(
        self,
        conversation_id: int,
        embedding_id: int
    ) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "UPDATE conversations SET embedding_id = ? WHERE id = ?",
                (embedding_id, conversation_id)
            )
            self._conn.commit()

    async def search_by_embedding(
        self,
        phone_number: str,
        query_embedding: List[float],
        limit: int = 10
    ) -> List[SearchResult]:
        """Search conversations by embedding cosine similarity.

        Uses sqlite-vec's ``vec_distance_cosine`` for ranked
        retrieval. Returns empty list if sqlite-vec is unavailable.

        Args:
            phone_number: User's phone number (scope filter).
            query_embedding: Query vector to compare against.
            limit: Maximum results to return.

        Returns:
            Search results ranked by similarity (highest first).
        """
        if not self._has_vec:
            return []
        return await asyncio.to_thread(
            self._search_by_embedding_sync,
            phone_number,
            query_embedding,
            limit
        )

    def _search_by_embedding_sync(
        self,
        phone_number: str,
        query_embedding: List[float],
        limit: int
    ) -> List[SearchResult]:
        cursor = self._conn.cursor()

        # Join embeddings with conversations and filter by user
        cursor.execute("""
            SELECT c.id, c.content, c.role, c.timestamp, c.project_name,
                   vec_distance_cosine(e.embedding, ?) as distance
            FROM conversations c
            JOIN embeddings e ON c.embedding_id = e.rowid
            WHERE c.phone_number = ?
            ORDER BY distance ASC
            LIMIT ?
        """, (json.dumps(query_embedding), phone_number, limit))

        rows = cursor.fetchall()
        return [
            SearchResult(
                id=row["id"],
                content=row["content"],
                role=row["role"],
                timestamp=(
                    self._parse_sqlite_timestamp(row["timestamp"])
                    or datetime.now(timezone.utc)
                ),
                project_name=row["project_name"],
                similarity_score=max(0.0, min(1.0, 1 - row["distance"])),  # Clamp to [0, 1]
                source_type="conversation"
            )
            for row in rows
        ]

    # Usage tracking operations

    async def record_usage(
        self,
        phone_number: str,
        source: str,
        model: str = "unknown",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        project_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Record a usage entry for token/cost tracking.

        Args:
            phone_number: User's phone number.
            source: One of 'do', 'ask', 'complex', 'summary',
                'autonomous', 'verification', 'haiku', 'nightwire'.
            model: Model identifier.
            input_tokens: Input/prompt token count.
            output_tokens: Output/completion token count.
            cost_usd: Cost in USD (float, matches CLI precision).
            project_name: Project context.
            session_id: CLI session ID, if available.

        Returns:
            The usage record row ID.
        """
        return await asyncio.to_thread(
            self._record_usage_sync,
            phone_number, source, model, input_tokens,
            output_tokens, cost_usd, project_name, session_id,
        )

    def _record_usage_sync(
        self,
        phone_number: str,
        source: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        project_name: Optional[str],
        session_id: Optional[str],
    ) -> int:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("""
                INSERT INTO usage_records
                (phone_number, project_name, model, input_tokens,
                 output_tokens, cost_usd, source, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                phone_number, project_name, model, input_tokens,
                output_tokens, cost_usd, source, session_id,
            ))
            self._conn.commit()
            return cursor.lastrowid

    async def get_usage_summary(
        self,
        phone_number: str,
        days: Optional[int] = None,
    ) -> dict:
        """Get usage summary for a user, optionally filtered by time.

        Args:
            phone_number: User's phone number.
            days: If set, only include records from the last N days.
                None means all time.

        Returns:
            Dict with keys: input_tokens, output_tokens, cost_usd,
            request_count.
        """
        return await asyncio.to_thread(
            self._get_usage_summary_sync, phone_number, days,
        )

    def _get_usage_summary_sync(
        self, phone_number: str, days: Optional[int],
    ) -> dict:
        cursor = self._conn.cursor()
        query = """
            SELECT COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens,
                   COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                   COUNT(*) as request_count
            FROM usage_records
            WHERE phone_number = ?
        """
        params: list = [phone_number]
        if days is not None:
            if days == 0:
                query += " AND timestamp >= datetime('now', 'start of day')"
            else:
                query += " AND timestamp >= datetime('now', ?)"
                params.append(f"-{days} days")
        cursor.execute(query, params)
        row = cursor.fetchone()
        return {
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cost_usd": row["cost_usd"],
            "request_count": row["request_count"],
        }

    async def get_usage_by_project(
        self,
        phone_number: str,
        days: Optional[int] = None,
    ) -> List[dict]:
        """Get per-project usage breakdown for a user.

        Args:
            phone_number: User's phone number.
            days: If set, only include records from the last N days.

        Returns:
            List of dicts with project_name, input_tokens,
            output_tokens, cost_usd, request_count.
        """
        return await asyncio.to_thread(
            self._get_usage_by_project_sync, phone_number, days,
        )

    def _get_usage_by_project_sync(
        self, phone_number: str, days: Optional[int],
    ) -> List[dict]:
        cursor = self._conn.cursor()
        query = """
            SELECT COALESCE(project_name, '(no project)') as project_name,
                   COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens,
                   COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                   COUNT(*) as request_count
            FROM usage_records
            WHERE phone_number = ?
        """
        params: list = [phone_number]
        if days is not None:
            if days == 0:
                query += " AND timestamp >= datetime('now', 'start of day')"
            else:
                query += " AND timestamp >= datetime('now', ?)"
                params.append(f"-{days} days")
        query += " GROUP BY project_name ORDER BY cost_usd DESC"
        cursor.execute(query, params)
        return [
            {
                "project_name": row["project_name"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost_usd": row["cost_usd"],
                "request_count": row["request_count"],
            }
            for row in cursor.fetchall()
        ]

    async def get_usage_all_users(
        self, days: Optional[int] = None,
    ) -> List[dict]:
        """Get usage summary for all users (admin query).

        Args:
            days: If set, only include records from the last N days.

        Returns:
            List of dicts with phone_number, input_tokens,
            output_tokens, cost_usd, request_count.
        """
        return await asyncio.to_thread(
            self._get_usage_all_users_sync, days,
        )

    def _get_usage_all_users_sync(self, days: Optional[int]) -> List[dict]:
        cursor = self._conn.cursor()
        query = """
            SELECT phone_number,
                   COALESCE(SUM(input_tokens), 0) as input_tokens,
                   COALESCE(SUM(output_tokens), 0) as output_tokens,
                   COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                   COUNT(*) as request_count
            FROM usage_records
        """
        params: list = []
        if days is not None:
            if days == 0:
                query += " WHERE timestamp >= datetime('now', 'start of day')"
            else:
                query += " WHERE timestamp >= datetime('now', ?)"
                params.append(f"-{days} days")
        query += " GROUP BY phone_number ORDER BY cost_usd DESC"
        cursor.execute(query, params)
        return [
            {
                "phone_number": row["phone_number"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost_usd": row["cost_usd"],
                "request_count": row["request_count"],
            }
            for row in cursor.fetchall()
        ]

    async def get_usage_cost_since(
        self,
        phone_number: str,
        days: int,
    ) -> float:
        """Get total cost for a user in the last N days.

        Used by budget alert checks.

        Args:
            phone_number: User's phone number.
            days: Number of days to look back.

        Returns:
            Total cost in USD.
        """
        summary = await self.get_usage_summary(phone_number, days)
        return summary["cost_usd"]


# Global database instance
_db: Optional[DatabaseConnection] = None


def get_database(db_path: Optional[Path] = None) -> DatabaseConnection:
    """Get or create the global database singleton.

    Args:
        db_path: Required on first call to set the database path.

    Returns:
        The global DatabaseConnection instance.

    Raises:
        ValueError: If called for the first time without a
            db_path.
    """
    global _db
    if _db is None:
        if db_path is None:
            raise ValueError("db_path required for first initialization")
        _db = DatabaseConnection(db_path)
    return _db


async def initialize_database(db_path: Path) -> DatabaseConnection:
    """Initialize and return the global database singleton.

    Closes any existing connection before creating a new one.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        The initialized DatabaseConnection.
    """
    global _db
    if _db is not None:
        try:
            await _db.close()
        except Exception:
            pass
    _db = DatabaseConnection(db_path)
    await _db.initialize()
    return _db
