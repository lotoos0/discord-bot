"""
TODO:
    - Pominac nute gdy jest niedostepna na youtube a jest w playliscie
    - Po przetworzeniu jednej nutki odrazu ja puscilo play a reszta w tle sie przetwarzala. 
TOFIX: 
    - title jak dodajemy playliste 
    - /queue nie dziala

FIXED: 
    - max songs 20
    - mix
"""


import discord
from discord import app_commands
import yt_dlp as youtube_dl
import asyncio
import requests

# Settings for youtube_dl with limit on playlist items
ytdl_format_options = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'playlist_items': '1-20',  # Pobierz tylko pierwsze 20 utworów
}
ffmpeg_options = {
        'options': '-vn',
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# Class to support music
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data):
        super().__init__(source)
        self.title = data.get('title', 'Unknown Title')
        self.url = data.get('webpage_url', data.get('original_url', ''))

    @classmethod
    async def from_url(cls, url, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))

        if 'entries' in data:
            data = data['entries'][0]

        filename = data.get('url', None)
        return cls(discord.FFmpegPCMAudio(filename, **ffmpeg_options), data=data)

def shorten_url(url):
    api_url = f"http://tinyurl.com/api-create.php?url={url}"
    try:
        response = requests.get(api_url)
        if response.status_code == 200:
            return response.text
    except Exception as e:
        print(f"\n [SHORTEN_URL] Error Shortening URL: {e}")
    return url

# BOT settings
intents = discord.Intents.default()
intents.message_content = True

# Using discord.Client instead of commands.Bot
class MyClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync(guild=None)

client = MyClient(intents=intents)
queue = []

# Synchronize slash commands
@client.event
async def on_ready():
    print(f"Logged in as {client.user}, slash commands have been synchronized!")

# Slash command to join the voice channel
@client.tree.command(name="join", description="Join the voice channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("You must be in a voice channel!", ephemeral=True)
        return
   
    channel = interaction.user.voice.channel
    await channel.connect()
    await interaction.response.send_message(f"Joined the channel {channel}!")

# Command to leave the voice channel
@client.tree.command(name="leave", description="Leave the voice channel")
async def leave(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        await interaction.response.send_message("Bot has left the voice channel!")
    else:
        await interaction.response.send_message("I am not in a voice channel!", ephemeral=True)

# Command to play music
@client.tree.command(name="play", description="Play music from YouTube")
async def play(interaction: discord.Interaction, url: str):
    if interaction.guild.voice_client is None:
        if interaction.user.voice:
            await interaction.user.voice.channel.connect()
        else:
            await interaction.response.send_message("You must be in a voice channel to play music!", ephemeral=True)
            return

    # Download information about video or playlist
    await interaction.response.send_message("Processing the link...", ephemeral=True)
    playlist_info = await client.loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
    
    if 'entries' in playlist_info:
        # Play the first song immediately
        first_entry = playlist_info['entries'][0]
        player = await YTDLSource.from_url(first_entry['url'], loop=client.loop)
        queue.append(player)
        
        # Add clickable song title with link for the first song
        song_link = f"[{player.title}]({first_entry['webpage_url']})"
        await interaction.channel.send(f"Now playing: **{song_link}**")

        if not interaction.guild.voice_client.is_playing():
            await play_next(interaction)

        # Process the rest of the playlist in the background
        asyncio.create_task(process_remaining_playlist(playlist_info['entries'][1:], interaction))

    else:
        # Play single song
        async with interaction.channel.typing():
            player = await YTDLSource.from_url(url, loop=client.loop)
            queue.append(player)
            song_link = f"[{player.title}]({shorten_url(url)})"
            await interaction.channel.send(f"Song **{song_link}** has been added to the queue.")
   
    if not interaction.guild.voice_client.is_playing():
        await play_next(interaction)

async def process_remaining_playlist(entries, interaction):
    """Process the remaining songs in the playlist in the background."""
    for entry in entries:
        try:
            player = await YTDLSource.from_url(entry['url'], loop=client.loop)
            queue.append(player)
            # Add clickable song title with link
            song_link = f"[{player.title}]({entry['webpage_url']})"
            await interaction.channel.send(f"Song **{song_link}** has been added to the queue.")
        except Exception as e:
            await interaction.channel.send(f"Error occurred while processing playlist: {e}")

async def play_next(interaction: discord.Interaction):
    try:
        if len(queue) > 0:
            player = queue.pop(0)
            interaction.guild.voice_client.play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(interaction), client.loop))
            song_link = f"[{player.title}]({shorten_url(player.url)})"
            await interaction.channel.send(f"Now playing: **{song_link}**")
        else:
            await interaction.channel.send("The queue is empty, disconnecting.")
            await interaction.guild.voice_client.disconnect()
    except Exception as e:
        pritn(f"Error in play_next: {e}")
# Command to see the queue
@client.tree.command(name="queue", description="Display the queue")
async def queue_list(interaction: discord.Interaction):
    if len(queue) == 0:
        await interaction.response.send_message("The queue is empty!", ephemeral=True)
    else:
        queue_titles = "\n".join([f"[{song.title}]({shorten_url(song.url)})" for song in queue])
        await interaction.response.send_message(f"Queue: \n{queue_titles}")

# Command to skip the current song
@client.tree.command(name="skip", description="Skip the currently playing song")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client is not None and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.stop()  # Stop the current song
        await interaction.response.send_message("The song has been skipped!")
    else:
        await interaction.response.send_message("No music is currently playing!")

# Command to clear the queue
@client.tree.command(name="clearqueue", description="Clear the entire queue")
async def clearqueue(interaction: discord.Interaction):
    queue.clear()
    await interaction.response.send_message("The queue has been cleared!")

# Run bot
from dotenv import load_dotenv
import os
load_dotenv()

token = os.getenv('DISCORD_TOKEN')
client.run(token)




