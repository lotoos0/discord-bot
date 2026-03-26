import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.module_stubs import install_test_stubs

install_test_stubs()

from music_service import MusicService
from music_state import MusicState


class FakeTextChannel:
    def __init__(self, channel_id=123):
        self.id = channel_id
        self.send = AsyncMock()


class FakeVoiceClient:
    def __init__(self):
        self.disconnect = AsyncMock()
        self.play = Mock()
        self.is_playing = Mock(return_value=False)
        self.channel = None


class MusicServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.client = SimpleNamespace(
            user=SimpleNamespace(id=999),
            loop=self.loop,
            get_channel=Mock(return_value=None),
            get_guild=Mock(),
        )
        self.state = MusicState()
        self.service = MusicService(self.client, self.state)
        self.guild_id = 42

    def make_guild(self, voice_client=None):
        return SimpleNamespace(id=self.guild_id, voice_client=voice_client)

    def make_interaction(self, guild=None, channel=None, user=None):
        guild = guild or self.make_guild(FakeVoiceClient())
        channel = channel or FakeTextChannel(456)
        user = user or SimpleNamespace(voice=None)
        return SimpleNamespace(
            guild=guild,
            channel=channel,
            user=user,
            followup=SimpleNamespace(send=AsyncMock()),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        )

    async def test_disconnect_guild_voice_disconnects_and_cleans_up(self):
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        self.service.send_guild_message = AsyncMock(return_value=True)
        self.state.cleanup_guild = Mock()

        await self.service.disconnect_guild_voice(
            guild,
            guild_id=self.guild_id,
            message="Disconnecting now",
            warning_context="warning",
            already_disconnected_log="already gone",
            success_log="disconnected",
        )

        self.service.send_guild_message.assert_awaited_once_with(
            self.guild_id, "Disconnecting now", "warning"
        )
        voice_client.disconnect.assert_awaited_once_with(force=False)
        self.state.cleanup_guild.assert_called_once_with(self.guild_id)

    async def test_disconnect_guild_voice_short_circuits_when_already_disconnected(
        self,
    ):
        guild = self.make_guild(voice_client=None)
        self.service.send_guild_message = AsyncMock(return_value=True)
        self.state.cleanup_guild = Mock()

        await self.service.disconnect_guild_voice(
            guild,
            guild_id=self.guild_id,
            message="Disconnecting now",
            warning_context="warning",
            already_disconnected_log="already gone",
            success_log="disconnected",
        )

        self.service.send_guild_message.assert_not_awaited()
        self.state.cleanup_guild.assert_not_called()

    async def test_disconnect_guild_voice_serializes_same_guild_concurrent_calls(self):
        gate = asyncio.Event()
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        self.state.cleanup_guild = Mock()

        async def send_guild_message(*args, **kwargs):
            await gate.wait()
            return True

        async def disconnect(*, force=False):
            guild.voice_client = None

        self.service.send_guild_message = AsyncMock(side_effect=send_guild_message)
        voice_client.disconnect.side_effect = disconnect

        first_call = asyncio.create_task(
            self.service.disconnect_guild_voice(
                guild,
                guild_id=self.guild_id,
                message="Disconnecting now",
                warning_context="warning",
                already_disconnected_log="already gone",
                success_log="disconnected",
            )
        )
        await asyncio.sleep(0)
        second_call = asyncio.create_task(
            self.service.disconnect_guild_voice(
                guild,
                guild_id=self.guild_id,
                message="Disconnecting now",
                warning_context="warning",
                already_disconnected_log="already gone",
                success_log="disconnected",
            )
        )
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(first_call, second_call)

        self.service.send_guild_message.assert_awaited_once()
        voice_client.disconnect.assert_awaited_once_with(force=False)
        self.state.cleanup_guild.assert_called_once_with(self.guild_id)

    async def test_disconnect_guild_voice_continues_when_message_send_fails(self):
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        self.service.send_guild_message = AsyncMock(return_value=False)
        self.state.cleanup_guild = Mock()

        await self.service.disconnect_guild_voice(
            guild,
            guild_id=self.guild_id,
            message="Disconnecting now",
            warning_context="warning",
            already_disconnected_log="already gone",
            success_log="disconnected",
        )

        self.service.send_guild_message.assert_awaited_once_with(
            self.guild_id, "Disconnecting now", "warning"
        )
        voice_client.disconnect.assert_awaited_once_with(force=False)
        self.state.cleanup_guild.assert_called_once_with(self.guild_id)

    async def test_enqueue_entry_appends_player_and_announces_success(self):
        player = SimpleNamespace(title="Demo", url="https://example.com/demo")

        with patch(
            "music_service.create_player_from_entry",
            new=AsyncMock(return_value=player),
        ):
            send_message = AsyncMock(return_value=True)
            self.service.send_channel_message = send_message

            result = await self.service.enqueue_entry(
                self.guild_id, FakeTextChannel(), {"url": "https://example.com/demo"}
            )

        self.assertTrue(result)
        self.assertEqual(self.state.get_queue(self.guild_id), [player])
        send_message.assert_awaited_once_with(
            unittest.mock.ANY,
            "Added to queue: **[Demo](https://example.com/demo)**",
            "Failed to send queue addition message",
        )

    async def test_enqueue_entry_respects_queue_limit(self):
        self.state.max_queue_size = 1
        self.state.get_queue(self.guild_id).append("existing")
        player = SimpleNamespace(title="Overflow", url="https://example.com/overflow")

        with patch(
            "music_service.create_player_from_entry",
            new=AsyncMock(return_value=player),
        ):
            send_message = AsyncMock(return_value=True)
            self.service.send_channel_message = send_message

            result = await self.service.enqueue_entry(
                self.guild_id,
                FakeTextChannel(),
                {"url": "https://example.com/overflow"},
            )

        self.assertFalse(result)
        self.assertEqual(self.state.get_queue(self.guild_id), ["existing"])
        send_message.assert_awaited_once_with(
            unittest.mock.ANY,
            "Queue is full (max 1 songs)!",
            "Failed to send queue full message",
        )

    async def test_enqueue_entry_reports_player_creation_error(self):
        with patch(
            "music_service.create_player_from_entry",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            send_message = AsyncMock(return_value=True)
            self.service.send_channel_message = send_message

            result = await self.service.enqueue_entry(
                self.guild_id, FakeTextChannel(), {"url": "https://example.com/bad"}
            )

        self.assertFalse(result)
        self.assertEqual(self.state.get_queue(self.guild_id), [])
        send_message.assert_awaited_once_with(
            unittest.mock.ANY,
            "Skipped one item (error): boom",
            "Failed to send enqueue error message",
        )

    async def test_handle_music_request_queues_first_song_and_cleans_loading_state(
        self,
    ):
        voice_client = FakeVoiceClient()
        voice_client.is_playing.return_value = False
        interaction = self.make_interaction(guild=self.make_guild(voice_client))
        first_info = {"title": "First", "url": "stream", "webpage_url": "https://first"}
        playlist_info = {"entries": [{"id": "first"}, {"id": "second"}]}
        created = {}
        real_create_task = asyncio.create_task

        def create_task_wrapper(coro):
            created["loading_before"] = self.state.loading_playlists[self.guild_id]
            task = real_create_task(coro)
            created["task"] = task
            return task

        self.service.enqueue_entry = AsyncMock(return_value=True)
        self.service.play_next = AsyncMock()
        self.service.enqueue_playlist_entries = AsyncMock(return_value=(1, 0))
        self.service.send_channel_message = AsyncMock(return_value=True)

        with patch(
            "music_service.extract_info_async",
            new=AsyncMock(side_effect=[first_info, playlist_info]),
        ), patch("music_service.asyncio.create_task", side_effect=create_task_wrapper):
            await self.service.handle_music_request(interaction, "https://playlist")
            await created["task"]

        self.service.enqueue_entry.assert_awaited_once_with(
            self.guild_id,
            interaction.channel,
            first_info,
            announce=False,
            use_entry_method=True,
        )
        self.service.play_next.assert_awaited_once_with(
            self.guild_id, interaction.channel.id
        )
        self.assertTrue(created["loading_before"])
        self.assertFalse(self.state.loading_playlists[self.guild_id])
        self.assertNotIn(self.guild_id, self.state.loading_tasks)
        interaction.followup.send.assert_awaited_once_with(
            "First song queued! Fetching rest of playlist in background...",
            ephemeral=True,
        )

    async def test_enqueue_playlist_entries_counts_queued_and_skipped_items(self):
        first_player = SimpleNamespace(title="One", url="https://example.com/one")

        with patch(
            "music_service.YTDLSource.from_url",
            new=AsyncMock(side_effect=[first_player, RuntimeError("broken")]),
        ):
            queued_count, skipped_count = await self.service.enqueue_playlist_entries(
                self.guild_id,
                [
                    {"url": "https://example.com/one"},
                    {"foo": "missing"},
                    {"id": "broken"},
                ],
            )

        self.assertEqual(queued_count, 1)
        self.assertEqual(skipped_count, 2)
        self.assertEqual(self.state.get_queue(self.guild_id), [first_player])

    async def test_get_next_ready_player_resolves_lazy_player(self):
        resolved_player = SimpleNamespace(title="Resolved")
        lazy_player = SimpleNamespace(
            is_lazy=True,
            title="Lazy",
            get_actual_source=AsyncMock(return_value=resolved_player),
        )
        self.state.get_queue(self.guild_id).append(lazy_player)

        player = await self.service.get_next_ready_player(self.guild_id)

        self.assertIs(player, resolved_player)
        lazy_player.get_actual_source.assert_awaited_once_with()

    async def test_get_next_ready_player_skips_broken_lazy_player_and_continues(self):
        broken_lazy = SimpleNamespace(
            is_lazy=True,
            title="Broken",
            get_actual_source=AsyncMock(side_effect=RuntimeError("bad lazy")),
        )
        ready_player = SimpleNamespace(is_lazy=False, title="Ready")
        queue = self.state.get_queue(self.guild_id)
        queue.extend([broken_lazy, ready_player])

        player = await self.service.get_next_ready_player(self.guild_id)

        self.assertIs(player, ready_player)
        broken_lazy.get_actual_source.assert_awaited_once_with()
        self.assertEqual(queue, [])

    async def test_retry_player_once_retries_only_a_single_time(self):
        player = SimpleNamespace(_retries=0, url="https://retry", title="Retry me")
        fresh_player = SimpleNamespace(title="Fresh")

        with patch(
            "music_service.YTDLSource.from_url",
            new=AsyncMock(return_value=fresh_player),
        ) as from_url:
            await self.service.retry_player_once(player, self.guild_id)
            await self.service.retry_player_once(player, self.guild_id)

        self.assertEqual(player._retries, 1)
        self.assertEqual(self.state.get_queue(self.guild_id), [fresh_player])
        from_url.assert_awaited_once_with("https://retry")

    async def test_build_after_play_callback_retries_and_advances_on_error(self):
        captured = {}
        player = SimpleNamespace(title="Demo")
        self.service.retry_player_once = AsyncMock()
        self.service.play_next = AsyncMock()

        def fake_run_coroutine_threadsafe(coro, loop):
            captured["coro"] = coro

            class DoneFuture:
                def result(self_inner):
                    return None

            return DoneFuture()

        with patch(
            "music_service.asyncio.run_coroutine_threadsafe",
            side_effect=fake_run_coroutine_threadsafe,
        ):
            callback = self.service.build_after_play_callback(
                player, self.guild_id, 555
            )
            callback(RuntimeError("stream error"))

        await captured["coro"]

        self.service.retry_player_once.assert_awaited_once_with(player, self.guild_id)
        self.service.play_next.assert_awaited_once_with(self.guild_id, 555)

    async def test_play_next_starts_playback_and_announces_song(self):
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        player = SimpleNamespace(title="Demo", url="https://demo", message_sent=False)
        self.client.get_guild.return_value = guild
        self.service.get_next_ready_player = AsyncMock(return_value=player)
        self.service.build_after_play_callback = Mock(return_value="callback")
        self.service.announce_now_playing = AsyncMock()

        await self.service.play_next(self.guild_id, 777)

        voice_client.play.assert_called_once_with(player, after="callback")
        self.service.announce_now_playing.assert_awaited_once_with(
            self.guild_id, player
        )
        self.assertEqual(self.state.text_channels[self.guild_id], 777)

    async def test_play_next_disconnects_when_queue_is_empty_and_not_loading(self):
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        self.client.get_guild.return_value = guild
        self.service.get_next_ready_player = AsyncMock(return_value=None)
        self.service.disconnect_for_empty_queue = AsyncMock()

        await self.service.play_next(self.guild_id, 888)

        self.service.disconnect_for_empty_queue.assert_awaited_once_with(
            guild,
            guild_id=self.guild_id,
            success_log=f"Disconnected from voice channel in guild {self.guild_id}",
            already_disconnected_log=(
                "Bot already disconnected from guild "
                f"{self.guild_id}, skipping queue empty disconnect."
            ),
            warning_context="Failed to send disconnect message",
        )

    async def test_play_next_returns_cleanly_when_guild_is_missing(self):
        self.client.get_guild.return_value = None
        self.service.get_next_ready_player = AsyncMock()
        self.service.disconnect_for_empty_queue = AsyncMock()

        await self.service.play_next(self.guild_id, 901)

        self.service.get_next_ready_player.assert_not_awaited()
        self.service.disconnect_for_empty_queue.assert_not_awaited()
        self.assertEqual(self.state.text_channels[self.guild_id], 901)

    async def test_play_next_returns_cleanly_when_voice_client_is_missing(self):
        guild = self.make_guild(voice_client=None)
        self.client.get_guild.return_value = guild
        self.service.get_next_ready_player = AsyncMock()
        self.service.disconnect_for_empty_queue = AsyncMock()

        await self.service.play_next(self.guild_id, 902)

        self.service.get_next_ready_player.assert_not_awaited()
        self.service.disconnect_for_empty_queue.assert_not_awaited()
        self.assertEqual(self.state.text_channels[self.guild_id], 902)

    async def test_play_next_waits_for_playlist_loading_before_disconnecting(self):
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        self.client.get_guild.return_value = guild
        self.state.loading_playlists[self.guild_id] = True
        self.service.get_next_ready_player = AsyncMock(return_value=None)
        self.service.wait_for_queue_during_playlist_load = AsyncMock(return_value=False)
        self.service.disconnect_for_empty_queue = AsyncMock()

        await self.service.play_next(self.guild_id, 999)

        self.service.wait_for_queue_during_playlist_load.assert_awaited_once_with(
            self.guild_id, 999
        )
        self.service.disconnect_for_empty_queue.assert_awaited_once_with(
            guild,
            guild_id=self.guild_id,
            success_log=(
                f"Playlist loading timeout in guild {self.guild_id}. Disconnected."
            ),
            already_disconnected_log=(
                "Bot already disconnected from guild "
                f"{self.guild_id}, skipping timeout disconnect."
            ),
            warning_context="Failed to send timeout disconnect message",
        )

    async def test_play_next_does_not_disconnect_when_song_appears_during_loading(self):
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        self.client.get_guild.return_value = guild
        self.state.loading_playlists[self.guild_id] = True
        self.service.get_next_ready_player = AsyncMock(return_value=None)
        self.service.wait_for_queue_during_playlist_load = AsyncMock(return_value=True)
        self.service.disconnect_for_empty_queue = AsyncMock()

        await self.service.play_next(self.guild_id, 1000)

        self.service.disconnect_for_empty_queue.assert_not_awaited()

    async def test_play_next_skips_disconnect_if_voice_client_disappears(
        self,
    ):
        voice_client = FakeVoiceClient()
        guild = self.make_guild(voice_client)
        self.client.get_guild.return_value = guild
        self.state.loading_playlists[self.guild_id] = True
        self.service.get_next_ready_player = AsyncMock(return_value=None)
        self.service.disconnect_for_empty_queue = AsyncMock()

        async def wait_for_queue(guild_id, text_channel_id):
            guild.voice_client = None
            return False

        self.service.wait_for_queue_during_playlist_load = AsyncMock(
            side_effect=wait_for_queue
        )

        await self.service.play_next(self.guild_id, 1001)

        self.service.wait_for_queue_during_playlist_load.assert_awaited_once_with(
            self.guild_id, 1001
        )
        self.service.disconnect_for_empty_queue.assert_not_awaited()

    async def test_on_voice_state_update_cleans_state_when_bot_leaves_voice(self):
        member = SimpleNamespace(id=self.client.user.id)
        before = SimpleNamespace(channel=SimpleNamespace(guild=SimpleNamespace(id=55)))
        after = SimpleNamespace(channel=None)
        self.state.cleanup_guild = Mock()

        await self.service.on_voice_state_update(member, before, after)

        self.state.cleanup_guild.assert_called_once_with(55)

    async def test_on_voice_state_update_disconnects_when_bot_is_left_alone(self):
        guild = self.make_guild(FakeVoiceClient())
        member = SimpleNamespace(id=123, guild=guild)
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=None)
        self.service.get_bot_voice_channel = Mock(return_value=object())
        self.service.is_bot_alone_in_channel = Mock(return_value=True)
        self.service.disconnect_guild_voice = AsyncMock()

        await self.service.on_voice_state_update(member, before, after)

        self.service.disconnect_guild_voice.assert_awaited_once_with(
            guild,
            guild_id=guild.id,
            message="No one on the voice channel, disconnecting. See ya!",
            warning_context="Failed to send alone disconnect message",
            already_disconnected_log=(
                f"Bot already disconnected from guild {guild.id} by another event."
            ),
            success_log=(
                f"Disconnected from voice channel in guild {guild.id} (bot was alone)."
            ),
        )

    async def test_on_voice_state_update_stays_connected_if_someone_rejoins(self):
        guild = self.make_guild(FakeVoiceClient())
        member = SimpleNamespace(id=123, guild=guild)
        before = SimpleNamespace(channel=None)
        after = SimpleNamespace(channel=None)
        self.state.alone_disconnect_delay = 5
        self.service.get_bot_voice_channel = Mock(side_effect=[object(), object()])
        self.service.is_bot_alone_in_channel = Mock(side_effect=[True, False])
        self.service.disconnect_guild_voice = AsyncMock()

        with patch("music_service.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            await self.service.on_voice_state_update(member, before, after)

        sleep_mock.assert_awaited_once_with(5)
        self.service.disconnect_guild_voice.assert_not_awaited()
