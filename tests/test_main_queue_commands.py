import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.module_stubs import install_test_stubs

install_test_stubs()

with patch.dict(os.environ, {"DISCORD_TOKEN": "offline-test-token"}):
    import main as bot_main

from music_service import MusicService
from music_state import MusicState


class LazyQueueCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot_main.state = MusicState()
        self.guild_id = 42
        self.service = MusicService(bot_main.client, bot_main.state)
        self.interaction = SimpleNamespace(
            guild=SimpleNamespace(id=self.guild_id),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    async def enqueue_lazy_track(self):
        loader_generation = bot_main.state.begin_playlist_loading(self.guild_id)
        queued_count, skipped_count = await self.service.enqueue_playlist_entries(
            self.guild_id,
            [{"title": "Queued", "url": "https://example.com/watch"}],
            loader_generation=loader_generation,
        )
        self.assertEqual((queued_count, skipped_count), (1, 0))
        return bot_main.state.get_queue(self.guild_id)[0]

    async def test_clearqueue_discards_lazy_track_without_starting_ffmpeg(self):
        with patch("music_audio.create_ffmpeg_source") as create_ffmpeg_source:
            queued_track = await self.enqueue_lazy_track()

            await bot_main.clearqueue(self.interaction)

        self.assertTrue(queued_track.is_lazy)
        self.assertEqual(bot_main.state.get_queue(self.guild_id), [])
        create_ffmpeg_source.assert_not_called()
        self.interaction.response.send_message.assert_awaited_once_with(
            "The queue has been cleared!"
        )

    async def test_remove_discards_lazy_track_without_starting_ffmpeg(self):
        with patch("music_audio.create_ffmpeg_source") as create_ffmpeg_source:
            queued_track = await self.enqueue_lazy_track()

            await bot_main.remove(self.interaction, 1)

        self.assertTrue(queued_track.is_lazy)
        self.assertEqual(bot_main.state.get_queue(self.guild_id), [])
        create_ffmpeg_source.assert_not_called()
        self.interaction.response.send_message.assert_awaited_once_with(
            "Removed **[Queued](https://example.com/watch)** from position 1."
        )
