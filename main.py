"""
ZROBIONE:
    - Title handling in playlists (verified - works correctly)
    - Removed TinyURL shortener (not needed, Discord handles long links well)
    - Skip songs that are unavailable on YouTube (playlist handling with try/except + continue)
    - After processing the first song – play immediately, the rest should be processed in the background
    - Max songs 20 (yt_dlp playlist_items)
    - Shuffle/mix
    - Per-guild queues
    - Proper followup/defer handling
    - after-callback with asyncio.run_coroutine_threadsafe
"""

import asyncio
import logging
import os
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
ytdl_format_options = {
    # Fallback chain: m4a → best available audio → best overall
    "format": "bestaudio[ext=m4a]/bestaudio[acodec!=none]/bestaudio/best",
    "noplaylist": False,
    "playlist_items": "1-20",
    "quiet": False,
    "no_warnings": False,
    # Suppress verbose debug logs from yt-dlp (but keep errors)
    "verbose": False,
    # Force IPv4 (helps when running inside Docker)
    "source_address": "0.0.0.0",
    "socket_timeout": 15,
    "retries": 5,
    "fragment_retries": 5,
    # JavaScript solver for signature challenges (required for SABR videos)
    "extractor_args": {
        "youtube": {
            "js_player": "https://www.youtube.com/s/player/latest/player.js",
        }
    },
}
cookies = "/app/cookies.txt"
if os.path.exists(cookies):
    ytdl_format_options["cookiefile"] = cookies

ffmpeg_options = {
    # Auto-reconnect and disable stdin blocking
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
    "options": "-vn",
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)


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
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(url, download=False)
            )
        except Exception as e:
            # Re-raise with context for better error messages
            raise RuntimeError(f"Failed to extract info from {url}: {e}")

        # Handle playlist case: take the first real entry
        if "entries" in data:
            entries = [e for e in data["entries"] if e]
            if not entries:
                raise RuntimeError("Empty playlist or no accessible entries.")
            data = entries[0]

        filename = data.get("url")
        if not filename:
            title = data.get("title", "Unknown")
            raise RuntimeError(
                f"No stream URL for '{title}' (video may be age-restricted, "
                "private, or SABR-protected). Try a different video."
            )
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

    async def get_actual_source(self):
        """If this is a lazy player, fetch the actual source now."""
        if not self.is_lazy:
            return self

        try:
            loop = asyncio.get_running_loop()
            entry_url = self.lazy_entry.get("webpage_url") or self.lazy_entry.get(
                "original_url"
            )
            if not entry_url:
                raise RuntimeError("No URL found in lazy entry")
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(entry_url, download=False)
            )
            filename = data.get("url")
            if not filename:
                raise RuntimeError("Failed to get stream URL")

            # Create actual FFmpeg source with the stream URL
            actual_source = discord.FFmpegPCMAudio(filename, **ffmpeg_options)
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
        loop = asyncio.get_running_loop()

        if lazy:
            # Create a lazy player - store entry, don't create source yet
            # Use a placeholder entry to avoid creating FFmpeg source
            return cls(None, data=entry, lazy_entry=entry)

        # Try to use the stream URL from entry, or fetch it if missing
        filename = entry.get("url")
        if filename:
            return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=entry)

        # If no URL, we need to extract it
        try:
            entry_url = entry.get("webpage_url") or entry.get("original_url")
            if not entry_url:
                raise RuntimeError("No URL found in entry")
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(entry_url, download=False)
            )
            filename = data.get("url")
            if not filename:
                raise RuntimeError("Failed to get stream URL")
            return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)
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
# Maximum songs in queue per guild
MAX_QUEUE_SIZE = 100


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


# ---- Events ----
@client.event
async def on_ready():
    logger.info(f"Logged in as {client.user}. Slash commands synchronized.")


@client.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    """Clean up guild state when bot leaves a voice channel."""
    if member.id != client.user.id:
        return

    # Bot left the channel
    if before.channel is not None and after.channel is None:
        guild_id = before.channel.guild.id
        logger.info(f"Bot left voice channel in guild {guild_id}. Cleaning up.")
        cleanup_guild(guild_id)


