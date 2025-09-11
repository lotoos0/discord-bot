"""
TODO:
    - Pominąć utwór niedostępny na YouTube (playlisty: try/except + continue).
    - Po przetworzeniu pierwszej nuty – gra od razu, reszta przetwarza się w tle.

TOFIX (po tym patchu powinno działać):
    - title przy dodawaniu playlisty
    - /queue nie działało przez interaction timeout / global queue

FIXED (w tym pliku):
    - max songs 20 (yt_dlp playlist_items)
    - mix
    - kolejka per-guild
    - poprawne followup / defer
    - after-callback z asyncio.run_coroutine_threadsafe
    - skracanie URL w executorze (nie blokuje event loop)
"""

import os
import asyncio
from collections import defaultdict

import discord
from discord import app_commands
import yt_dlp as youtube_dl
import requests
from dotenv import load_dotenv

# ---- yt-dlp / ffmpeg ----
ytdl_format_options = {
    "format": "bestaudio/best",
    "noplaylist": False,
    "playlist_items": "1-20",  # tylko pierwsze 20 pozycji z playlisty
    "quiet": True,
}
ffmpeg_options = {
    "options": "-vn",
    # (opcjonalnie lepsza odporność na rozłączenia)
    # "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)


# ---- Model źródła audio ----
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data):
        super().__init__(source)
        self.title = data.get("title", "Unknown Title")
        self.url = data.get("webpage_url", data.get("original_url", ""))

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False)
        )

        # Gdy to playlist/entries – bierzemy pierwszy realny entry
        if "entries" in data:
            # filtruj puste
            entries = [e for e in data["entries"] if e]
            if not entries:
                raise RuntimeError("Empty playlist or no accessible entries.")
            data = entries[0]

        filename = data.get("url")
        if not filename:
            raise RuntimeError("yt_dlp did not return a stream URL.")
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)


# ---- Asynchroniczny skracacz URL (nie blokuje loopa) ----
async def shorten_url_async(url: str) -> str:
    loop = asyncio.get_running_loop()

    def _do():
        try:
            r = requests.get(f"https://tinyurl.com/api-create.php?url={url}", timeout=5)
            if r.status_code == 200:
                return r.text
        except Exception as e:
            print(f"[SHORTEN_URL] {e}")
        return url

    return await loop.run_in_executor(None, _do)


# ---- Discord Client + Intents ----
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True  # jawnie, dla pewności


class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Global sync (pierwsze uruchomienie może potrwać kilkanaście sekund)
        await self.tree.sync(guild=None)


client = MyClient(intents=intents)

# ---- Kolejki per-guild ----
# guild_id -> list[YTDLSource]
queues: dict[int, list[YTDLSource]] = defaultdict(list)


def get_queue(guild_id: int) -> list:
    return queues[guild_id]


# ---- Eventy ----
@client.event
async def on_ready():
    print(f"Logged in as {client.user}. Slash commands synchronized.")


# ---- Komendy ----
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
    # zabezpiecza przed timeoutem 3s
    await interaction.response.defer(ephemeral=True)

    # dołącz do VC jeśli nie ma
    if interaction.guild.voice_client is None:
        if interaction.user.voice:
            await interaction.user.voice.channel.connect()
        else:
            await interaction.followup.send(
                "You must be in a voice channel!", ephemeral=True
            )
            return

    text_channel_id = interaction.channel.id
    guild_id = interaction.guild.id

    # pobierz metadane (executor)
    try:
        playlist_info = await client.loop.run_in_executor(
            None, lambda: ytdl.extract_info(url, download=False)
        )
    except Exception as e:
        await interaction.followup.send(f"Cannot process URL: {e}", ephemeral=True)
        return

    async def enqueue_one(entry: dict, announce: bool = True):
        try:
            # bierzemy webpage_url (oryginalny link do filmu na YT)
            entry_url = entry.get("webpage_url") or entry.get("url")
            player = await YTDLSource.from_url(entry_url, loop=client.loop)
            get_queue(guild_id).append(player)
            if announce:
                short = await shorten_url_async(player.url)
                await interaction.channel.send(
                    f"Added to queue: **[{player.title}]({short})**"
                )
        except Exception as e:
            await interaction.channel.send(f"Skipped one item (error): {e}")

    # playlist vs single
    if "entries" in playlist_info:
        entries = [e for e in playlist_info["entries"] if e]
        if not entries:
            await interaction.followup.send("Empty playlist.", ephemeral=True)
            return

        # pierwszy utwór – natychmiast
        first = entries[0]
        first_url = first.get("url") or first.get("webpage_url")
        if not first_url:
            await interaction.followup.send(
                "First playlist item has no URL.", ephemeral=True
            )
            return

        await enqueue_one(first, announce=False)

        # start odtwarzania jeśli nic nie gra
        if not interaction.guild.voice_client.is_playing():
            await play_next(guild_id, text_channel_id)

        # reszta playlisty w tle
        async def process_rest():
            for e in entries[1:]:
                await enqueue_one(e, announce=True)

        asyncio.create_task(process_rest())
        await interaction.followup.send("Playlist queued (max 20).", ephemeral=True)

    else:
        # pojedynczy link
        await enqueue_one(first, announce=True)
        if not interaction.guild.voice_client.is_playing():
            await play_next(guild_id, text_channel_id)
        await interaction.followup.send("Song queued.", ephemeral=True)


@client.tree.command(name="queue", description="Display the queue")
async def queue_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    q = get_queue(interaction.guild.id)
    if not q:
        await interaction.followup.send("The queue is empty!", ephemeral=True)
        return

    # skracamy linki asynchronicznie po kolei (20 pozycji max)
    lines = []
    for i, song in enumerate(q[:20], 1):
        short = await shorten_url_async(song.url)
        lines.append(f"{i}. [{song.title}]({short})")
    msg = "Queue:\n" + "\n".join(lines)
    await interaction.followup.send(msg, ephemeral=False)


@client.tree.command(name="skip", description="Skip the currently playing song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()  # wyzwoli after-callback i przejście do kolejnego
        await interaction.response.send_message("Skipped!")
    else:
        await interaction.response.send_message(
            "No music is currently playing!", ephemeral=True
        )


@client.tree.command(name="clearqueue", description="Clear the entire queue")
async def clearqueue(interaction: discord.Interaction):
    get_queue(interaction.guild.id).clear()
    await interaction.response.send_message("The queue has been cleared!")


# ---- Odtwarzanie kolejnych utworów (bez Interaction w środku) ----
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
                if err:
                    print(f"[AUDIO] after error: {err}")
                fut = asyncio.run_coroutine_threadsafe(
                    play_next(guild_id, text_channel_id), client.loop
                )
                try:
                    fut.result()
                except Exception as e:
                    print(f"[AUDIO] after future error: {e}")

            voice.play(player, after=_after_play)

            channel = client.get_channel(text_channel_id)
            if channel:
                short = await shorten_url_async(player.url)
                await channel.send(f"Now playing: **[{player.title}]({short})**")
        else:
            channel = client.get_channel(text_channel_id)
            if channel:
                await channel.send("Queue is empty, disconnecting.")
            if guild.voice_client:
                await guild.voice_client.disconnect()
    except Exception as e:
        print(f"Error in play_next: {e}")


# ---- Start bota ----
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("Missing DISCORD_TOKEN in environment.")
client.run(token)
