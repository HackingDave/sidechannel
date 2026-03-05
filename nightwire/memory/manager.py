"""Central coordinator for all memory operations.

Provides the high-level API for the memory subsystem. Orchestrates
database storage, embedding generation, semantic search, context
building, and Haiku-based summarization.

Key class:
    MemoryManager -- stores messages, manages preferences/memories,
        performs semantic search, and builds context for prompt
        injection.

Module-level functions:
    get_memory_manager() -- returns the global singleton instance.
    initialize_memory_manager() -- creates and initializes the
        global singleton.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import structlog

from .database import DatabaseConnection, initialize_database
from .embeddings import EmbeddingService
from .models import (
    Conversation,
    ExplicitMemory,
    Preference,
    SearchResult,
)

logger = structlog.get_logger("nightwire.memory")


class MemoryManager:
    """Central coordinator for all memory operations.

    Provides high-level methods for storing conversations, retrieving context,
    and managing user preferences and memories.
    """

    def __init__(
        self,
        db_path: Path,
        session_timeout_minutes: int = 30,
        max_context_tokens: int = 1500,
        embedding_model: str = "all-MiniLM-L6-v2",
        enable_embeddings: bool = True
    ):
        """Initialize the memory manager (call initialize() before use).

        Args:
            db_path: Path to the SQLite database file.
            session_timeout_minutes: Minutes of inactivity before
                a new session is created.
            max_context_tokens: Token budget for context injection.
            embedding_model: sentence-transformers model name.
            enable_embeddings: If False, disables vector search
                and falls back to keyword matching.
        """
        self.db_path = db_path
        self.session_timeout = session_timeout_minutes
        self.max_context_tokens = max_context_tokens
        self._db: Optional[DatabaseConnection] = None
        self._embeddings: Optional[EmbeddingService] = None
        self._embedding_model = embedding_model
        self._enable_embeddings = enable_embeddings
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the database and embedding service.

        Thread-safe via double-checked locking. Safe to call
        multiple times; subsequent calls are no-ops.

        Raises:
            Exception: If database initialization fails.
        """
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._initialize_unlocked()

    async def _initialize_unlocked(self) -> None:
        """Internal initialization (caller must hold _init_lock)."""
        self._db = await initialize_database(self.db_path)

        # Initialize embeddings if enabled
        if self._enable_embeddings:
            try:
                self._embeddings = EmbeddingService(self._embedding_model)
                logger.info("embeddings_enabled", model=self._embedding_model)
            except Exception as e:
                logger.warning("embeddings_disabled", error=str(e))
                self._embeddings = None

        self._initialized = True
        logger.info("memory_manager_initialized", db_path=str(self.db_path))

    async def _ensure_initialized(self) -> None:
        """Ensure the database is initialized (thread-safe)."""
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                await self._initialize_unlocked()

    @property
    def db(self) -> DatabaseConnection:
        """Get the database connection."""
        if self._db is None:
            raise RuntimeError("MemoryManager not initialized. Call initialize() first.")
        return self._db

    # Message storage
    async def store_message(
        self,
        phone_number: str,
        role: str,
        content: str,
        project_name: Optional[str] = None,
        command_type: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None
    ) -> int:
        """Store a conversation message.

        Args:
            phone_number: User's phone number (E.164 format)
            role: 'user' or 'assistant'
            content: Message content
            project_name: Current project context if any
            command_type: Command type if this was a command (/do, /ask, etc.)
            metadata: Additional metadata (tokens, latency, etc.)

        Returns:
            The conversation ID
        """
        await self._ensure_initialized()

        # Ensure user exists
        await self.db.ensure_user(phone_number)

        # Get or create session
        session = await self.db.get_or_create_session(
            phone_number,
            project_name,
            self.session_timeout
        )
        logger.debug("session_resolved", session_id=session.id, is_new=session.message_count == 0)

        # Store the conversation
        conv_id = await self.db.store_conversation(
            phone_number=phone_number,
            session_id=session.id,
            role=role,
            content=content,
            project_name=project_name,
            command_type=command_type,
            metadata=metadata
        )

        # Update counters
        await self.db.update_user_activity(phone_number)
        await self.db.update_session_count(session.id)

        logger.debug(
            "message_stored",
            phone=phone_number[:6] + "...",
            role=role,
            conv_id=conv_id,
            content_length=len(content)
        )

        return conv_id

    # History retrieval
    async def get_history(
        self,
        phone_number: str,
        limit: int = 20,
        before: Optional[datetime] = None,
        project_name: Optional[str] = None
    ) -> List[Conversation]:
        """Get conversation history for a user.

        Args:
            phone_number: User's phone number
            limit: Maximum number of messages to return
            before: Only return messages before this timestamp
            project_name: Filter by project if specified

        Returns:
            List of conversations in chronological order
        """
        await self._ensure_initialized()
        return await self.db.get_history(phone_number, limit, before, project_name)

    # Preference management
    async def store_preference(
        self,
        phone_number: str,
        category: str,
        key: str,
        value: str,
        source_id: Optional[int] = None,
        confidence: float = 1.0
    ) -> int:
        """Store or update a user preference.

        Args:
            phone_number: User's phone number
            category: Preference category ('style', 'project', 'personal', 'technical')
            key: Preference key
            value: Preference value
            source_id: Conversation ID where this was learned
            confidence: Confidence score (0.0 to 1.0)

        Returns:
            Preference ID
        """
        await self._ensure_initialized()
        return await self.db.store_preference(
            phone_number, category, key, value, source_id, confidence
        )

    async def get_preferences(
        self,
        phone_number: str,
        category: Optional[str] = None
    ) -> List[Preference]:
        """Get user preferences.

        Args:
            phone_number: User's phone number
            category: Filter by category if specified

        Returns:
            List of preferences
        """
        await self._ensure_initialized()
        return await self.db.get_preferences(phone_number, category)

    # Explicit memory management
    async def remember(
        self,
        phone_number: str,
        memory_text: str,
        tags: Optional[List[str]] = None,
        project_name: Optional[str] = None
    ) -> int:
        """Store an explicit memory from /remember command.

        Args:
            phone_number: User's phone number
            memory_text: The text to remember
            tags: Optional tags for categorization
            project_name: Optional project to associate with this memory

        Returns:
            Memory ID
        """
        await self._ensure_initialized()
        memory_id = await self.db.store_memory(phone_number, memory_text, tags, project_name)
        logger.info(
            "memory_stored",
            phone=phone_number[:6] + "...",
            memory_id=memory_id,
            text_length=len(memory_text),
            project=project_name
        )
        return memory_id

    async def get_memories(
        self,
        phone_number: str,
        limit: int = 50,
        project_name: Optional[str] = None
    ) -> List[ExplicitMemory]:
        """Get explicit memories for a user.

        Args:
            phone_number: User's phone number
            limit: Maximum number of memories to return
            project_name: Optional project to filter by

        Returns:
            List of memories
        """
        await self._ensure_initialized()
        return await self.db.get_memories(phone_number, limit, project_name)

    # Semantic search
    async def semantic_search(
        self,
        phone_number: str,
        query: str,
        limit: int = 10,
        project_name: Optional[str] = None
    ) -> List[SearchResult]:
        """Search conversations semantically using embeddings.

        Falls back to keyword matching if embeddings are not available.

        Args:
            phone_number: User's phone number
            query: Search query
            limit: Maximum results to return
            project_name: Optional project to filter by

        Returns:
            List of search results ranked by relevance
        """
        await self._ensure_initialized()

        # Get history to search through (filtered by project if specified)
        history = await self.db.get_history(phone_number, limit=500, project_name=project_name)

        if not history:
            return []

        # If embeddings available, use semantic search
        if self._embeddings is not None:
            try:
                return await self._semantic_search_with_embeddings(query, history, limit)
            except Exception as e:
                logger.warning("embedding_search_failed", error=str(e))
                # Fall through to keyword search

        # Fallback: Simple keyword matching
        return self._keyword_search(query, history, limit)

    async def _semantic_search_with_embeddings(
        self,
        query: str,
        history: List[Conversation],
        limit: int
    ) -> List[SearchResult]:
        """Perform semantic search using embeddings."""
        # Generate query embedding
        query_embedding = await self._embeddings.embed(query)

        # Generate embeddings for all conversations (batch for efficiency)
        texts = [conv.content for conv in history]
        conv_embeddings = await self._embeddings.embed_batch(texts)

        # Calculate similarities
        results_with_scores = []
        for i, conv in enumerate(history):
            similarity = self._embeddings._cosine_similarity(query_embedding, conv_embeddings[i])
            results_with_scores.append((conv, similarity))

        # Sort by similarity (highest first)
        results_with_scores.sort(key=lambda x: x[1], reverse=True)

        # Convert to SearchResult objects
        results = []
        for conv, score in results_with_scores[:limit]:
            # Only include results with reasonable similarity
            if score > 0.2:  # Threshold for relevance
                results.append(SearchResult(
                    id=conv.id or 0,
                    content=conv.content,
                    role=conv.role,
                    timestamp=conv.timestamp,
                    project_name=conv.project_name,
                    similarity_score=score,
                    source_type="conversation"
                ))

        top_score = results_with_scores[0][1] if results_with_scores else 0.0
        logger.debug(
            "vector_search_results",
            query_length=len(query),
            results_count=len(results),
            top_score=round(top_score, 3),
        )

        return results

    def _keyword_search(
        self,
        query: str,
        history: List[Conversation],
        limit: int
    ) -> List[SearchResult]:
        """Fallback keyword-based search."""
        query_lower = query.lower()
        query_words = set(query_lower.split())
        results = []

        for conv in history:
            content_lower = conv.content.lower()

            # Check for exact phrase match
            if query_lower in content_lower:
                score = 0.8
            # Check for word overlap
            else:
                content_words = set(content_lower.split())
                overlap = query_words & content_words
                if overlap:
                    score = 0.3 + (0.3 * len(overlap) / len(query_words))
                else:
                    continue  # No match

            results.append(SearchResult(
                id=conv.id or 0,
                content=conv.content,
                role=conv.role,
                timestamp=conv.timestamp,
                project_name=conv.project_name,
                similarity_score=score,
                source_type="conversation"
            ))

        # Sort by score (highest first)
        results.sort(key=lambda x: x.similarity_score, reverse=True)
        return results[:limit]

    # Context building
    async def get_relevant_context(
        self,
        phone_number: str,
        query: str,
        project_name: Optional[str] = None,
        max_results: int = 5,
        max_tokens: int = 1500,
        use_summarizer: bool = False
    ) -> str:
        """Get relevant context for prompt injection.

        Retrieves preferences, explicit memories, and relevant past
        conversations, then formats them for injection into Claude prompts.

        Args:
            phone_number: User's phone number
            query: Current query/task
            project_name: Current project if any
            max_results: Maximum search results to consider
            max_tokens: Maximum tokens in context
            use_summarizer: Whether to use Haiku to summarize (slower but better)

        Returns:
            Formatted context string for prompt injection
        """
        await self._ensure_initialized()

        from .context_builder import ContextBuilder

        builder = ContextBuilder(max_tokens=max_tokens)

        # Get preferences
        preferences = await self.get_preferences(phone_number)

        # Get explicit memories
        memories = await self.get_memories(phone_number, limit=10)

        # Get relevant history using semantic search
        search_results = await self.semantic_search(phone_number, query, limit=max_results)

        # Optionally summarize with Haiku
        summarized_context = None
        if use_summarizer and search_results:
            try:
                from .haiku_summarizer import get_haiku_summarizer
                summarizer = get_haiku_summarizer()
                summarized_context = await summarizer.summarize_for_context(
                    search_results,
                    query,
                    max_output_tokens=300
                )
                # Record haiku summarizer usage
                if summarizer.last_usage:
                    try:
                        await self.db.record_usage(
                            phone_number=phone_number,
                            project_name=project_name,
                            model=summarizer.last_usage.get("model", "haiku"),
                            input_tokens=summarizer.last_usage.get("input_tokens", 0),
                            output_tokens=summarizer.last_usage.get("output_tokens", 0),
                            cost_usd=summarizer.last_usage.get("cost_usd", 0.0),
                            source="haiku",
                        )
                    except Exception:
                        pass  # Non-critical
            except Exception as e:
                logger.warning("summarizer_failed", error=str(e))

        # Fetch recent /do command history for conversational continuity
        command_history = None
        try:
            history = await self.get_history(
                phone_number, project_name=project_name, limit=10
            )
            # Filter to /do commands only, format as role/content dicts
            command_history = [
                {"role": h.role, "content": h.content}
                for h in history
                if getattr(h, "command_type", None) == "do"
            ]
            if not command_history:
                command_history = None
        except Exception as e:
            logger.debug("command_history_fetch_failed", error=str(e))

        # Build the context section
        return builder.build_context_section(
            preferences=preferences,
            explicit_memories=memories,
            relevant_history=search_results if not summarized_context else None,
            summarized_context=summarized_context,
            current_project=project_name,
            command_history=command_history,
        )

    # Deletion operations
    async def forget(
        self,
        phone_number: str,
        target: str
    ) -> bool:
        """Delete memories based on target scope.

        Args:
            phone_number: User's phone number
            target: 'all', 'preferences', or 'today'

        Returns:
            True if any data was deleted
        """
        await self._ensure_initialized()

        if target == "all":
            count = await self.db.delete_all_user_data(phone_number)
            logger.info("user_data_deleted", phone=phone_number[:6] + "...", count=count)
            return count > 0

        elif target == "preferences":
            count = await self.db.delete_preferences(phone_number)
            logger.info("preferences_deleted", phone=phone_number[:6] + "...", count=count)
            return count > 0

        elif target == "today":
            count = await self.db.delete_today_conversations(phone_number)
            logger.info("today_deleted", phone=phone_number[:6] + "...", count=count)
            return count > 0

        return False

    async def close(self) -> None:
        """Shut down the memory system.

        Closes the Haiku summarizer's HTTP client and the
        SQLite database connection. Resets the initialized flag
        so the manager can be re-initialized if needed.
        """
        try:
            from .haiku_summarizer import close_summarizer
            await close_summarizer()
        except Exception as e:
            logger.warning("haiku_summarizer_close_error", error=str(e))

        if self._db:
            await self._db.close()
            self._initialized = False
            logger.info("memory_manager_closed")


# Global memory manager instance
_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> Optional[MemoryManager]:
    """Get the global memory manager instance.

    Returns:
        The MemoryManager singleton, or None if not yet
        initialized.
    """
    return _memory_manager


async def initialize_memory_manager(
    db_path: Path,
    session_timeout_minutes: int = 30,
    max_context_tokens: int = 1500
) -> MemoryManager:
    """Create, initialize, and return the global memory manager.

    Args:
        db_path: Path to the SQLite database file.
        session_timeout_minutes: Session inactivity timeout.
        max_context_tokens: Token budget for context injection.

    Returns:
        The initialized MemoryManager singleton.
    """
    global _memory_manager
    _memory_manager = MemoryManager(
        db_path,
        session_timeout_minutes,
        max_context_tokens
    )
    await _memory_manager.initialize()
    return _memory_manager
