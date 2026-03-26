from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_audio import YTDLSource


@dataclass
class MusicState:
    max_queue_size: int = 100
    alone_disconnect_delay: int = 0
    queues: dict[int, list["YTDLSource"]] = field(
        default_factory=lambda: defaultdict(list)
    )
    loading_playlists: dict[int, bool] = field(
        default_factory=lambda: defaultdict(bool)
    )
    loading_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    text_channels: dict[int, int] = field(default_factory=dict)
    disconnect_locks: dict[int, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock)
    )

    def get_queue(self, guild_id: int) -> list["YTDLSource"]:
        return self.queues[guild_id]

    def cleanup_guild(self, guild_id: int):
        """Clean up guild state when disconnecting."""
        if guild_id in self.queues:
            self.queues[guild_id].clear()

        self.loading_playlists[guild_id] = False
        if guild_id in self.loading_tasks:
            task = self.loading_tasks.pop(guild_id)
            if not task.done():
                task.cancel()

        self.text_channels.pop(guild_id, None)

    def finish_playlist_loading(self, guild_id: int):
        """Mark background playlist loading as finished for a guild."""
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
