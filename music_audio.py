from __future__ import annotations

import asyncio
import logging
import os

import discord
import yt_dlp as youtube_dl

logger = logging.getLogger(__name__)

BASE_YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio[ext=m4a]/bestaudio[acodec!=none]/bestaudio/best",
    "noplaylist": False,
    "playlist_items": "1-50",
    "quiet": False,
    "no_warnings": False,
    "verbose": False,
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
    "retries": 5,
    "fragment_retries": 5,
}

YTDL_CLIENT_FALLBACKS = (
    None,
    {"youtube": {"player_client": ["web"]}},
    {"youtube": {"player_client": ["ios"]}},
    {"youtube": {"player_client": ["tv"]}},
)

COOKIE_PATHS = ("/app/cookies.txt", "cookies.txt")

ytdl_format_options = dict(BASE_YTDL_FORMAT_OPTIONS)
for cookie_path in COOKIE_PATHS:
    if os.path.exists(cookie_path):
        ytdl_format_options["cookiefile"] = cookie_path
        break

ffmpeg_options = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
    "options": "-vn",
}


def build_ytdl_options(**overrides):
    """Build yt-dlp options without mutating the shared defaults."""
    options = dict(ytdl_format_options)
    extractor_args = overrides.pop("extractor_args", None)
    options.update(overrides)
    if extractor_args is not None:
        options["extractor_args"] = extractor_args
    return options


def describe_youtube_client(extractor_args) -> str:
    """Return a human-readable label for the configured YouTube client."""
    if not extractor_args:
        return "default client"

    youtube_args = extractor_args.get("youtube", {})
    player_clients = youtube_args.get("player_client") or []
    if not player_clients:
        return "custom client"
    return ", ".join(player_clients)


def extract_info_with_fallback(url: str, **overrides):
    """Try yt-dlp extraction with a small YouTube client fallback chain."""
    attempts: list[tuple[str, str]] = []

    for extractor_args in YTDL_CLIENT_FALLBACKS:
        options = build_ytdl_options(**overrides)
        if extractor_args is None:
            options.pop("extractor_args", None)
        else:
            options["extractor_args"] = extractor_args

        client_label = describe_youtube_client(options.get("extractor_args"))
        try:
            logger.info(f"Trying yt-dlp extraction with {client_label}: {url}")
            return youtube_dl.YoutubeDL(options).extract_info(url, download=False)
        except Exception as exc:
            attempts.append((client_label, str(exc)))
            logger.warning(
                f"yt-dlp extraction failed with {client_label} for {url}: {exc}"
            )

    last_client, last_error = attempts[-1]
    raise RuntimeError(
        "yt-dlp could not extract this URL after trying multiple YouTube clients. "
        f"Last attempt ({last_client}): {last_error}"
    )


