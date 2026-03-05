"""Configuration management for nightwire.

Loads YAML settings (settings.yaml, projects.yaml) and environment
variables (.env) into a typed Config object. Property getters provide
safe access with sensible defaults for every subsystem: Claude SDK,
Nightwire assistant, memory, autonomous, logging, plugins, sandbox,
and auto-update.

Key classes:
    Config: Central configuration manager.

Key functions:
    get_config: Singleton accessor for the global Config instance.
"""

import os
import shutil
from pathlib import Path
from typing import List, Optional

import structlog
import yaml
from dotenv import load_dotenv

logger = structlog.get_logger("nightwire.config")


class Config:
    """Central configuration manager for nightwire.

    Loads settings.yaml, projects.yaml, and .env from the config
    directory. Provides typed property accessors for every
    configurable subsystem. Thread-safe for reads (no mutation
    after __init__ except save_projects/add_project/remove_project).

    Args:
        config_dir: Path to the config directory. Defaults to
            ``<repo_root>/config/``.
    """

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            config_dir = Path(__file__).parent.parent / "config"
        self.config_dir = config_dir

        # Load environment variables
        env_file = config_dir / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        # Load settings
        self.settings = self._load_yaml("settings.yaml")
        self.projects = self._load_yaml("projects.yaml") or {"projects": []}

    def _load_yaml(self, filename: str) -> dict:
        """Load a YAML configuration file."""
        filepath = self.config_dir / filename
        if filepath.exists():
            with open(filepath, "r") as f:
                return yaml.safe_load(f) or {}
        return {}

    def save_projects(self):
        """Save the projects configuration."""
        filepath = self.config_dir / "projects.yaml"
        with open(filepath, "w") as f:
            yaml.dump(self.projects, f, default_flow_style=False)

    @property
    def allowed_numbers(self) -> List[str]:
        """Get list of allowed phone numbers."""
        numbers = self.settings.get("allowed_numbers", [])
        if not isinstance(numbers, list):
            logger.error("allowed_numbers_invalid_type", type=type(numbers).__name__)
            return []
        return numbers

    def validate(self):
        """Validate critical settings at startup.

        Checks allowed_numbers format (E.164 or UUID) and
        autonomous config constraints. Logs warnings/errors
        but does not raise -- the bot starts in degraded mode.
        """
        import re
        uuid_pattern = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            re.IGNORECASE,
        )
        numbers = self.allowed_numbers
        if not numbers:
            logger.warning("no_allowed_numbers", msg="Bot will reject all messages")
        for n in numbers:
            if not isinstance(n, str):
                logger.error("invalid_allowed_entry", entry="..." + str(n)[-4:])
            elif uuid_pattern.match(n):
                pass  # Valid Signal UUID
            elif not n.startswith("+") or not n[1:].isdigit():
                logger.error("invalid_phone_number_format", number="..." + str(n)[-4:])

        # Check autonomous config types
        auto_config = self.settings.get("autonomous", {})
        if isinstance(auto_config, dict):
            mp = auto_config.get("max_parallel")
            if mp is not None and (not isinstance(mp, int) or mp < 1 or mp > 10):
                logger.error(
                    "config_invalid_value",
                    key="autonomous.max_parallel",
                    value=mp,
                    valid="1-10",
                )

    @property
    def signal_api_url(self) -> str:
        """Get Signal API URL. Env var SIGNAL_API_URL takes precedence."""
        return os.environ.get("SIGNAL_API_URL") or self.settings.get("signal_api_url", "http://127.0.0.1:8080")

    @property
    def instance_name(self) -> str:
        """Instance name for message prefixes (default: nightwire)."""
        return self.settings.get("instance_name", "nightwire")

    @property
    def projects_base_path(self) -> Path:
        """Get base path for projects."""
        configured = self.settings.get("projects_base_path")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "projects"

    @property
    def log_dir(self) -> Path:
        """Get log directory path."""
        configured = self.settings.get("log_dir")
        if configured:
            return Path(configured).expanduser()
        return Path(__file__).parent.parent / "logs"

    @property
    def logging_level(self) -> str:
        """Global log level (default INFO). Controls console and combined file."""
        log_config = self.settings.get("logging", {})
        return log_config.get("level", "INFO")

    @property
    def logging_subsystem_levels(self) -> dict:
        """Per-subsystem log level overrides. E.g. {"autonomous": "DEBUG"}."""
        log_config = self.settings.get("logging", {})
        return log_config.get("subsystem_levels", {})

    @property
    def logging_max_file_size_mb(self) -> int:
        """Max size per log file in MB before rotation (default 10)."""
        log_config = self.settings.get("logging", {})
        return log_config.get("max_file_size_mb", 10)

    @property
    def logging_backup_count(self) -> int:
        """Number of rotated log files to keep (default 5)."""
        log_config = self.settings.get("logging", {})
        return log_config.get("backup_count", 5)

    @property
    def claude_timeout(self) -> int:
        """Get Claude command timeout in seconds (default 30 minutes)."""
        return self.settings.get("claude_timeout", 1800)

    @property
    def claude_max_turns(self) -> int:
        """Get max turns per Claude invocation to prevent token overflow (default 15)."""
        return self.settings.get("claude_max_turns", 15)

    @property
    def claude_path(self) -> str:
        """Get absolute path to Claude CLI binary.

        Resolution order: settings.yaml ``claude_path`` → ``which claude``
        → ``~/.local/bin/claude`` → bare ``claude`` (relies on PATH).
        """
        configured = self.settings.get("claude_path")
        if configured:
            return configured
        found = shutil.which("claude")
        if found:
            return found
        home_local = Path.home() / ".local" / "bin" / "claude"
        if home_local.exists():
            return str(home_local)
        return "claude"

    @property
    def claude_api_key(self) -> str:
        """Get Anthropic API key (optional).

        Only needed if not using Claude CLI OAuth login. The CLI
        also reads this env var directly, so setting it provides
        API key auth to both the CLI and any direct SDK callers.
        """
        return os.environ.get("ANTHROPIC_API_KEY", "")

    @property
    def claude_model(self) -> str:
        """Get Claude model for CLI calls (default claude-sonnet-4-5).

        Passed as ``--model`` flag to the CLI. Supports model aliases
        like ``sonnet``, ``opus``, ``haiku`` or full model IDs.
        Override via ``claude_model`` in settings.yaml.
        """
        return self.settings.get("claude_model", "claude-sonnet-4-5")

    @property
    def claude_system_prompt(self) -> str:
        """Load CLAUDE.md guidelines as system prompt text.

        The CLI runner uses ``--append-system-prompt-file`` instead of
        this property. Retained for any callers that need the raw text.
        """
        guidelines_path = self.config_dir / "CLAUDE.md"
        if guidelines_path.exists():
            try:
                return guidelines_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.error("guidelines_load_error", error=str(e))
        return ""

    @property
    def anthropic_client_config(self) -> dict:
        """Get Anthropic SDK client config (timeout, max_retries).

        Reads from settings.yaml 'anthropic' section with sensible defaults.
        """
        anthropic_config = self.settings.get("anthropic", {})
        return {
            "timeout": anthropic_config.get("timeout", 600.0),
            "max_retries": anthropic_config.get("max_retries", 2),
        }

    # nightwire AI assistant configuration (any OpenAI-compatible provider)

    def _get_assistant_config(self) -> dict:
        """Get nightwire assistant config dict, protecting against non-dict values."""
        cfg = (
            self.settings.get("nightwire_assistant")
            or self.settings.get("sidechannel_assistant", {})
        )
        if not isinstance(cfg, dict):
            return {}
        return cfg

    @property
    def nightwire_assistant_enabled(self) -> bool:
        """Whether nightwire AI assistant is enabled."""
        sc_config = self._get_assistant_config()
        if sc_config.get("enabled") is not None:
            return sc_config.get("enabled", False)
        # Fallback to legacy nova / grok config
        nova_config = self.settings.get("nova", {})
        if nova_config.get("enabled") is not None:
            return nova_config.get("enabled", False)
        grok_config = self.settings.get("grok", {})
        return grok_config.get("enabled", False)

    @property
    def grok_enabled(self) -> bool:
        """Backward-compatible alias for nightwire_assistant_enabled."""
        return self.nightwire_assistant_enabled

    @property
    def nightwire_assistant_provider(self) -> str:
        """Detect which provider to use.

        Any string is valid — 'openai' and 'grok' have built-in presets.
        Priority:
        1. Explicit nightwire_assistant.provider setting
        2. Auto-detect from env: only OPENAI_API_KEY -> 'openai'
        3. Auto-detect from env: only GROK_API_KEY -> 'grok'
        4. Both keys present -> 'grok' (backward compat)
        5. Neither key -> 'grok' (will fail gracefully at call time)
        """
        sc_config = self._get_assistant_config()
        explicit = sc_config.get("provider")
        if explicit:
            return explicit
        # Fallback to legacy nova config
        nova_config = self.settings.get("nova", {})
        explicit = nova_config.get("provider")
        if explicit:
            return explicit

        has_openai = bool(os.environ.get("OPENAI_API_KEY"))
        has_grok = bool(os.environ.get("GROK_API_KEY"))

        if has_openai and not has_grok:
            return "openai"
        # grok for: only grok, both, or neither
        return "grok"

    @property
    def nightwire_assistant_api_key(self) -> str:
        """Return the API key for the active provider.

        Priority:
        1. Explicit api_key_env setting -> read that env var
        2. provider == 'openai' -> OPENAI_API_KEY
        3. provider == 'grok' -> GROK_API_KEY
        4. Fallback: NIGHTWIRE_API_KEY env var
        """
        sc_config = self._get_assistant_config()
        api_key_env = sc_config.get("api_key_env")
        if api_key_env:
            key = os.environ.get(api_key_env, "")
            if not key:
                logger.warning("config_api_key_env_empty", env_var=api_key_env)
            return key
        provider = self.nightwire_assistant_provider
        if provider == "openai":
            return os.environ.get("OPENAI_API_KEY", "")
        if provider == "grok":
            return os.environ.get("GROK_API_KEY", "")
        return os.environ.get("NIGHTWIRE_API_KEY", "")

    @property
    def nightwire_assistant_api_url(self) -> str:
        """Return the API URL for the active provider.

        Priority:
        1. Explicit api_url in config
        2. Provider presets: openai/grok
        3. No default for unknown providers (log warning)
        """
        sc_config = self._get_assistant_config()
        custom_url = sc_config.get("api_url")
        if custom_url:
            return custom_url
        # Fallback to legacy nova config
        nova_config = self.settings.get("nova", {})
        custom_url = nova_config.get("api_url")
        if custom_url:
            return custom_url
        provider = self.nightwire_assistant_provider
        if provider == "openai":
            return "https://api.openai.com/v1/chat/completions"
        if provider == "grok":
            return "https://api.x.ai/v1/chat/completions"
        logger.warning("config_no_api_url_for_provider", provider=provider,
                       hint="Set nightwire_assistant.api_url in settings.yaml")
        return ""

    @property
    def nightwire_assistant_model(self) -> str:
        """Return the model name for the active provider.

        Priority:
        1. Explicit model in config
        2. Provider presets: openai -> gpt-4o, grok -> grok-3-latest
        3. No default for unknown providers (log warning)
        """
        sc_config = self._get_assistant_config()
        model = sc_config.get("model")
        if model:
            return model
        # Fallback to legacy nova / grok config
        nova_config = self.settings.get("nova", {})
        model = nova_config.get("model")
        if model:
            return model
        grok_config = self.settings.get("grok", {})
        model = grok_config.get("model")
        if model:
            return model
        # Provider presets
        provider = self.nightwire_assistant_provider
        if provider == "openai":
            return "gpt-4o"
        if provider == "grok":
            return "grok-3-latest"
        logger.warning("config_no_model_for_provider", provider=provider,
                       hint="Set nightwire_assistant.model in settings.yaml")
        return ""

    @property
    def nightwire_assistant_max_tokens(self) -> int:
        """Return max tokens for nightwire assistant responses."""
        default = 1024
        for section in ("nightwire_assistant", "sidechannel_assistant", "nova", "grok"):
            cfg = self.settings.get(section, {})
            if not isinstance(cfg, dict):
                continue
            val = cfg.get("max_tokens")
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    logger.warning("config_invalid_max_tokens", section=section, value=val)
                    return default
        return default

    # Memory configuration
    @property
    def memory_session_timeout(self) -> int:
        """Get session timeout in minutes for memory grouping."""
        memory_config = self.settings.get("memory", {})
        return memory_config.get("session_timeout", 30)

    @property
    def memory_max_context_tokens(self) -> int:
        """Get max tokens for memory context injection."""
        memory_config = self.settings.get("memory", {})
        return memory_config.get("max_context_tokens", 1500)

    @property
    def memory_embedding_model(self) -> str:
        """Get embedding model name for semantic search."""
        memory_config = self.settings.get("memory", {})
        return memory_config.get("embedding_model", "all-MiniLM-L6-v2")

    # Autonomous system configuration
    @property
    def autonomous_enabled(self) -> bool:
        """Whether autonomous system is enabled."""
        auto_config = self.settings.get("autonomous", {})
        return auto_config.get("enabled", True)

    @property
    def autonomous_poll_interval(self) -> int:
        """Seconds between queue polls."""
        auto_config = self.settings.get("autonomous", {})
        return auto_config.get("poll_interval", 30)

    @property
    def autonomous_max_retries(self) -> int:
        """Max retries for failed tasks."""
        auto_config = self.settings.get("autonomous", {})
        return auto_config.get("max_retries", 2)

    @property
    def autonomous_quality_gates(self) -> bool:
        """Whether to run tests/typecheck after tasks."""
        auto_config = self.settings.get("autonomous", {})
        return auto_config.get("quality_gates", True)

    @property
    def autonomous_max_parallel(self) -> int:
        """Max parallel task workers (default 3, max 10)."""
        auto_config = self.settings.get("autonomous", {})
        val = auto_config.get("max_parallel", 3)
        try:
            return max(1, min(int(val), 10))
        except (ValueError, TypeError):
            logger.warning("config_invalid_max_parallel", value=val)
            return 3

    @property
    def autonomous_verification(self) -> bool:
        """Whether to run independent verification on task output."""
        auto_config = self.settings.get("autonomous", {})
        return auto_config.get("verification", True)

    @property
    def autonomous_effort_levels(self) -> dict:
        """Effort level mapping for task types."""
        defaults = {
            "prd_breakdown": "max",
            "implementation": "high",
            "bug_fix": "high",
            "refactor": "medium",
            "testing": "medium",
            "verification": "max",
        }
        auto_config = self.settings.get("autonomous", {})
        user_levels = auto_config.get("effort_levels", {})
        return {**defaults, **user_levels}

    # Auto-update configuration
    @property
    def auto_update_enabled(self) -> bool:
        """Whether auto-update checking is enabled."""
        update_config = self.settings.get("auto_update", {})
        return update_config.get("enabled", False)

    @property
    def auto_update_check_interval(self) -> int:
        """Seconds between update checks (default 6 hours)."""
        update_config = self.settings.get("auto_update", {})
        return update_config.get("check_interval", 21600)

    @property
    def auto_update_branch(self) -> str:
        """Git branch to track for updates."""
        update_config = self.settings.get("auto_update", {})
        return update_config.get("branch", "main")

    @property
    def allowed_paths(self) -> List[Path]:
        """Get list of additional allowed paths (outside projects_base_path)."""
        paths = self.settings.get("allowed_paths", [])
        return [Path(p).expanduser() for p in paths]

    @property
    def plugins_dir(self) -> Path:
        """Get plugins directory path."""
        configured = self.settings.get("plugins_dir")
        if configured:
            return Path(configured).expanduser()
        return Path(self.config_dir).parent / "plugins"

    @property
    def attachments_dir(self) -> Path:
        """Get attachments storage directory path.

        Configurable via ``attachments_dir`` in settings.yaml.
        Defaults to ``data/attachments/`` relative to the install directory.
        """
        configured = self.settings.get("attachments_dir")
        if configured:
            return Path(configured).expanduser()
        return Path(self.config_dir).parent / "data" / "attachments"

    @property
    def claude_max_budget_usd(self) -> Optional[float]:
        """Maximum dollar amount for a single Claude CLI invocation.

        Passed as ``--max-budget-usd`` flag. Default None (no cap).
        Configurable via ``claude_max_budget_usd`` in settings.yaml.
        """
        val = self.settings.get("claude_max_budget_usd")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            logger.warning("config_invalid_budget", value=val)
            return None

    @property
    def usage_daily_budget_usd(self) -> Optional[float]:
        """Daily spending budget in USD. Alert at 80%, block at 100%.

        Configurable via ``usage.daily_budget_usd`` in settings.yaml.
        Default None (no daily cap).
        """
        usage = self.settings.get("usage", {})
        if not isinstance(usage, dict):
            return None
        val = usage.get("daily_budget_usd")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @property
    def usage_weekly_budget_usd(self) -> Optional[float]:
        """Weekly spending budget in USD. Alert at 80%, block at 100%.

        Configurable via ``usage.weekly_budget_usd`` in settings.yaml.
        Default None (no weekly cap).
        """
        usage = self.settings.get("usage", {})
        if not isinstance(usage, dict):
            return None
        val = usage.get("weekly_budget_usd")
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @property
    def attachment_max_age_hours(self) -> int:
        """Max age for attachment files in hours before cleanup deletes them.

        Configurable via ``attachment_max_age_hours`` in settings.yaml.
        Default 24. Set to 0 to disable automatic cleanup.
        """
        return int(self.settings.get("attachment_max_age_hours", 24))

    @property
    def sandbox_enabled(self) -> bool:
        """Whether Docker sandbox is enabled for task execution."""
        sandbox_config = self.settings.get("sandbox", {})
        return sandbox_config.get("enabled", False)

    @property
    def sandbox_config(self) -> dict:
        """Get sandbox configuration dict."""
        return self.settings.get("sandbox", {})

    def get_project_list(self) -> List[dict]:
        """Get list of registered projects."""
        return self.projects.get("projects", [])

    def add_project(self, name: str, path: str, description: str = "") -> bool:
        """Add a new project to the registry.

        Args:
            name: Unique project name.
            path: Absolute filesystem path to the project.
            description: Optional one-line description.

        Returns:
            True if added, False if a project with that name exists.
        """
        # Check if project already exists
        for p in self.projects.get("projects", []):
            if p["name"] == name:
                return False

        if "projects" not in self.projects:
            self.projects["projects"] = []

        self.projects["projects"].append({
            "name": name,
            "path": path,
            "description": description
        })
        self.save_projects()
        return True

    def remove_project(self, name: str) -> bool:
        """Remove a project from the registry by name.

        Args:
            name: Project name (case-insensitive match).

        Returns:
            True if removed, False if not found.
        """
        projects = self.projects.get("projects", [])
        for i, p in enumerate(projects):
            if p["name"].lower() == name.lower():
                projects.pop(i)
                self.save_projects()
                return True
        return False

    def get_project_path(self, name: str) -> Optional[Path]:
        """Get the path for a project by name (case-insensitive)."""
        for p in self.projects.get("projects", []):
            if p["name"].lower() == name.lower():
                return Path(p["path"])
        return None


# Global config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get or create the global config instance."""
    global _config
    if _config is None:
        _config = Config()
    return _config
