import asyncio
import logging
import os
import random
from collections import defaultdict

import discord
import yt_dlp as youtube_dl
from discord import app_commands
from dotenv import load_dotenv

# ---- Logging configuration ----
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---- yt-dlp / ffmpeg settings ----
BASE_YTDL_FORMAT_OPTIONS = {
    # Fallback chain: m4a → best available audio → best overall
    "format": "bestaudio[ext=m4a]/bestaudio[acodec!=none]/bestaudio/best",
    "noplaylist": False,
    "playlist_items": "1-50",
    "quiet": False,
    "no_warnings": False,
    # Suppress verbose debug logs from yt-dlp (but keep errors)
    "verbose": False,
    # Force IPv4 (helps when running inside Docker)
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
cookies_paths = ["/app/cookies.txt", "cookies.txt"]
ytdl_format_options = dict(BASE_YTDL_FORMAT_OPTIONS)
for cookies in cookies_paths:
    if os.path.exists(cookies):
        ytdl_format_options["cookiefile"] = cookies
        break

ffmpeg_options = {
    # Auto-reconnect and disable stdin blocking
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
    return (
        entry.get("webpage_url")
        or entry.get("original_url")
        or entry.get("url")
    )


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


# ---- Audio source wrapper ----
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, lazy_entry=None):
        # For lazy players, source is None - we'll set it later
        if source is not None:
            super().__init__(source)
        else:
            # For lazy players: don't call super().__init__, just set attributes manually
            self.original = None
            self.source = None
            self.volume = 0.5
            self._volume = 0.5

        self.title = data.get("title", "Unknown Title")
        self.url = data.get("webpage_url", data.get("original_url", ""))
        self._retries = 0
        # Store entry data for lazy loading (fetching URL later)
        self.lazy_entry = lazy_entry
        self.is_lazy = lazy_entry is not None
        # Flag to prevent duplicate "Now playing" messages
        self.message_sent = False

    @classmethod
    async def from_url(cls, url):
        try:
            data = await extract_info_async(url)
        except Exception as e:
            # Re-raise with context for better error messages
            raise RuntimeError(f"Failed to extract info from {url}: {e}")

        # Handle playlist case: take the first real entry
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

            # Create actual FFmpeg source with the stream URL
            actual_source = create_ffmpeg_source(require_stream_url(data))
            # Properly set up the PCMVolumeTransformer
            self.original = actual_source
            self.source = actual_source
            self.is_lazy = False
            return self
        except Exception as e:
            raise RuntimeError(f"Failed to load lazy entry: {e}")

    @classmethod
    async def from_entry(cls, entry: dict, lazy: bool = False):
        """Create YTDLSource from already-extracted playlist entry (no re-fetching).

        If lazy=True, store entry for later retrieval (no actual audio source created yet).
        """
        if lazy:
            # Create a lazy player - store entry, don't create source yet
            # Use a placeholder entry to avoid creating FFmpeg source
            return cls(None, data=entry, lazy_entry=entry)

        # Try to use the stream URL from entry, or fetch it if missing
        filename = entry.get("url")
        if filename:
            return cls(create_ffmpeg_source(filename), data=entry)

        # If no URL, we need to extract it
        try:
            entry_url = get_entry_url(entry)
            if not entry_url:
                raise RuntimeError("No URL found in entry")
            data = await extract_info_async(entry_url)
            return cls(create_ffmpeg_source(require_stream_url(data)), data=data)
        except Exception as e:
            raise RuntimeError(f"Failed to extract stream from entry: {e}")


# ---- Discord client + intents ----
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True  # explicitly enabled for voice handling


class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Global sync (may take a few seconds on first run)
        await self.tree.sync(guild=None)


client = MyClient(intents=intents)

# ---- Per-guild queues ----
# guild_id -> list[YTDLSource]
queues: dict[int, list[YTDLSource]] = defaultdict(list)
# Track if playlists are being loaded for a guild (to prevent early disconnect)
loading_playlists: dict[int, bool] = defaultdict(bool)
# Track active playlist loading tasks to prevent overlapping
loading_tasks: dict[int, asyncio.Task] = {}
# Track last text channel for each guild (for sending messages)
text_channels: dict[int, int] = {}
# Locks to prevent race condition when disconnecting (ensures only one disconnect message)
disconnect_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# Maximum songs in queue per guild
MAX_QUEUE_SIZE = 100
# Delay in seconds before disconnecting when bot is alone (0 = immediate, can be changed to 180 for 3 min, etc.)
ALONE_DISCONNECT_DELAY = 0


def get_queue(guild_id: int) -> list[YTDLSource]:
    return queues[guild_id]


def cleanup_guild(guild_id: int):
    """Clean up guild state when disconnecting."""
    if guild_id in queues:
        queues[guild_id].clear()
    loading_playlists[guild_id] = False
    # Cancel any pending loading tasks
    if guild_id in loading_tasks:
        task = loading_tasks.pop(guild_id)
        if not task.done():
            task.cancel()
    # Remove text channel tracking
    text_channels.pop(guild_id, None)


def finish_playlist_loading(guild_id: int):
    """Mark background playlist loading as finished for a guild."""
    loading_playlists[guild_id] = False
    loading_tasks.pop(guild_id, None)


def remember_text_channel(guild_id: int, channel_id: int):
    """Store the last text channel used by a guild command."""
    text_channels[guild_id] = channel_id


def get_guild_text_channel(guild_id: int) -> discord.TextChannel | None:
    """Return the remembered text channel for a guild, if still available."""
    channel_id = text_channels.get(guild_id)
    if channel_id is None:
        return None

    channel = client.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel
    return None


async def send_channel_message(channel, message: str, warning_context: str) -> bool:
    """Send a message and log a warning if Discord rejects it."""
    if channel is None:
        return False

    try:
        await channel.send(message)
        return True
    except Exception as exc:
        logger.warning(f"{warning_context}: {exc}")
        return False


async def send_guild_message(
    guild_id: int, message: str, warning_context: str
) -> bool:
    """Send a message to the guild's remembered text channel."""
    return await send_channel_message(
        get_guild_text_channel(guild_id), message, warning_context
    )


def get_bot_voice_channel(guild: discord.Guild):
    """Return the bot's active voice/stage channel for a guild."""
    voice_client = guild.voice_client
    if voice_client is None or voice_client.channel is None:
        return None

    channel = voice_client.channel
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return channel
    return None


def is_bot_alone_in_channel(channel) -> bool:
    """Return True when no human members remain in the bot's current channel."""
    return not any(not member.bot for member in channel.members)


async def disconnect_guild_voice(
    guild: discord.Guild,
    *,
    guild_id: int,
    message: str | None,
    warning_context: str,
    already_disconnected_log: str,
    success_log: str,
):
    """Disconnect from voice once, send an optional text message, and clean up state."""
    async with disconnect_locks[guild_id]:
        if guild.voice_client is None:
            logger.info(already_disconnected_log)
            return

        if message:
            await send_guild_message(guild_id, message, warning_context)

        await guild.voice_client.disconnect(force=False)
        logger.info(success_log)

    cleanup_guild(guild_id)


def get_requester_voice_channel(interaction: discord.Interaction):
    """Return the requesting user's current voice channel, if any."""
    if interaction.user.voice:
        return interaction.user.voice.channel
    return None


async def ensure_bot_connected(interaction: discord.Interaction) -> bool:
    """Connect the bot to the requester's voice channel when needed."""
    if interaction.guild.voice_client is not None:
        return True

    channel = get_requester_voice_channel(interaction)
    if channel is None:
        await interaction.followup.send(
            "You must be in a voice channel!", ephemeral=True
        )
        return False

    try:
        await channel.connect()
        return True
    except Exception as exc:
        await interaction.followup.send(
            f"Failed to connect: {exc} (missing permissions or bot is banned?)",
            ephemeral=True,
        )
        return False


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


async def enqueue_entry(
    guild_id: int,
    channel,
    entry: dict,
    *,
    announce: bool = True,
    use_entry_method: bool = False,
    lazy: bool = False,
) -> bool:
    """Create a player from an entry and append it to the guild queue."""
    try:
        player = await create_player_from_entry(
            entry, use_entry_method=use_entry_method, lazy=lazy
        )
    except Exception as exc:
        logger.error(f"Error enqueueing song: {exc}", exc_info=True)
        await send_channel_message(
            channel,
            f"Skipped one item (error): {exc}",
            "Failed to send enqueue error message",
        )
        return False

    queue = get_queue(guild_id)
    if len(queue) >= MAX_QUEUE_SIZE:
        if announce:
            await send_channel_message(
                channel,
                f"Queue is full (max {MAX_QUEUE_SIZE} songs)!",
                "Failed to send queue full message",
            )
        return False

    queue.append(player)
    if announce:
        await send_channel_message(
            channel,
            f"Added to queue: **[{player.title}]({player.url})**",
            "Failed to send queue addition message",
        )
    return True


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


async def enqueue_playlist_entries(guild_id: int, entries: list[dict]) -> tuple[int, int]:
    """Enqueue playlist entries one by one, skipping failures without aborting."""
    queued_count = 0
    skipped_count = 0

    for entry in entries:
        try:
            video_url = get_playlist_entry_url(entry)
            if not video_url:
                logger.warning(f"Could not get URL for entry: {entry}")
                skipped_count += 1
                continue

            player = await YTDLSource.from_url(video_url)
            queue = get_queue(guild_id)
            if len(queue) < MAX_QUEUE_SIZE:
                queue.append(player)
                queued_count += 1
        except Exception as exc:
            logger.warning(
                f"Skipped unavailable/errored video: {entry.get('id', 'unknown')} - {exc}"
            )
            skipped_count += 1

    return queued_count, skipped_count


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


# ---- Events ----
@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}. Slash commands synchronized.")


