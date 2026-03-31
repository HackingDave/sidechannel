"""Scheduler plugin — registers the /schedule command."""

from nightwire.plugin_base import (
    CommandHandler,
    HelpSection,
    NightwirePlugin,
    PluginContext,
)
from typing import Dict, List


class SchedulerPlugin(NightwirePlugin):
    name = "scheduler"
    description = "Schedule prompts to run at regular intervals"
    version = "1.0.0"

    def __init__(self, ctx: PluginContext):
        super().__init__(ctx)
        # The actual SchedulerCommands handler is injected by bot.py
        # after the scheduler system is initialized. This plugin just
        # provides the command registration and help text.
        self._handler = None

    def set_handler(self, handler):
        """Called by bot.py to inject the SchedulerCommands handler."""
        self._handler = handler

    def commands(self) -> Dict[str, CommandHandler]:
        return {"schedule": self._handle_schedule}

    async def _handle_schedule(self, sender: str, args: str) -> str:
        if not self._handler:
            return "Scheduler not initialized. Check bot startup logs."
        return await self._handler.handle(sender, args)

    def help_sections(self) -> List[HelpSection]:
        return [
            HelpSection(
                title="Scheduler",
                commands={
                    "schedule add <when> <prompt>": "Create a scheduled task",
                    "schedule list": "List all scheduled tasks",
                    "schedule remove <id>": "Delete a schedule",
                    "schedule pause <id>": "Pause a schedule",
                    "schedule resume <id>": "Resume a paused schedule",
                    "schedule run <id>": "Trigger a schedule immediately",
                    "schedule history <id>": "View recent run history",
                },
            )
        ]
