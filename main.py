"""Discord bot entrypoint with slash commands and startup wiring."""

import logging
import os
import random

import discord
from discord import app_commands
from dotenv import load_dotenv

from music_audio import build_queue_page_message
from music_service import MusicService
from music_state import MusicState

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class MyClient(discord.Client):
    """Discord client that owns the slash-command tree."""

    def __init__(self, *args, **kwargs):
        """Initialize the Discord client and command tree."""
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        """Synchronize slash commands when the client starts."""
        await self.tree.sync(guild=None)


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

client = MyClient(intents=intents)
state = MusicState()
music_service = MusicService(client, state)


@client.event
async def on_ready():
    """Log successful startup and slash-command synchronization."""
    logger.info("Logged in as %s. Slash commands synchronized.", client.user)


@client.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
):
    """Delegate voice-state handling to the music service."""
    await music_service.on_voice_state_update(member, before, after)


@client.tree.command(name="join", description="Join the voice channel")
async def join(interaction: discord.Interaction):
    """Join or move to the requester's voice channel."""
    connection_result = await music_service.ensure_bot_connected(interaction)
    if not connection_result:
        return

    channel = music_service.get_requester_voice_channel(interaction)
    if connection_result == "already_connected":
        await interaction.response.send_message(
            "Bot is already in your voice channel!", ephemeral=True
        )
        return

    if connection_result == "moved":
        await interaction.response.send_message(f"Moved to the channel {channel}!")
        return

    await interaction.response.send_message(f"Joined the channel {channel}!")


@client.tree.command(name="leave", description="Leave the voice channel")
async def leave(interaction: discord.Interaction):
    """Disconnect the bot from voice if it is currently connected."""
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        await interaction.response.send_message("Bot has left the voice channel!")
        return

    await interaction.response.send_message(
        "I am not in a voice channel!", ephemeral=True
    )


@client.tree.command(
    name="play", description="Join voice channel and play music (URL or playlist)"
)
async def play(interaction: discord.Interaction, url: str):
    """Connect to voice if needed and start playback for a URL or playlist."""
    await interaction.response.defer(ephemeral=True)
    if not await music_service.ensure_bot_connected(interaction):
        return

    await music_service.handle_music_request(interaction, url)


@client.tree.command(
    name="add", description="Add music to queue (bot must already be playing)"
)
async def add(interaction: discord.Interaction, url: str):
    """Add a URL or playlist to the queue without reconnecting the bot."""
    await interaction.response.defer(ephemeral=True)
    if interaction.guild.voice_client is None:
        await interaction.followup.send(
            "Bot is not in a voice channel! Use `/play` to start playing first.",
            ephemeral=True,
        )
        return

    await music_service.handle_music_request(interaction, url)


@client.tree.command(name="queue", description="Display the queue")
@discord.app_commands.describe(page="Page number (20 songs per page)")
async def queue_list(interaction: discord.Interaction, page: int = 1):
    """Show one page of the current guild queue."""
    await interaction.response.defer(ephemeral=True)
    queue = state.get_queue(interaction.guild.id)
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
    """Stop the current track so playback advances to the next item."""
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await interaction.response.send_message("Skipped!")
        return

    await interaction.response.send_message(
        "No music is currently playing!", ephemeral=True
    )


@client.tree.command(name="clearqueue", description="Clear the entire queue")
async def clearqueue(interaction: discord.Interaction):
    """Clear the queue and stop any background playlist loading."""
    guild_id = interaction.guild.id
    state.get_queue(guild_id).clear()
    state.stop_playlist_loading(guild_id)
    await interaction.response.send_message("The queue has been cleared!")


@client.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle(interaction: discord.Interaction):
    """Shuffle the current guild queue in place."""
    if not interaction.guild:
        return

    queue = state.get_queue(interaction.guild.id)
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
    """Remove one queued song by its 1-based position."""
    if not interaction.guild:
        return

    queue = state.get_queue(interaction.guild.id)
    if not queue:
        await interaction.response.send_message("The queue is empty!", ephemeral=True)
        return

    if position < 1 or position > len(queue):
        await interaction.response.send_message(
            f"Invalid position! Please choose between 1 and {len(queue)}.",
            ephemeral=True,
        )
        return

    removed_song = queue.pop(position - 1)
    await interaction.response.send_message(
        f"Removed **[{removed_song.title}]({removed_song.url})** from position {position}."
    )


load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.error("Missing DISCORD_TOKEN in environment.")
    raise RuntimeError("Missing DISCORD_TOKEN in environment.")

logger.info("Starting Discord bot...")
client.run(token)
