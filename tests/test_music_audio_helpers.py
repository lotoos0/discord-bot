import unittest
from types import SimpleNamespace

from tests.module_stubs import install_test_stubs

install_test_stubs()

from music_audio import (
    build_playlist_summary,
    build_queue_page_message,
    get_first_available_entry,
    get_playlist_entry_url,
    require_stream_url,
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
