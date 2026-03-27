"""Device targeting plugin — route task commands to specific nightwire instances."""

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from nightwire.plugin_base import HelpSection, MessageMatcher, NightwirePlugin, PluginContext

# Commands that should NOT be gated — must work on all instances
_PASSTHROUGH_COMMANDS = frozenset({"target", "help"})


class DeviceTargetPlugin(NightwirePlugin):
    """Route /do and other task commands to a specific nightwire instance."""

    name = "device_target"
    description = "Route task commands to a specific nightwire instance"
    version = "1.0.0"

    def __init__(self, ctx: PluginContext):
        super().__init__(ctx)
        self._targets: Dict[str, str] = {}  # sender -> device name
        self._devices: List[str] = []  # cached nightwire device names
        self._refresh_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._targets_file = Path(ctx.data_dir) / "targets.json"
        self._load_targets()

    def _load_targets(self) -> None:
        """Load per-sender targets from disk."""
        if self._targets_file.is_file():
            try:
                self._targets = json.loads(self._targets_file.read_text())
            except (json.JSONDecodeError, OSError) as e:
                self.ctx.logger.warning("device_target_load_failed", error=str(e))
                self._targets = {}

    def _save_targets(self) -> None:
        """Persist per-sender targets to disk."""
        try:
            self._targets_file.write_text(json.dumps(self._targets, indent=2))
        except OSError as e:
            self.ctx.logger.error("device_target_save_failed", error=str(e))

    def _match_device(self, name: str) -> Optional[str]:
        """Match a user-provided name against known devices. Case-insensitive."""
        name_lower = name.lower()
        # Exact match
        for device in self._devices:
            if device.lower() == name_lower:
                return device
        # Suffix match (e.g., "osx" matches "nightwire-osx")
        for device in self._devices:
            if device.lower().endswith("-" + name_lower):
                return device
        return None

    def _is_gated_message(self, message: str) -> bool:
        """Check if a message should be gated. Everything is gated except passthrough commands."""
        stripped = message.strip()
        if stripped.startswith("/"):
            cmd = stripped[1:].split()[0].lower() if stripped[1:] else ""
            return cmd not in _PASSTHROUGH_COMMANDS
        # Plain text messages (implicit /do) are gated
        return True

    async def _handle_gate(self, sender: str, message: str) -> Optional[str]:
        """Pre-command gate handler.

        Returns:
            None: pass through (target matches this instance, or single-instance mode)
            "": silently consume (target is a different instance)
            str: send this response (no target set but multiple instances exist)
        """
        # No gating needed when running as sole instance
        if len(self._devices) < 2:
            return None

        target = self._targets.get(sender)
        if target is None:
            return "No target set. Use /target to pick a machine."
        if target == self.ctx.instance_name:
            return None  # This instance handles it
        return ""  # Different instance — silently consume

    def _should_respond(self, sender: str) -> bool:
        """Determine if this instance should send the /target response.

        All instances process /target to sync state, but only one responds.
        """
        current_target = self._targets.get(sender)
        if current_target is not None:
            return current_target == self.ctx.instance_name
        # No target set — first device alphabetically responds (deterministic tie-break)
        if self._devices:
            return self.ctx.instance_name == self._devices[0]
        return True

    async def _handle_target(self, sender: str, args: str) -> Optional[str]:
        """Handle /target command. All instances update state, only one responds."""
        args = args.strip().lower()

        if args == "clear":
            self._targets.pop(sender, None)
            self._save_targets()
            result = "Target cleared. Use /target to pick a machine."
        elif args == "status":
            target = self._targets.get(sender)
            if not target:
                result = "No target set. Use /target to pick a machine."
            else:
                warning = ""
                if target not in self._devices:
                    warning = f"\n⚠️ Warning: {target} is not currently visible in linked devices."
                result = f"Current target: {target}{warning}"
        elif args:
            matched = self._match_device(args)
            if matched:
                self._targets[sender] = matched
                self._save_targets()
                result = f"Target set to {matched}. Task commands will run on this instance."
            else:
                device_list = "\n".join(f"  - {d}" for d in self._devices) if self._devices else "  (none found)"
                result = f"No matching device found for '{args}'.\n\nAvailable instances:\n{device_list}"
        elif not self._devices:
            result = "No nightwire instances found. Check that devices are linked and Signal API is reachable."
        elif len(self._devices) == 1:
            self._targets[sender] = self._devices[0]
            self._save_targets()
            result = f"Only one instance found. Auto-selected: {self._devices[0]}"
        else:
            current = self._targets.get(sender)
            current_line = f"\nCurrent target: {current}" if current else ""
            device_list = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(self._devices))
            result = f"Available nightwire instances:\n{device_list}\n\nReply with /target <name> to select (e.g., /target osx).{current_line}"

        if not self._should_respond(sender):
            return None
        return result

    async def _refresh_devices(self) -> None:
        """Fetch linked devices from Signal API and filter for nightwire instances."""
        account = self.ctx.get_config("signal_account")
        if not account:
            self.ctx.logger.warning("device_target_no_account", msg="Set plugins.device_target.signal_account in settings.yaml")
            return

        url = f"{self.ctx.signal_api_url}/v1/devices/{account}"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    devices = await resp.json()
                    # Filter for devices with "nightwire" in the name
                    self._devices = sorted(
                        d["name"] for d in devices
                        if isinstance(d, dict) and d.get("name") and "nightwire" in d["name"].lower()
                    )
                    self.ctx.logger.info("device_target_refresh_complete", devices=self._devices)
                else:
                    self.ctx.logger.warning("device_target_refresh_failed", status=resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.ctx.logger.warning("device_target_refresh_error", error=str(e))

    async def _refresh_loop(self) -> None:
        """Periodically refresh the device list."""
        interval = self.ctx.get_config("refresh_interval", 300)
        while True:
            await self._refresh_devices()
            await asyncio.sleep(interval)

    async def on_start(self) -> None:
        """Start device refresh loop (only if signal_account is configured)."""
        if not self.ctx.get_config("signal_account"):
            self.ctx.logger.info("device_target_skipped", msg="No signal_account configured, plugin inactive")
            return
        self._session = aiohttp.ClientSession()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def on_stop(self) -> None:
        """Clean up resources."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    def commands(self):
        return {"target": self._handle_target}

    def message_matchers(self):
        return [
            MessageMatcher(
                priority=5,
                match_fn=self._is_gated_message,
                handle_fn=self._handle_gate,
                description="Device targeting gate",
                pre_command=True,
            ),
        ]

    def help_sections(self):
        return [
            HelpSection(
                title="Device Targeting",
                commands={
                    "target": "Show available instances and pick one",
                    "target <name>": "Set target instance (e.g., /target osx)",
                    "target status": "Show current target",
                    "target clear": "Clear target",
                },
            )
        ]
