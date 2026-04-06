"""Configuration management for nightwire."""

import os
import shutil
from pathlib import Path
from typing import List, Optional

import structlog
import yaml
from dotenv import load_dotenv

logger = structlog.get_logger()


class Config:
    """Configuration manager for the bot."""

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
        tmp = filepath.with_suffix(".yaml.tmp")
        with open(tmp, "w") as f:
            yaml.dump(self.projects, f, default_flow_style=False)
        os.replace(str(tmp), str(filepath))

    @property
    def allowed_numbers(self) -> List[str]:
        """Get list of allowed phone numbers."""
        numbers = self.settings.get("allowed_numbers", [])
        if not isinstance(numbers, list):
            logger.error("allowed_numbers_invalid_type", type=type(numbers).__name__)
            return []
        return numbers

    def validate(self):
        """Validate critical settings at startup. Call from main.py."""
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
                logger.error("config_invalid_value", key="autonomous.max_parallel", value=mp, valid="1-10")

    @property
    def instance_name(self) -> str:
        """Get the display name for this bot instance (e.g., 'nightwire', 'nightwire-mac')."""
        return self.settings.get("instance_name", "nightwire")

    @property
    def signal_api_url(self) -> str:
        """Get Signal API URL. Env var SIGNAL_API_URL takes precedence."""
        return os.environ.get("SIGNAL_API_URL") or self.settings.get("signal_api_url", "http://127.0.0.1:8080")

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
    def claude_timeout(self) -> int:
        """Get Claude command timeout in seconds (default 30 minutes)."""
        return self.settings.get("claude_timeout", 1800)

    @property
    def claude_max_turns(self) -> int:
        """Get max turns per Claude invocation to prevent token overflow (default 25)."""
        return self.settings.get("claude_max_turns", 25)

    @property
    def max_concurrent_tasks(self) -> int:
        """Max Claude subprocesses running simultaneously (default 2)."""
        return self.settings.get("max_concurrent_tasks", 2)

    @property
    def claude_path(self) -> str:
        """Get absolute path to Claude CLI binary."""
        configured = self.settings.get("claude_path")
        if configured:
            return configured
        # Try to find claude in PATH
        found = shutil.which("claude")
        if found:
            return found
        # Fallback to common locations
        home_local = Path.home() / ".local" / "bin" / "claude"
        if home_local.exists():
            return str(home_local)
        return "claude"  # Hope it's in PATH

    @property
    def runner_type(self) -> str:
        """Get configured task runner type (default claude)."""
        return self.settings.get("runner", {}).get("type", "claude")

    @property
    def runner_path(self) -> str:
        """Get absolute path to active runner binary."""
        configured = self.settings.get("runner", {}).get("path")
        if configured:
            return configured

        if self.runner_type == "opencode":
            found = shutil.which("opencode")
            if found:
                return found
            home_local = Path.home() / ".local" / "bin" / "opencode"
            if home_local.exists():
                return str(home_local)
        elif self.runner_type == "codex":
            found = shutil.which("codex")
            if found:
                return found
            npm_global = Path.home() / ".npm-global" / "bin" / "codex"
            if npm_global.exists():
                return str(npm_global)
            home_local = Path.home() / ".local" / "bin" / "codex"
            if home_local.exists():
                return str(home_local)

        return self.claude_path

    # nightwire AI assistant configuration (any OpenAI-compatible provider)
    def _get_dict_setting(self, *keys: str) -> dict:
        """Safely get a config section that should be a dict, with fallback keys.

        Returns the first non-empty dict found, or {} if none are valid dicts.
        Protects against config values like `nightwire_assistant: true` (bool)
        instead of the expected dict structure.
        """
        for key in keys:
            val = self.settings.get(key)
            if isinstance(val, dict) and val:
                return val
        return {}

    @property
    def nightwire_assistant_enabled(self) -> bool:
        """Whether nightwire AI assistant is enabled."""
        sc_config = self._get_dict_setting("nightwire_assistant", "sidechannel_assistant")
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
        sc_config = self._get_dict_setting("nightwire_assistant", "sidechannel_assistant")
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
        sc_config = self._get_dict_setting("nightwire_assistant", "sidechannel_assistant")
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
        sc_config = self._get_dict_setting("nightwire_assistant", "sidechannel_assistant")
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
        sc_config = self._get_dict_setting("nightwire_assistant", "sidechannel_assistant")
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
    def attachments_dir(self) -> Path:
        """Get attachments directory path."""
        configured = self.settings.get("attachments_dir")
        if configured:
            return Path(configured).expanduser()
        return Path(self.config_dir).parent / "data" / "attachments"

    @property
    def plugins_dir(self) -> Path:
        """Get plugins directory path."""
        configured = self.settings.get("plugins_dir")
        if configured:
            return Path(configured).expanduser()
        return Path(self.config_dir).parent / "plugins"

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
        """Add a new project to the registry."""
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
        """Remove a project from the registry by name (case-insensitive)."""
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