# ---- Slash commands ----
@client.tree.command(name="join", description="Join the voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "You must be in a voice channel!", ephemeral=True
        )
        return
    channel = interaction.user.voice.channel
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

    async def enqueue_one(
        entry: dict,
        announce: bool = True,
        use_entry_method: bool = False,
        lazy: bool = False,
    ):
        try:
            if use_entry_method:
                # For already-extracted playlist entries
                player = await YTDLSource.from_entry(entry, lazy=lazy)
            else:
                # For direct URLs
                entry_url = entry.get("webpage_url") or entry.get("url")
                player = await YTDLSource.from_url(entry_url)

            # Check queue size limit
            q = get_queue(guild_id)
            if len(q) >= MAX_QUEUE_SIZE:
                if announce and interaction.channel:
                    await interaction.channel.send(
                        f"Queue is full (max {MAX_QUEUE_SIZE} songs)!"
                    )
                return

            q.append(player)
            if announce and interaction.channel:
                await interaction.channel.send(
                    f"Added to queue: **[{player.title}]({player.url})**"
                )
        except Exception as e:
            logger.error(f"Error enqueueing song: {e}", exc_info=True)
            if interaction.channel:
                await interaction.channel.send(f"Skipped one item (error): {e}")

    # First, try to get just the first song WITHOUT waiting for full playlist
    loop = asyncio.get_running_loop()
    try:
        # Create a special ytdl instance that only gets first item (noplaylist=True)
        ytdl_single = youtube_dl.YoutubeDL({**ytdl_format_options, "noplaylist": True})
        first_info = await loop.run_in_executor(
            None, lambda: ytdl_single.extract_info(url, download=False)
        )
    except Exception as e:
        await interaction.followup.send(f"Cannot process URL: {e}", ephemeral=True)
        return

    # Enqueue and play first song immediately
    await enqueue_one(first_info, announce=False, use_entry_method=True)
    if not interaction.guild.voice_client.is_playing():
        await play_next(guild_id, text_channel_id)

    # Now fetch the full playlist in background (without blocking)
    loading_playlists[guild_id] = True

    async def fetch_and_enqueue_rest():
        try:
            # Fetch playlist structure using extract_flat to get list of video IDs/URLs
            # without downloading each one individually (avoids errors breaking entire playlist)
            ytdl_flat = youtube_dl.YoutubeDL(
                {**ytdl_format_options, "extract_flat": "in_playlist"}
            )
            playlist_info = await loop.run_in_executor(
                None, lambda: ytdl_flat.extract_info(url, download=False)
            )

            if "entries" not in playlist_info:
                logger.info(f"URL {url} is not a playlist, skipping background queue.")
                loading_playlists[guild_id] = False
                loading_tasks.pop(guild_id, None)
                return

            entries = [e for e in playlist_info["entries"] if e]
            if not entries:
                logger.info(f"Playlist has no entries.")
                loading_playlists[guild_id] = False
                loading_tasks.pop(guild_id, None)
                return

            # Skip first entry (already queued) and enqueue the rest (silently)
            queued_count = 0
            skipped_count = 0
            for e in entries[1:]:
                try:
                    # For extract_flat entries, we might just have id/url, not full info
                    # Use from_url method instead to properly load each song
                    video_url = e.get("url") or e.get("webpage_url")
                    if not video_url:
                        # If extract_flat only gave us an ID, construct the YouTube URL
                        video_id = e.get("id")
                        if video_id:
                            video_url = f"https://www.youtube.com/watch?v={video_id}"

                    if video_url:
                        # Fetch full info for this single video (not as lazy, to ensure proper loading)
                        player = await YTDLSource.from_url(video_url)
                        q = get_queue(guild_id)
                        if len(q) < MAX_QUEUE_SIZE:
                            q.append(player)
                            queued_count += 1
                    else:
                        logger.warning(f"Could not get URL for entry: {e}")
                        skipped_count += 1
                except Exception as skip_error:
                    # Log unavailable/errored videos but continue with the rest
                    logger.warning(
                        f"Skipped unavailable/errored video: {e.get('id', 'unknown')} - {skip_error}"
                    )
                    skipped_count += 1
                    continue

            # Send summary message once at the end
            if queued_count > 0 and interaction.channel:
                summary = (
                    f"✅ Added **{queued_count}** more songs to queue from playlist."
                )
                if skipped_count > 0:
                    summary += f" (Skipped {skipped_count} unavailable videos)"
                await interaction.channel.send(summary)

            logger.info(
                f"Finished queueing {queued_count} additional songs in guild {guild_id} (skipped {skipped_count})."
            )
        except Exception as e:
            logger.error(
                f"Error fetching full playlist in background: {e}", exc_info=True
            )
        finally:
            loading_playlists[guild_id] = False
            loading_tasks.pop(guild_id, None)

    task = asyncio.create_task(fetch_and_enqueue_rest())
    loading_tasks[guild_id] = task

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

    # Connect to VC if not already connected
    if interaction.guild.voice_client is None:
        if interaction.user.voice:
            try:
                await interaction.user.voice.channel.connect()
            except Exception as e:
                await interaction.followup.send(
                    f"Failed to connect: {e} (missing permissions or bot is banned?)",
                    ephemeral=True,
                )
                return
        else:
            await interaction.followup.send(
                "You must be in a voice channel!", ephemeral=True
            )
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
async def queue_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    q = get_queue(interaction.guild.id)
    if not q:
        await interaction.followup.send("The queue is empty!", ephemeral=True)
        return

    # Show first 20 songs
    songs_to_display = q[:20]
    lines = [
        f"{i}. [{song.title}]({song.url})" for i, song in enumerate(songs_to_display, 1)
    ]
    msg = "Queue:\n" + "\n".join(lines)
    await interaction.followup.send(msg, ephemeral=False)


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


