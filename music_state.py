"""Per-guild mutable state for queues, background tasks, and disconnect locks."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_audio import YTDLSource


@dataclass
class MusicState:  # pylint: disable=too-many-instance-attributes
    """Store queue and playback-related state scoped by guild ID."""

    max_queue_size: int = 100
    alone_disconnect_delay: int = 0
    queues: dict[int, list["YTDLSource"]] = field(
        default_factory=lambda: defaultdict(list)
    )
    loading_playlists: dict[int, bool] = field(
        default_factory=lambda: defaultdict(bool)
    )
    playlist_load_generations: dict[int, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    loading_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    text_channels: dict[int, int] = field(default_factory=dict)
    disconnect_locks: dict[int, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )

    def get_queue(self, guild_id: int) -> list["YTDLSource"]:
        """Return the mutable queue list for one guild."""
        return self.queues[guild_id]

    def cleanup_guild(self, guild_id: int):
        """Clean up guild state when disconnecting."""
        if guild_id in self.queues:
            self.queues[guild_id].clear()

        self.stop_playlist_loading(guild_id)
        self.text_channels.pop(guild_id, None)

    def begin_playlist_loading(self, guild_id: int) -> int:
        """Start a new owned playlist loader for one guild."""
        previous_task = self.loading_tasks.pop(guild_id, None)
        if previous_task is not None and not previous_task.done():
            previous_task.cancel()

        generation = self.playlist_load_generations[guild_id] + 1
        self.playlist_load_generations[guild_id] = generation
        self.loading_playlists[guild_id] = True
        return generation

    def register_playlist_loading_task(
        self, guild_id: int, generation: int, task: asyncio.Task
    ) -> bool:
        """Track a loader task only when it still owns the guild loading slot."""
        if not self.is_current_playlist_loader(guild_id, generation):
            if not task.done():
                task.cancel()
            return False

        self.loading_tasks[guild_id] = task
        return True

    def is_current_playlist_loader(self, guild_id: int, generation: int) -> bool:
        """Return True when the generation still owns playlist loading for a guild."""
        return (
            self.loading_playlists.get(guild_id, False)
            and self.playlist_load_generations.get(guild_id, 0) == generation
        )

    def finish_playlist_loading(self, guild_id: int, generation: int | None = None):
        """Mark playlist loading as finished for the current owning loader only."""
        if generation is not None and not self.is_current_playlist_loader(
            guild_id, generation
        ):
            return

        self.loading_playlists[guild_id] = False
        self.loading_tasks.pop(guild_id, None)

    def stop_playlist_loading(self, guild_id: int):
        """Stop background playlist loading for one guild."""
        self.loading_playlists[guild_id] = False
        task = self.loading_tasks.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()

    def remember_text_channel(self, guild_id: int, channel_id: int):
        """Store the last text channel used by a guild command."""
        self.text_channels[guild_id] = channel_id
