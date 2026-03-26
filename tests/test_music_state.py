import unittest
from unittest.mock import Mock

from music_state import MusicState


class MusicStateTests(unittest.TestCase):
    def test_get_queue_returns_same_list_for_same_guild(self):
        state = MusicState()

        queue = state.get_queue(123)
        queue.append("song")

        self.assertEqual(state.get_queue(123), ["song"])

    def test_cleanup_guild_clears_queue_resets_flags_and_cancels_pending_task(self):
        state = MusicState()
        guild_id = 123
        pending_task = Mock()
        pending_task.done.return_value = False

        state.get_queue(guild_id).append("song")
        state.loading_playlists[guild_id] = True
        state.loading_tasks[guild_id] = pending_task
        state.remember_text_channel(guild_id, 456)

        state.cleanup_guild(guild_id)

        self.assertEqual(state.get_queue(guild_id), [])
        self.assertFalse(state.loading_playlists[guild_id])
        pending_task.cancel.assert_called_once_with()
        self.assertNotIn(guild_id, state.loading_tasks)
        self.assertNotIn(guild_id, state.text_channels)

    def test_finish_playlist_loading_resets_flag_and_removes_task_without_cancelling(
        self,
    ):
        state = MusicState()
        guild_id = 321
        task = Mock()

        state.loading_playlists[guild_id] = True
        state.loading_tasks[guild_id] = task

        state.finish_playlist_loading(guild_id)

        self.assertFalse(state.loading_playlists[guild_id])
        self.assertNotIn(guild_id, state.loading_tasks)
        task.cancel.assert_not_called()

    def test_remember_text_channel_stores_latest_channel(self):
        state = MusicState()

        state.remember_text_channel(11, 99)

        self.assertEqual(state.text_channels[11], 99)
