# Discord Music Bot 🎶

A lightweight Discord music bot written in **Python 3.11+** using [discord.py](https://github.com/Rapptz/discord.py), [yt-dlp](https://github.com/yt-dlp/yt-dlp) and **FFmpeg**.  
Plays music from YouTube (single songs and playlists), manages a per-guild queue, and supports basic playback commands.

---

## ✨ Features

- Slash commands (`/play`, `/queue`, `/skip`, `/clearqueue`, `/join`, `/leave`)
- Play both **single YouTube links** and **playlists** (max 20 items)
- **Per-guild queues** (each server has its own independent queue)
- **Auto-reconnect for FFmpeg streams** to handle YouTube resets
- **Async URL shortener** (TinyURL) – non-blocking in event loop
- Error handling for broken/removed videos (skipped gracefully)
- Environment variable for Discord token (`DISCORD_TOKEN`)

---

## 🚀 Getting Started

### Requirements
- Python **3.11+**
- [FFmpeg](https://ffmpeg.org/download.html) (must be installed and available in `PATH`)
- [Opus](https://opus-codec.org/) libraries (e.g. `libopus0` on Debian/Ubuntu)

---

## 🐳 Running with Docker

Build and run the bot inside a container:

``` docker build -t discord-music-bot . ``` 

``` docker run --rm --env-file .env discord-music-bot ```

Dependencies like ffmpeg and libopus are already included in the Dockerfile.

---

## ⚙️ Commands

- ```/join``` -> Bot joins your current voice channel
- ```/leave``` -> Bot leaves the voice channel
- ```/play <url>``` -> Add a YouTube song/playlist to the queue
    - First item in a playlist starts immediately
    - Remaining items are processed in the background
- ```/queue``` -> Display the current queue (max 20 items)
- ```/skip``` -> Skip the currently playing track
- ```/clearqueue``` -> Clear the queue

## TODO

### Planned Features
- ```/pause```, ```/resume```, ```/stop```
- ```/nowplaying``` (show current track + progress bar)
- Idle auto-disconnect after X minutes
