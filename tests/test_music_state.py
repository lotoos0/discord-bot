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
        self.assertFalse(state.loading_playlists.get(guild_id, False))
        pending_task.cancel.assert_called_once_with()
        self.assertNotIn(guild_id, state.loading_tasks)
        self.assertNotIn(guild_id, state.text_channels)

    def test_cleanup_guild_keeps_other_guild_state_untouched(self):
        state = MusicState()
        guild_id = 123
        other_guild_id = 456
        pending_task = Mock()
        pending_task.done.return_value = False
        other_task = Mock()
        other_task.done.return_value = False

        state.get_queue(guild_id).append("song-a")
        state.get_queue(other_guild_id).append("song-b")
        state.loading_playlists[guild_id] = True
        state.loading_playlists[other_guild_id] = True
        state.loading_tasks[guild_id] = pending_task
        state.loading_tasks[other_guild_id] = other_task
        state.remember_text_channel(guild_id, 111)
        state.remember_text_channel(other_guild_id, 222)

        state.cleanup_guild(guild_id)

        self.assertEqual(state.get_queue(guild_id), [])
        self.assertEqual(state.get_queue(other_guild_id), ["song-b"])
        self.assertFalse(state.loading_playlists.get(guild_id, False))
        self.assertTrue(state.loading_playlists.get(other_guild_id, False))
        pending_task.cancel.assert_called_once_with()
        other_task.cancel.assert_not_called()
        self.assertNotIn(guild_id, state.loading_tasks)
        self.assertIn(other_guild_id, state.loading_tasks)
        self.assertNotIn(guild_id, state.text_channels)
        self.assertEqual(state.text_channels[other_guild_id], 222)

    def test_cleanup_guild_does_not_cancel_completed_task(self):
        state = MusicState()
        guild_id = 123
        completed_task = Mock()
        completed_task.done.return_value = True

        state.loading_tasks[guild_id] = completed_task

        state.cleanup_guild(guild_id)

        completed_task.cancel.assert_not_called()
        self.assertNotIn(guild_id, state.loading_tasks)

    def test_finish_playlist_loading_resets_flag_and_removes_task_without_cancelling(
        self,
    ):
        state = MusicState()
        guild_id = 321
        task = Mock()

        state.loading_playlists[guild_id] = True
        state.loading_tasks[guild_id] = task

        state.finish_playlist_loading(guild_id)

        self.assertFalse(state.loading_playlists.get(guild_id, False))
        self.assertNotIn(guild_id, state.loading_tasks)
        task.cancel.assert_not_called()

    def test_finish_playlist_loading_only_clears_target_guild(self):
        state = MusicState()
        guild_id = 321
        other_guild_id = 654
        task = Mock()
        other_task = Mock()

        state.loading_playlists[guild_id] = True
        state.loading_playlists[other_guild_id] = True
        state.loading_tasks[guild_id] = task
        state.loading_tasks[other_guild_id] = other_task

        state.finish_playlist_loading(guild_id)

        self.assertFalse(state.loading_playlists.get(guild_id, False))
        self.assertTrue(state.loading_playlists.get(other_guild_id, False))
        self.assertNotIn(guild_id, state.loading_tasks)
        self.assertIn(other_guild_id, state.loading_tasks)
        task.cancel.assert_not_called()
        other_task.cancel.assert_not_called()

    def test_remember_text_channel_stores_latest_channel(self):
        state = MusicState()

        state.remember_text_channel(11, 99)

        self.assertEqual(state.text_channels[11], 99)
