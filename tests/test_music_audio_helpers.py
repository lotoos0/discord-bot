import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.module_stubs import install_test_stubs

install_test_stubs()

from music_audio import (
    build_playlist_summary,
    build_queue_page_message,
    create_player_from_entry,
    get_first_available_entry,
    get_playlist_entry_url,
    require_stream_url,
)


class MusicAudioLazySourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_player_creates_ffmpeg_only_when_resolved(self):
        entry = {
            "title": "Queued track",
            "webpage_url": "https://example.com/watch",
        }
        extracted_data = {
            "title": "Queued track",
            "url": "https://example.com/stream",
            "webpage_url": entry["webpage_url"],
        }
        ffmpeg_source = Mock()

        with patch(
            "music_audio.extract_info_async",
            new=AsyncMock(return_value=extracted_data),
        ) as extract_info, patch(
            "music_audio.create_ffmpeg_source",
            return_value=ffmpeg_source,
        ) as create_ffmpeg_source:
            player = await create_player_from_entry(
                entry,
                use_entry_method=True,
                lazy=True,
            )

            self.assertTrue(player.is_lazy)
            create_ffmpeg_source.assert_not_called()

            resolved_player = await player.get_actual_source()

        self.assertIs(resolved_player, player)
        self.assertFalse(player.is_lazy)
        self.assertIs(player.source, ffmpeg_source)
        extract_info.assert_awaited_once_with(entry["webpage_url"])
        create_ffmpeg_source.assert_called_once_with(
            extracted_data["url"],
            None,
        )


class MusicAudioHelperTests(unittest.TestCase):
    def test_get_playlist_entry_url_prefers_direct_url(self):
        entry = {
            "url": "https://youtube.test/watch?v=abc",
            "id": "abc",
        }

        self.assertEqual(
            get_playlist_entry_url(entry),
            "https://youtube.test/watch?v=abc",
        )

    def test_get_playlist_entry_url_builds_watch_url_from_id(self):
        self.assertEqual(
            get_playlist_entry_url({"id": "abc123"}),
            "https://www.youtube.com/watch?v=abc123",
        )

    def test_get_first_available_entry_returns_first_non_empty_entry(self):
        playlist_info = {"entries": [None, {"title": "Track 1"}, {"title": "Track 2"}]}

        self.assertEqual(get_first_available_entry(playlist_info), {"title": "Track 1"})

    def test_get_first_available_entry_raises_when_playlist_is_empty(self):
        with self.assertRaisesRegex(RuntimeError, "Empty playlist"):
            get_first_available_entry({"entries": [None, None]})

    def test_require_stream_url_raises_helpful_error_when_missing(self):
        with self.assertRaisesRegex(RuntimeError, "No stream URL for 'Demo Song'"):
            require_stream_url({"title": "Demo Song"})

    def test_build_playlist_summary_includes_skipped_suffix_only_when_needed(self):
        self.assertEqual(
            build_playlist_summary(2, 1),
            "Added **2** more songs to queue from playlist. (Skipped 1 unavailable videos)",
        )
        self.assertEqual(
            build_playlist_summary(3, 0),
            "Added **3** more songs to queue from playlist.",
        )

    def test_build_queue_page_message_renders_expected_page(self):
        queue = [
            SimpleNamespace(title="Song A", url="https://example.com/a"),
            SimpleNamespace(title="Song B", url="https://example.com/b"),
            SimpleNamespace(title="Song C", url="https://example.com/c"),
        ]

        message = build_queue_page_message(queue, page=2, per_page=2)

        self.assertEqual(
            message,
            "Queue (3 songs) - Page 2/2:\n3. [Song C](https://example.com/c)",
        )
