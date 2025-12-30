# Changelog

All notable changes to this project will be documented in this file.

## [2025-12-30 Update 2] - Command UX Improvements

### Added
- **`/add` Command** - New command for adding songs to existing queue (bot must already be in VC)
  - Clearer UX: `/play` for starting the bot, `/add` when it's already playing
  - Prevents confusion about what `/play` does when bot is already active

### Changed
- **`/play` Description** - Updated to clarify it joins voice channel and starts playing
- **Code Refactoring** - Extracted shared logic into `_handle_music_request()` helper function to avoid duplication

### Removed
- **TinyURL Shortener** - Removed URL shortening feature that was causing queue timeouts
  - Discord handles long URLs well in markdown format `[title](url)`
  - `/queue` command now responds instantly (no API calls)
  - Removed `requests` library dependency
  - Removed `shorten_url_async()`, cache, and rate limiting code

### Fixed
- **`/queue` Timeout Issue** - Command is now instant instead of waiting up to 10s for TinyURL API

---

## [2025-12-30] - Playlist Loading Fixes

### Fixed
- **Playlist Queue Bug** - Bot now correctly queues all remaining songs from playlists instead of disconnecting with "Queue is empty"
  - Changed from `extract_info()` (which fails on any unavailable video) to `extract_flat` with individual video processing
  - Each video is now fetched separately with proper error handling for unavailable videos
  - Unavailable/errored videos are logged and skipped without affecting the rest of the playlist
- **Incomplete Playlist Data** - Fixed issue where `extract_flat` entries had incomplete data for playback
  - Now properly constructs YouTube URLs from video IDs when needed
  - Uses `from_url()` for each video to ensure full data is loaded

### Added
- **Skip Feedback** - Shows count of skipped unavailable videos in the playlist summary message
- **Better Logging** - Logs skipped videos with specific reasons for debugging

### Performance
- Improved reliability: Playlists no longer fail completely if one video is unavailable

---

## [2025-12-27 Update 3] - Logging & Auto-Cleanup

### Added
- **Logging Module** - Replaced all `print()` with proper `logging` module for better debugging
- **Auto Voice Cleanup** - Added `on_voice_state_update` event to cleanup guild state when bot leaves voice channel
- **Detailed Logging** - Added logs for:
  - Playlist queuing progress
  - Song playback start
  - Retry attempts
  - Disconnections (normal and timeout)
  - Errors with full stack traces

### Fixed
- **Critical: asyncio.get_event_loop() in play()** - Changed from deprecated `client.loop` to `asyncio.get_running_loop()`
- **Logging for all operations** - Better visibility into bot behavior

### Improved
- **Error Traceability** - Full exception info logged for debugging
- **Guild State Management** - Automatic cleanup on voice state changes prevents memory leaks

---

## [2025-12-27 Update 2] - Bug Fixes & Stability

### Fixed
- **asyncio.get_event_loop() Deprecation** - Replaced with `asyncio.get_running_loop()` to avoid Python 3.10+ warnings
- **Null Channel Checks** - Added `if interaction.channel:` checks throughout play command and responses
- **Rate Limiting** - Added 100ms cooldown between tinyurl API calls to prevent getting banned
- **Timeout on `/queue` Display** - Added 10s timeout to `asyncio.gather()` for URL shortening; falls back to full URLs on timeout
- **Retry Logic Bug** - Fixed off-by-one error in retry counter (was `< 1` now correctly `== 0`)
- **Channel Send Errors** - Wrapped all `channel.send()` calls in try/except to handle deleted channels
- **Overlapping Playlist Tasks** - Added `loading_tasks` dict to prevent multiple `process_rest()` from running on same guild
- **Missing Guild Cleanup** - Task cancellation added to `cleanup_guild()` function

### Added
- **Rate Limiter** - `SHORTEN_RATE_LIMIT = 0.1` constant with global timestamp tracking
- **Task Tracking** - `loading_tasks: dict[int, asyncio.Task]` to prevent concurrent playlist loading
- **Better Error Messages** - Connection errors now show "missing permissions or bot is banned?" 
- **Timeout Handling** - `/queue` gracefully shows without links if shortening takes too long
- **Channel Error Logging** - Proper exception handling for all Discord API calls

### Changed
- **Type Hints** - `get_queue()` now returns `list[YTDLSource]` instead of generic `list`
- **Error Handling** - `.connect()` in join now wrapped in try/except with useful error messages
- **Channel Null Safety** - All message sends now check if channel exists first

---

## [2025-12-27] - Performance & Stability Improvements

### Added
- **URL Shortener Cache** - Caches shortened URLs to avoid redundant API calls for duplicate links
- **Queue Size Limit** - Maximum 100 songs per guild to prevent memory issues
- **Guild Cleanup Function** - Properly cleans up guild state (`queues`, `loading_playlists` flags) on disconnect
- **5-Second Timeout for Playlist Loading** - Waits max 5 seconds for new songs instead of indefinite waiting

### Changed
- **Parallel URL Shortening in `/queue`** - Changed from sequential shortening to `asyncio.gather()` for ~20x faster response time
- **Improved Empty Queue Handling** - Better logic when queue is empty but playlists are still loading
- **URL Shortener Whitespace** - Added `.strip()` to remove extra whitespace from tinyurl responses

### Fixed
- **Memory Leak** - `loading_playlists` flag now properly cleared on guild disconnect
- **Race Condition** - Improved handling when `/clearqueue` is called while new songs are being added
- **Infinite Sleep Loop** - Replaced `await asyncio.sleep(1)` with bounded loop (5 iterations max)
- **Early Disconnect Bug** - Bot no longer disconnects prematurely when playlists are still being processed

### Performance
- `/queue` command is now **~20x faster** with parallel URL shortening
- Reduced API calls by caching shortened URLs
- Fixed potential event loop blocking from sequential shortening operations

### Technical Details
- Added `url_shortener_cache: dict[str, str]` for caching
- Added `MAX_QUEUE_SIZE = 100` constant
- Added `cleanup_guild(guild_id: int)` function for proper state management
- Changed queue waiting logic from simple sleep to loop-based polling with timeout

---

## Previous Features
- Max songs 20 (yt_dlp playlist_items)
- Shuffle/mix support
- Per-guild queues
- Proper followup/defer handling
- after-callback with asyncio.run_coroutine_threadsafe
- Async URL shortener (non-blocking)
