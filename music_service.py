"""Music playback orchestration for queues, voice state, and playlists."""

from __future__ import annotations

import asyncio
import logging

import discord

from music_audio import (
    YTDLSource,
    build_playlist_summary,
    create_player_from_entry,
    extract_info_async,
    get_playlist_entries,
    get_playlist_entry_url,
)
from music_state import MusicState

logger = logging.getLogger(__name__)


class MusicService:
    """Coordinate queue management, playback, and voice connections."""

    def __init__(self, client: discord.Client, state: MusicState):
        self.client = client
        self.state = state

    def get_guild_text_channel(self, guild_id: int) -> discord.TextChannel | None:
        """Return the remembered text channel for a guild, if still available."""
        channel_id = self.state.text_channels.get(guild_id)
        if channel_id is None:
            return None

        channel = self.client.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def send_channel_message(
        self, channel, message: str, warning_context: str
    ) -> bool:
        """Send a message and log a warning if Discord rejects it."""
        if channel is None:
            return False

        try:
            await channel.send(message)
            return True
        except Exception as exc:
            logger.warning("%s: %s", warning_context, exc)
            return False

    async def send_guild_message(
        self, guild_id: int, message: str, warning_context: str
    ) -> bool:
        """Send a message to the guild's remembered text channel."""
        return await self.send_channel_message(
            self.get_guild_text_channel(guild_id), message, warning_context
        )

    @staticmethod
    def get_bot_voice_channel(guild: discord.Guild):
        """Return the bot's active voice or stage channel for a guild."""
        voice_client = guild.voice_client
        if voice_client is None or voice_client.channel is None:
            return None

        channel = voice_client.channel
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return None

    @staticmethod
    def is_bot_alone_in_channel(channel) -> bool:
        """Return True when no human members remain in the bot's current channel."""
        return not any(not member.bot for member in channel.members)

    # pylint: disable=too-many-arguments
    async def disconnect_guild_voice(
        self,
        guild: discord.Guild,
        *,
        guild_id: int,
        message: str | None,
        warning_context: str,
        already_disconnected_log: str,
        success_log: str,
    ):
        """Disconnect from voice once, send an optional text message, and clean up."""
        async with self.state.disconnect_locks[guild_id]:
            if guild.voice_client is None:
                logger.info(already_disconnected_log)
                return

            if message:
                await self.send_guild_message(guild_id, message, warning_context)

            await guild.voice_client.disconnect(force=False)
            logger.info(success_log)

        self.state.cleanup_guild(guild_id)

    @staticmethod
    def get_requester_voice_channel(interaction: discord.Interaction):
        """Return the requesting user's current voice channel, if any."""
        if interaction.user.voice:
            return interaction.user.voice.channel
        return None

    async def send_interaction_message(
        self, interaction: discord.Interaction, message: str, *, ephemeral: bool = True
    ):
        """Send a response or followup message depending on interaction state."""
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
            return

        await interaction.response.send_message(message, ephemeral=ephemeral)

    async def ensure_bot_connected(
        self, interaction: discord.Interaction
    ) -> str | None:
        """Ensure the bot is connected to the requester's voice channel."""
        channel = self.get_requester_voice_channel(interaction)
        if channel is None:
            await self.send_interaction_message(
                interaction, "You must be in a voice channel!", ephemeral=True
            )
            return None

        voice_client = interaction.guild.voice_client
        if voice_client is None:
            try:
                await channel.connect()
                return "connected"
            except Exception as exc:
                await self.send_interaction_message(
                    interaction,
                    f"Failed to connect: {exc} (missing permissions or bot is banned?)",
                    ephemeral=True,
                )
                return None

        try:
            if voice_client.channel == channel:
                return "already_connected"

            await voice_client.move_to(channel)
            return "moved"
        except Exception as exc:
            await self.send_interaction_message(
                interaction,
                f"Failed to connect: {exc} (missing permissions or bot is banned?)",
                ephemeral=True,
            )
            return None

    # pylint: disable=too-many-arguments
    async def enqueue_entry(
        self,
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
            logger.error("Error enqueueing song: %s", exc, exc_info=True)
            await self.send_channel_message(
                channel,
                f"Skipped one item (error): {exc}",
                "Failed to send enqueue error message",
            )
            return False

        queue = self.state.get_queue(guild_id)
        if len(queue) >= self.state.max_queue_size:
            if announce:
                await self.send_channel_message(
                    channel,
                    f"Queue is full (max {self.state.max_queue_size} songs)!",
                    "Failed to send queue full message",
                )
            return False

        queue.append(player)
        if announce:
            await self.send_channel_message(
                channel,
                f"Added to queue: **[{player.title}]({player.url})**",
                "Failed to send queue addition message",
            )
        return True

    async def enqueue_playlist_entries(
        self, guild_id: int, entries: list[dict]
    ) -> tuple[int, int]:
        """Enqueue playlist entries one by one, skipping failures without aborting."""
        queued_count = 0
        skipped_count = 0

        for entry in entries:
            try:
                video_url = get_playlist_entry_url(entry)
                if not video_url:
                    logger.warning("Could not get URL for entry: %s", entry)
                    skipped_count += 1
                    continue

                player = await YTDLSource.from_url(video_url)
                queue = self.state.get_queue(guild_id)
                if len(queue) < self.state.max_queue_size:
                    queue.append(player)
                    queued_count += 1
            except Exception as exc:
                logger.warning(
                    "Skipped unavailable/errored video: %s - %s",
                    entry.get("id", "unknown"),
                    exc,
                )
                skipped_count += 1

        return queued_count, skipped_count

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """Clean up guild state when the bot leaves or gets left alone."""
        if member.id == self.client.user.id:
            if before.channel is not None and after.channel is None:
                guild_id = before.channel.guild.id
                logger.info(
                    "Bot left voice channel in guild %s. Cleaning up.", guild_id
                )
                self.state.cleanup_guild(guild_id)
            return

        guild = member.guild
        bot_channel = self.get_bot_voice_channel(guild)
        if bot_channel is None:
            return

        if not self.is_bot_alone_in_channel(bot_channel):
            return

        logger.info(
            "Bot is alone in voice channel in guild %s. Disconnecting after %ss "
            "delay.",
            guild.id,
            self.state.alone_disconnect_delay,
        )

        if self.state.alone_disconnect_delay > 0:
            await asyncio.sleep(self.state.alone_disconnect_delay)
            bot_channel = self.get_bot_voice_channel(guild)
            if bot_channel is None:
                return
            if not self.is_bot_alone_in_channel(bot_channel):
                logger.info(
                    "Someone rejoined voice channel in guild %s. Staying connected.",
                    guild.id,
                )
                return

        await self.disconnect_guild_voice(
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

    async def handle_music_request(self, interaction: discord.Interaction, url: str):
        """Handle the shared flow for /play and /add."""
        text_channel_id = interaction.channel.id
        guild_id = interaction.guild.id
        self.state.remember_text_channel(guild_id, text_channel_id)

        try:
            first_info = await extract_info_async(url, noplaylist=True)
        except Exception as exc:
            await interaction.followup.send(
                f"Cannot process URL: {exc}", ephemeral=True
            )
            return

        first_song_queued = await self.enqueue_entry(
            guild_id,
            interaction.channel,
            first_info,
            announce=False,
            use_entry_method=True,
        )
        if not first_song_queued:
            return

        if not interaction.guild.voice_client.is_playing():
            await self.play_next(guild_id, text_channel_id)

        self.state.loading_playlists[guild_id] = True

        async def fetch_and_enqueue_rest():
            try:
                playlist_info = await extract_info_async(
                    url, extract_flat="in_playlist"
                )
                entries = get_playlist_entries(playlist_info)
                if not entries:
                    logger.info(
                        "URL %s is not a playlist, skipping background queue.", url
                    )
                    return

                queued_count, skipped_count = await self.enqueue_playlist_entries(
                    guild_id, entries[1:]
                )
                if queued_count > 0:
                    await self.send_channel_message(
                        interaction.channel,
                        build_playlist_summary(queued_count, skipped_count),
                        "Failed to send playlist summary message",
                    )

                logger.info(
                    "Finished queueing %s additional songs in guild %s "
                    "(skipped %s).",
                    queued_count,
                    guild_id,
                    skipped_count,
                )
            except Exception as exc:
                logger.error(
                    "Error fetching full playlist in background: %s",
                    exc,
                    exc_info=True,
                )
            finally:
                self.state.finish_playlist_loading(guild_id)

        self.state.loading_tasks[guild_id] = asyncio.create_task(
            fetch_and_enqueue_rest()
        )

        logger.info(
            "First song queued immediately in guild %s, fetching rest in "
            "background...",
            guild_id,
        )
        await interaction.followup.send(
            "First song queued! Fetching rest of playlist in background...",
            ephemeral=True,
        )

    async def get_next_ready_player(self, guild_id: int) -> YTDLSource | None:
        """Pop players until one is ready to play or the queue runs empty."""
        queue = self.state.get_queue(guild_id)
        while queue:
            player = queue.pop(0)
            if not getattr(player, "is_lazy", False):
                return player

            try:
                return await player.get_actual_source()
            except Exception as exc:
                logger.error("Failed to load lazy player '%s': %s", player.title, exc)

        return None

    async def retry_player_once(  # pylint: disable=protected-access
        self, player: YTDLSource, guild_id: int
    ):
        """Retry a failed track once by re-extracting its source URL."""
        if player._retries != 0:
            return

        try:
            player._retries = 1
            fresh_player = await YTDLSource.from_url(player.url)
            self.state.get_queue(guild_id).insert(0, fresh_player)
            logger.info("Retried failed song: %s", player.title)
        except Exception as exc:
            logger.warning("Retry failed for %s: %s", player.title, exc)

    def build_after_play_callback(
        self, player: YTDLSource, guild_id: int, text_channel_id: int
    ):
        """Create the discord.py callback that advances playback after each track."""

        def _after_play(err):
            async def continue_playback():
                if err:
                    await self.retry_player_once(player, guild_id)
                await self.play_next(guild_id, text_channel_id)

            future = asyncio.run_coroutine_threadsafe(
                continue_playback(), self.client.loop
            )
            try:
                future.result()
            except Exception as exc:
                logger.error("Error in after-play callback: %s", exc, exc_info=True)

        return _after_play

    async def announce_now_playing(self, guild_id: int, player: YTDLSource):
        """Send the now playing message once per track."""
        if player.message_sent:
            return

        message_sent = await self.send_guild_message(
            guild_id,
            f"Now playing: **[{player.title}]({player.url})**",
            "Failed to send now playing message",
        )
        if message_sent:
            player.message_sent = True
            logger.info("Now playing in guild %s: %s", guild_id, player.title)

    async def wait_for_queue_during_playlist_load(
        self, guild_id: int, text_channel_id: int
    ) -> bool:
        """Wait briefly for background playlist loading to add more songs."""
        for _ in range(5):
            await asyncio.sleep(1)
            if self.state.get_queue(guild_id):
                await self.play_next(guild_id, text_channel_id)
                return True
        return False

    # pylint: disable=too-many-arguments
    async def disconnect_for_empty_queue(
        self,
        guild: discord.Guild,
        *,
        guild_id: int,
        success_log: str,
        already_disconnected_log: str,
        warning_context: str,
    ):
        """Disconnect the bot when the queue stays empty."""
        await self.disconnect_guild_voice(
            guild,
            guild_id=guild_id,
            message="Queue is empty, disconnecting.",
            warning_context=warning_context,
            already_disconnected_log=already_disconnected_log,
            success_log=success_log,
        )

    async def play_next(  # pylint: disable=too-many-return-statements
        self, guild_id: int, text_channel_id: int
    ):
        """Advance playback for the guild queue."""
        self.state.remember_text_channel(guild_id, text_channel_id)
        guild = self.client.get_guild(guild_id)
        if guild is None or guild.voice_client is None:
            return

        player = await self.get_next_ready_player(guild_id)
        if player is not None:
            try:
                guild.voice_client.play(
                    player,
                    after=self.build_after_play_callback(
                        player, guild_id, text_channel_id
                    ),
                )
            except Exception as exc:
                self.state.get_queue(guild_id).insert(0, player)
                logger.error(
                    "Failed to start playback in guild %s for '%s': %s",
                    guild_id,
                    player.title,
                    exc,
                    exc_info=True,
                )
                return

            try:
                await self.announce_now_playing(guild_id, player)
            except Exception as exc:
                logger.error(
                    "Playback started but now-playing announcement failed in guild "
                    "%s for '%s': %s",
                    guild_id,
                    player.title,
                    exc,
                    exc_info=True,
                )
            return

        if self.state.loading_playlists[guild_id]:
            try:
                queued_song_arrived = await self.wait_for_queue_during_playlist_load(
                    guild_id, text_channel_id
                )
            except Exception as exc:
                logger.error(
                    "Error while waiting for playlist loading in guild %s: %s",
                    guild_id,
                    exc,
                    exc_info=True,
                )
                return

            if queued_song_arrived:
                return
            if guild.voice_client is None:
                return

            try:
                await self.disconnect_for_empty_queue(
                    guild,
                    guild_id=guild_id,
                    success_log=(
                        f"Playlist loading timeout in guild {guild_id}. Disconnected."
                    ),
                    already_disconnected_log=(
                        "Bot already disconnected from guild "
                        f"{guild_id}, skipping timeout disconnect."
                    ),
                    warning_context="Failed to send timeout disconnect message",
                )
            except Exception as exc:
                logger.error(
                    "Failed to disconnect after playlist timeout in guild %s: %s",
                    guild_id,
                    exc,
                    exc_info=True,
                )
            return

        try:
            await self.disconnect_for_empty_queue(
                guild,
                guild_id=guild_id,
                success_log=f"Disconnected from voice channel in guild {guild_id}",
                already_disconnected_log=(
                    "Bot already disconnected from guild "
                    f"{guild_id}, skipping queue empty disconnect."
                ),
                warning_context="Failed to send disconnect message",
            )
        except Exception as exc:
            logger.error(
                "Failed to disconnect for empty queue in guild %s: %s",
                guild_id,
                exc,
                exc_info=True,
            )