@client.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    """Clean up guild state when bot leaves a voice channel, and auto-disconnect when alone."""
    # Case 1: Bot left the channel
    if member.id == client.user.id:
        if before.channel is not None and after.channel is None:
            guild_id = before.channel.guild.id
            logger.info(f"Bot left voice channel in guild {guild_id}. Cleaning up.")
            cleanup_guild(guild_id)
        return

    # Case 2: Someone else joined/left/moved - check if bot is now alone
    guild = member.guild
    bot_channel = get_bot_voice_channel(guild)
    if bot_channel is None:
        return

    if is_bot_alone_in_channel(bot_channel):
        logger.info(
            f"Bot is alone in voice channel in guild {guild.id}. "
            f"Disconnecting after {ALONE_DISCONNECT_DELAY}s delay."
        )

        if ALONE_DISCONNECT_DELAY > 0:
            await asyncio.sleep(ALONE_DISCONNECT_DELAY)
            bot_channel = get_bot_voice_channel(guild)
            if bot_channel is None:
                return
            if not is_bot_alone_in_channel(bot_channel):
                logger.info(
                    f"Someone rejoined voice channel in guild {guild.id}. Staying connected."
                )
                return

        await disconnect_guild_voice(
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

# ---- Slash commands ----
@client.tree.command(name="join", description="Join the voice channel")
async def join(interaction: discord.Interaction):
    channel = get_requester_voice_channel(interaction)
    if channel is None:
        await interaction.response.send_message(
            "You must be in a voice channel!", ephemeral=True
        )
        return
    await channel.connect()
    await interaction.response.send_message(f"Joined the channel {channel}!")


@client.tree.command(name="leave", description="Leave the voice channel")
async def leave(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        await interaction.response.send_message("Bot has left the voice channel!")
    else:
        await interaction.response.send_message(
            "I am not in a voice channel!", ephemeral=True
        )


async def _handle_music_request(interaction: discord.Interaction, url: str):
    """Shared logic for /play and /add commands."""
    text_channel_id = interaction.channel.id
    guild_id = interaction.guild.id
    remember_text_channel(guild_id, text_channel_id)

    try:
        first_info = await extract_info_async(url, noplaylist=True)
    except Exception as exc:
        await interaction.followup.send(f"Cannot process URL: {exc}", ephemeral=True)
        return

    await enqueue_entry(
        guild_id,
        interaction.channel,
        first_info,
        announce=False,
        use_entry_method=True,
    )
    if not interaction.guild.voice_client.is_playing():
        await play_next(guild_id, text_channel_id)

    loading_playlists[guild_id] = True

    async def fetch_and_enqueue_rest():
        try:
            playlist_info = await extract_info_async(url, extract_flat="in_playlist")
            entries = get_playlist_entries(playlist_info)
            if not entries:
                logger.info(f"URL {url} is not a playlist, skipping background queue.")
                return

            queued_count, skipped_count = await enqueue_playlist_entries(
                guild_id, entries[1:]
            )
            if queued_count > 0:
                await send_channel_message(
                    interaction.channel,
                    build_playlist_summary(queued_count, skipped_count),
                    "Failed to send playlist summary message",
                )

            logger.info(
                f"Finished queueing {queued_count} additional songs in guild {guild_id} (skipped {skipped_count})."
            )
        except Exception as exc:
            logger.error(
                f"Error fetching full playlist in background: {exc}", exc_info=True
            )
        finally:
            finish_playlist_loading(guild_id)

    loading_tasks[guild_id] = asyncio.create_task(fetch_and_enqueue_rest())

    logger.info(
        f"First song queued immediately in guild {guild_id}, fetching rest in background..."
    )
    await interaction.followup.send(
        "First song queued! Fetching rest of playlist in background...", ephemeral=True
    )

@client.tree.command(
    name="play", description="Join voice channel and play music (URL or playlist)"
)
async def play(interaction: discord.Interaction, url: str):
    """Join VC and start playing music."""
    await interaction.response.defer(ephemeral=True)

    if not await ensure_bot_connected(interaction):
        return
    await _handle_music_request(interaction, url)


@client.tree.command(
    name="add", description="Add music to queue (bot must already be playing)"
)
async def add(interaction: discord.Interaction, url: str):
    """Add music to existing queue."""
    await interaction.response.defer(ephemeral=True)

    # Check if bot is in VC
    if interaction.guild.voice_client is None:
        await interaction.followup.send(
            "Bot is not in a voice channel! Use `/play` to start playing first.",
            ephemeral=True,
        )
        return

    await _handle_music_request(interaction, url)


@client.tree.command(name="queue", description="Display the queue")
@discord.app_commands.describe(page="Page number (20 songs per page)")
async def queue_list(interaction: discord.Interaction, page: int = 1):
    await interaction.response.defer(ephemeral=True)
    queue = get_queue(interaction.guild.id)
    if not queue:
        await interaction.followup.send("The queue is empty!", ephemeral=True)
        return

    per_page = 20
    total_pages = (len(queue) + per_page - 1) // per_page
    if page < 1 or page > total_pages:
        await interaction.followup.send(
            f"Invalid page. Available pages: 1-{total_pages}", ephemeral=True
        )
        return

    await interaction.followup.send(
        build_queue_page_message(queue, page, per_page), ephemeral=False
    )

@client.tree.command(name="skip", description="Skip the currently playing song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()  # Will trigger after-callback and move to the next
        await interaction.response.send_message("Skipped!")
    else:
        await interaction.response.send_message(
            "No music is currently playing!", ephemeral=True
        )


@client.tree.command(name="clearqueue", description="Clear the entire queue")
async def clearqueue(interaction: discord.Interaction):
    get_queue(interaction.guild.id).clear()
    await interaction.response.send_message("The queue has been cleared!")


@client.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    if not interaction.guild:
        return
    queue = get_queue(interaction.guild.id)
    if not queue:
        await interaction.response.send_message(
            "The queue is empty! Nothing to shuffle.", ephemeral=True
        )
        return

    random.shuffle(queue)
    await interaction.response.send_message(
        f"Shuffled **{len(queue)}** songs in the queue!"
    )


@client.tree.command(
    name="remove", description="Remove a song from the queue by position"
)
async def remove(interaction: discord.Interaction, position: int):
    if not interaction.guild:
        return
    queue = get_queue(interaction.guild.id)

    if not queue:
        await interaction.response.send_message("The queue is empty!", ephemeral=True)
        return

    if position < 1 or position > len(queue):
        await interaction.response.send_message(
            f"Invalid position! Please choose between 1 and {len(queue)}.",
            ephemeral=True,
        )
        return

    # Remove song (position is 1-indexed for users, 0-indexed for list)
    removed_song = queue.pop(position - 1)
    await interaction.response.send_message(
        f"Removed **[{removed_song.title}]({removed_song.url})** from position {position}."
    )


async def get_next_ready_player(guild_id: int) -> YTDLSource | None:
    """Pop players until one is ready to play or the queue runs empty."""
    queue = get_queue(guild_id)
    while queue:
        player = queue.pop(0)
        if not getattr(player, "is_lazy", False):
            return player

        try:
            return await player.get_actual_source()
        except Exception as exc:
            logger.error(f"Failed to load lazy player '{player.title}': {exc}")

    return None


async def retry_player_once(player: YTDLSource, guild_id: int):
    """Retry a failed track once by re-extracting its source URL."""
    if player._retries != 0:
        return

    try:
        player._retries = 1
        fresh_player = await YTDLSource.from_url(player.url)
        get_queue(guild_id).insert(0, fresh_player)
        logger.info(f"Retried failed song: {player.title}")
    except Exception as exc:
        logger.warning(f"Retry failed for {player.title}: {exc}")


def build_after_play_callback(player: YTDLSource, guild_id: int, text_channel_id: int):
    """Create the discord.py callback that advances playback after each track."""

    def _after_play(err):
        async def continue_playback():
            if err:
                await retry_player_once(player, guild_id)
            await play_next(guild_id, text_channel_id)

        future = asyncio.run_coroutine_threadsafe(continue_playback(), client.loop)
        try:
            future.result()
        except Exception as exc:
            logger.error(f"Error in after-play callback: {exc}", exc_info=True)

    return _after_play


async def announce_now_playing(guild_id: int, player: YTDLSource):
    """Send the now playing message once per track."""
    if player.message_sent:
        return

    message_sent = await send_guild_message(
        guild_id,
        f"Now playing: **[{player.title}]({player.url})**",
        "Failed to send now playing message",
    )
    if message_sent:
        player.message_sent = True
        logger.info(f"Now playing in guild {guild_id}: {player.title}")


async def wait_for_queue_during_playlist_load(guild_id: int, text_channel_id: int) -> bool:
    """Wait briefly for background playlist loading to add more songs."""
    for _ in range(5):
        await asyncio.sleep(1)
        if get_queue(guild_id):
            await play_next(guild_id, text_channel_id)
            return True
    return False


async def disconnect_for_empty_queue(
    guild: discord.Guild,
    *,
    guild_id: int,
    success_log: str,
    already_disconnected_log: str,
    warning_context: str,
):
    """Disconnect the bot when the queue stays empty."""
    await disconnect_guild_voice(
        guild,
        guild_id=guild_id,
        message="Queue is empty, disconnecting.",
        warning_context=warning_context,
        already_disconnected_log=already_disconnected_log,
        success_log=success_log,
    )


# ---- Play next songs ----
async def play_next(guild_id: int, text_channel_id: int):
    try:
        remember_text_channel(guild_id, text_channel_id)
        guild = client.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return

        player = await get_next_ready_player(guild_id)
        if player is not None:
            guild.voice_client.play(
                player, after=build_after_play_callback(player, guild_id, text_channel_id)
            )
            await announce_now_playing(guild_id, player)
            return

        if loading_playlists[guild_id]:
            queued_song_arrived = await wait_for_queue_during_playlist_load(
                guild_id, text_channel_id
            )
            if queued_song_arrived:
                return
            if guild.voice_client is None:
                return

            await disconnect_for_empty_queue(
                guild,
                guild_id=guild_id,
                success_log=f"Playlist loading timeout in guild {guild_id}. Disconnected.",
                already_disconnected_log=(
                    f"Bot already disconnected from guild {guild_id}, skipping timeout disconnect."
                ),
                warning_context="Failed to send timeout disconnect message",
            )
            return

        await disconnect_for_empty_queue(
            guild,
            guild_id=guild_id,
            success_log=f"Disconnected from voice channel in guild {guild_id}",
            already_disconnected_log=(
                f"Bot already disconnected from guild {guild_id}, skipping queue empty disconnect."
            ),
            warning_context="Failed to send disconnect message",
        )
    except Exception as exc:
        logger.error(
            f"Critical error in play_next for guild {guild_id}: {exc}", exc_info=True
        )

# ---- Bot start ----
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.error("Missing DISCORD_TOKEN in environment.")
    raise RuntimeError("Missing DISCORD_TOKEN in environment.")

logger.info("Starting Discord bot...")
client.run(token)