# ---- Play next songs ----
async def play_next(guild_id: int, text_channel_id: int):
    try:
        guild = client.get_guild(guild_id)
        if not guild:
            return
        voice = guild.voice_client
        if not voice:
            return

        q = get_queue(guild_id)
        if q:
            player = q.pop(0)

            # If it's a lazy player, fetch the actual source now
            if hasattr(player, "is_lazy") and player.is_lazy:
                try:
                    player = await player.get_actual_source()
                except Exception as e:
                    logger.error(f"Failed to load lazy player '{player.title}': {e}")
                    await play_next(guild_id, text_channel_id)
                    return

            def _after_play(err):
                async def _cont():
                    # If error - try refreshing the same track once
                    if err and player._retries == 0:
                        try:
                            player._retries = 1
                            fresh = await YTDLSource.from_url(player.url)
                            get_queue(guild_id).insert(0, fresh)
                            logger.info(f"Retried failed song: {player.title}")
                        except Exception as e:
                            logger.warning(f"Retry failed for {player.title}: {e}")
                    await play_next(guild_id, text_channel_id)

                fut = asyncio.run_coroutine_threadsafe(_cont(), client.loop)
                try:
                    fut.result()
                except Exception as e:
                    logger.error(f"Error in after-play callback: {e}", exc_info=True)

            voice.play(player, after=_after_play)

            channel = client.get_channel(text_channel_id)
            # Send "Now playing" message only once per song
            if channel and not player.message_sent:
                try:
                    await channel.send(
                        f"Now playing: **[{player.title}]({player.url})**"
                    )
                    player.message_sent = True
                    logger.info(f"Now playing in guild {guild_id}: {player.title}")
                except Exception as e:
                    logger.error(f"Failed to send now playing message: {e}")
        else:
            channel = client.get_channel(text_channel_id)
            # Don't disconnect if playlists are still being loaded
            if not loading_playlists[guild_id]:
                if channel:
                    try:
                        await channel.send("Queue is empty, disconnecting.")
                    except Exception as e:
                        logger.warning(f"Failed to send disconnect message: {e}")
                cleanup_guild(guild_id)
                if guild.voice_client:
                    await guild.voice_client.disconnect()
                    logger.info(f"Disconnected from voice channel in guild {guild_id}")
            else:
                # Playlists loading - wait up to 5 seconds for new songs
                for _ in range(5):
                    await asyncio.sleep(1)
                    q = get_queue(guild_id)
                    if q and guild.voice_client:
                        # Songs appeared in queue - play next one
                        await play_next(guild_id, text_channel_id)
                        return
                # Timeout - still no songs, disconnect
                if channel:
                    try:
                        await channel.send("Queue is empty, disconnecting.")
                    except Exception as e:
                        logger.warning(
                            f"Failed to send timeout disconnect message: {e}"
                        )
                cleanup_guild(guild_id)
                if guild.voice_client:
                    await guild.voice_client.disconnect()
                    logger.info(
                        f"Playlist loading timeout in guild {guild_id}. Disconnected."
                    )
    except Exception as e:
        logger.error(
            f"Critical error in play_next for guild {guild_id}: {e}", exc_info=True
        )


# ---- Bot start ----
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.error("Missing DISCORD_TOKEN in environment.")
    raise RuntimeError("Missing DISCORD_TOKEN in environment.")

logger.info("Starting Discord bot...")
client.run(token)