async def extract_info_async(url: str, **overrides) -> dict:
    """Run yt-dlp extraction in the executor used by the Discord bot."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: extract_info_with_fallback(url, **overrides)
    )


def get_first_available_entry(data: dict) -> dict:
    """Return the first playable entry when yt-dlp returns playlist-like data."""
    entries = [entry for entry in data.get("entries", []) if entry]
    if not entries:
        raise RuntimeError("Empty playlist or no accessible entries.")
    return entries[0]


def get_entry_url(entry: dict) -> str | None:
    """Return the most useful URL field from an extracted yt-dlp entry."""
    return entry.get("webpage_url") or entry.get("original_url") or entry.get("url")


def require_stream_url(data: dict) -> str:
    """Return the extracted stream URL or raise a user-friendly error."""
    stream_url = data.get("url")
    if stream_url:
        return stream_url

    title = data.get("title", "Unknown")
    raise RuntimeError(
        f"No stream URL for '{title}' (video may be age-restricted, "
        "private, or SABR-protected). Try a different video."
    )


def create_ffmpeg_source(stream_url: str) -> discord.FFmpegPCMAudio:
    """Create the FFmpeg audio source used by discord.py playback."""
    return discord.FFmpegPCMAudio(stream_url, **ffmpeg_options)


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, lazy_entry=None):
        if source is not None:
            super().__init__(source)
        else:
            self.original = None
            self.source = None
            self.volume = 0.5
            self._volume = 0.5

        self.title = data.get("title", "Unknown Title")
        self.url = data.get("webpage_url", data.get("original_url", ""))
        self._retries = 0
        self.lazy_entry = lazy_entry
        self.is_lazy = lazy_entry is not None
        self.message_sent = False

    @classmethod
    async def from_url(cls, url: str):
        try:
            data = await extract_info_async(url)
        except Exception as exc:
            raise RuntimeError(f"Failed to extract info from {url}: {exc}")

        if "entries" in data:
            data = get_first_available_entry(data)

        return cls(create_ffmpeg_source(require_stream_url(data)), data=data)

    async def get_actual_source(self):
        """If this is a lazy player, fetch the actual source now."""
        if not self.is_lazy:
            return self

        try:
            entry_url = get_entry_url(self.lazy_entry)
            if not entry_url:
                raise RuntimeError("No URL found in lazy entry")

            data = await extract_info_async(entry_url)
            actual_source = create_ffmpeg_source(require_stream_url(data))
            self.original = actual_source
            self.source = actual_source
            self.is_lazy = False
            return self
        except Exception as exc:
            raise RuntimeError(f"Failed to load lazy entry: {exc}")

    @classmethod
    async def from_entry(cls, entry: dict, lazy: bool = False):
        """Create a player from an already-extracted entry."""
        if lazy:
            return cls(None, data=entry, lazy_entry=entry)

        filename = entry.get("url")
        if filename:
            return cls(create_ffmpeg_source(filename), data=entry)

        try:
            entry_url = get_entry_url(entry)
            if not entry_url:
                raise RuntimeError("No URL found in entry")

            data = await extract_info_async(entry_url)
            return cls(create_ffmpeg_source(require_stream_url(data)), data=data)
        except Exception as exc:
            raise RuntimeError(f"Failed to extract stream from entry: {exc}")


async def create_player_from_entry(
    entry: dict, *, use_entry_method: bool = False, lazy: bool = False
) -> YTDLSource:
    """Create a playable YTDLSource from an extracted entry."""
    if use_entry_method:
        return await YTDLSource.from_entry(entry, lazy=lazy)

    entry_url = get_entry_url(entry)
    if not entry_url:
        raise RuntimeError("No URL found in entry")
    return await YTDLSource.from_url(entry_url)


def get_playlist_entries(playlist_info: dict) -> list[dict]:
    """Return only non-empty playlist entries."""
    return [entry for entry in playlist_info.get("entries", []) if entry]


def get_playlist_entry_url(entry: dict) -> str | None:
    """Normalize a playlist entry into a direct YouTube watch URL when possible."""
    video_url = entry.get("url") or entry.get("webpage_url")
    if video_url:
        return video_url

    video_id = entry.get("id")
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def build_playlist_summary(queued_count: int, skipped_count: int) -> str:
    """Build the user-facing summary for background playlist loading."""
    summary = f"Added **{queued_count}** more songs to queue from playlist."
    if skipped_count > 0:
        summary += f" (Skipped {skipped_count} unavailable videos)"
    return summary


def build_queue_page_message(queue: list[YTDLSource], page: int, per_page: int) -> str:
    """Render one queue page as a Discord-friendly message."""
    total_pages = (len(queue) + per_page - 1) // per_page
    start = (page - 1) * per_page
    songs_to_display = queue[start : start + per_page]
    lines = [
        f"{index}. [{song.title}]({song.url})"
        for index, song in enumerate(songs_to_display, start + 1)
    ]
    header = f"Queue ({len(queue)} songs) - Page {page}/{total_pages}:\n"
    return header + "\n".join(lines)
