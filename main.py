"""
TODO:
    - Skip songs that are unavailable on YouTube (playlist handling with try/except + continue).
    - After processing the first song – play immediately, the rest should be processed in the background.

TOFIX (should be resolved in this patch):
    - Title handling when adding playlists
    - /queue sometimes failed due to interaction timeout / global queue

FIXED (in this file):
    - Max songs 20 (yt_dlp playlist_items)
    - Shuffle/mix
    - Per-guild queues
    - Proper followup/defer handling
    - after-callback with asyncio.run_coroutine_threadsafe
    - Async URL shortener (does not block event loop)
"""

import os
import asyncio
import logging
from collections import defaultdict

import discord
from discord import app_commands
import yt_dlp as youtube_dl
import requests
from dotenv import load_dotenv

# ---- Logging configuration ----
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
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
    def __init__(self, source, *, data):
        super().__init__(source)
        self.title = data.get("title", "Unknown Title")
        self.url = data.get("webpage_url", data.get("original_url", ""))
        self._retries = 0

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

    @classmethod
    async def from_entry(cls, entry: dict):
        """Create YTDLSource from already-extracted playlist entry (no re-fetching)."""
        loop = asyncio.get_running_loop()
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


# ---- Async URL shortener (non-blocking) with cache & rate limiting ----
url_shortener_cache: dict[str, str] = {}
last_shorten_time: float = 0.0
SHORTEN_RATE_LIMIT = 0.1  # Min 100ms between API calls

async def shorten_url_async(url: str) -> str:
    """Shorten URL with caching and rate limiting to avoid API spam."""
    global last_shorten_time
    
    if url in url_shortener_cache:
        return url_shortener_cache[url]
    
    loop = asyncio.get_running_loop()
    current_time = loop.time()
    time_since_last = current_time - last_shorten_time
    
    # Rate limit: wait if needed
    if time_since_last < SHORTEN_RATE_LIMIT:
        await asyncio.sleep(SHORTEN_RATE_LIMIT - time_since_last)

    def _do():
        try:
            r = requests.get(f"https://tinyurl.com/api-create.php?url={url}", timeout=5)
            if r.status_code == 200:
                return r.text.strip()
        except Exception as e:
            logger.warning(f"URL shortening failed for {url}: {e}")
        return url

    result = await loop.run_in_executor(None, _do)
    last_shorten_time = asyncio.get_running_loop().time()
    url_shortener_cache[url] = result
    return result


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
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
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


@client.tree.command(
    name="play", description="Play music from YouTube (URL or playlist)"
)
async def play(interaction: discord.Interaction, url: str):
    # Prevent interaction timeout (3s limit)
    await interaction.response.defer(ephemeral=True)

    # Connect to VC if not already connected
    if interaction.guild.voice_client is None:
        if interaction.user.voice:
            try:
                await interaction.user.voice.channel.connect()
            except Exception as e:
                await interaction.followup.send(
                    f"Failed to connect: {e} (missing permissions or bot is banned?)", ephemeral=True
                )
                return
        else:
            await interaction.followup.send(
                "You must be in a voice channel!", ephemeral=True
            )
            return

    text_channel_id = interaction.channel.id
    guild_id = interaction.guild.id

    # Download metadata (via executor)
    try:
        loop = asyncio.get_running_loop()
        playlist_info = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False)
        )
    except Exception as e:
        await interaction.followup.send(f"Cannot process URL: {e}", ephemeral=True)
        return

    async def enqueue_one(entry: dict, announce: bool = True, use_entry_method: bool = False):
        try:
            if use_entry_method:
                # For already-extracted playlist entries (faster)
                player = await YTDLSource.from_entry(entry)
            else:
                # For direct URLs
                entry_url = entry.get("webpage_url") or entry.get("url")
                player = await YTDLSource.from_url(entry_url)
            
            # Check queue size limit
            q = get_queue(guild_id)
            if len(q) >= MAX_QUEUE_SIZE:
                if announce and interaction.channel:
                    await interaction.channel.send(f"Queue is full (max {MAX_QUEUE_SIZE} songs)!")
                return
            
            q.append(player)
            if announce and interaction.channel:
                short = await shorten_url_async(player.url)
                await interaction.channel.send(
                    f"Added to queue: **[{player.title}]({short})**"
                )
        except Exception as e:
            logger.error(f"Error enqueueing song: {e}", exc_info=True)
            if interaction.channel:
                await interaction.channel.send(f"Skipped one item (error): {e}")

    # Playlist vs single
    if "entries" in playlist_info:
        entries = [e for e in playlist_info["entries"] if e]
        if not entries:
            await interaction.followup.send("Empty playlist.", ephemeral=True)
            return

        # Mark that we're loading playlists for this guild
        loading_playlists[guild_id] = True

        # First song - process immediately and play
        first = entries[0]
        await enqueue_one(first, announce=False, use_entry_method=True)

        # Start playing if nothing is currently playing
        if not interaction.guild.voice_client.is_playing():
            await play_next(guild_id, text_channel_id)

        # Process the rest of the playlist in the background
        async def process_rest():
            try:
                for e in entries[1:]:
                    await enqueue_one(e, announce=True, use_entry_method=True)
            finally:
                # Mark that playlist loading is done
                loading_playlists[guild_id] = False
                loading_tasks.pop(guild_id, None)

        task = asyncio.create_task(process_rest())
        loading_tasks[guild_id] = task
        logger.info(f"Playlist queued in guild {guild_id}. First song added, processing {len(entries)-1} more in background.")
        await interaction.followup.send("Playlist queued (max 20).", ephemeral=True)

    else:
        # Single link
        await enqueue_one(playlist_info, announce=True)
        if not interaction.guild.voice_client.is_playing():
            await play_next(guild_id, text_channel_id)
        logger.info(f"Single song queued in guild {guild_id}: {playlist_info.get('title', 'Unknown')}")
        await interaction.followup.send("Song queued.", ephemeral=True)


@client.tree.command(name="queue", description="Display the queue")
async def queue_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    q = get_queue(interaction.guild.id)
    if not q:
        await interaction.followup.send("The queue is empty!", ephemeral=True)
        return

    # Shorten links in parallel (20 items max) with timeout
    songs_to_display = q[:20]
    try:
        shorts = await asyncio.wait_for(
            asyncio.gather(*[shorten_url_async(song.url) for song in songs_to_display]),
            timeout=10.0
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("Queue display timed out, showing without links.", ephemeral=False)
        shorts = [song.url for song in songs_to_display]
    
    lines = [f"{i}. [{song.title}]({short})" for i, (song, short) in enumerate(zip(songs_to_display, shorts), 1)]
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
            if channel:
                try:
                    short = await shorten_url_async(player.url)
                    await channel.send(f"Now playing: **[{player.title}]({short})**")
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
                        logger.warning(f"Failed to send timeout disconnect message: {e}")
                cleanup_guild(guild_id)
                if guild.voice_client:
                    await guild.voice_client.disconnect()
                    logger.info(f"Playlist loading timeout in guild {guild_id}. Disconnected.")
    except Exception as e:
        logger.error(f"Critical error in play_next for guild {guild_id}: {e}", exc_info=True)


# ---- Bot start ----
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.error("Missing DISCORD_TOKEN in environment.")
    raise RuntimeError("Missing DISCORD_TOKEN in environment.")

logger.info("Starting Discord bot...")
client.run(token)
