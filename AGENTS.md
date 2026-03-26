# AGENTS.md

Persistent instructions for agents working in this repository.

## Project Focus

- This repository is a Discord music bot.
- Preserve current slash-command behavior unless the user explicitly asks for a behavior change.
- The highest-risk areas are async playback flow, per-guild queue isolation, disconnect logic, lazy source resolution, and background playlist loading.
- `start-bot.bat` should continue to work as a one-click launcher for `main.py` unless a task explicitly changes the startup flow.

## When Planning Is Required

Create an implementation plan before editing code when a task:

- touches more than one module,
- changes queue, playback, disconnect, playlist, or CI behavior,
- is likely to take more than 20-30 minutes,
- is a refactor, migration, or multi-step cleanup.

Large tasks should be executed milestone by milestone, with validation after each milestone.

## Coding Rules

- Follow Clean Code style: small focused functions, intention-revealing names, and minimal duplication.
- Prefer explicit control flow over clever shortcuts.
- Keep code, comments, logs, and developer-facing documentation in English unless the user asks otherwise.
- Do not introduce architecture churn without a clear payoff.
- Avoid hidden side effects. If state changes, make that obvious in the function name or structure.

## Repo-Specific Guardrails

- Keep queue state isolated per guild.
- Do not break `MusicState.cleanup_guild()`, loading task cancellation, or disconnect lock behavior.
- Treat `play_next()`, `on_voice_state_update()`, and playlist background loading as regression-prone code paths.
- For fixes in queue/playback/disconnect flow, add or update offline unit tests when practical.
- Keep tests offline. Do not require real Discord, FFmpeg, cookies, or live `yt-dlp` network calls in unit tests.

## Dependency And Docs Consistency

- If a dependency is added or removed, update `requirements.txt` in the same change.
- If behavior, commands, setup steps, or dependencies change, review `README.md` and `CHANGELOG.md` in the same change.
- Do not leave repo metadata in a contradictory state. Example: if the changelog says a dependency was removed, `requirements.txt` must match.

## Safety And Change Scope

- Never commit secrets, tokens, `.env` contents, cookies, session files, or machine-local credentials.
- Prefer structured logging over `print()` for runtime diagnostics.
- Keep diffs focused. Do not mix the requested change with unrelated refactors or cleanup unless explicitly justified.

## Validation

Before pushing, run the real checks that exist in this repo:

- `python -m isort --profile black --check-only --diff .`
- `python -m black --check .`
- `python -m unittest -v`

When Python source files change, also run:

- `python -m py_compile main.py music_audio.py music_service.py music_state.py`

If a command cannot be run in the current environment, say so clearly in the final report.

## Done Means

A task is not done until:

- the requested change is implemented,
- relevant tests are updated when practical,
- the validation commands have been run or the blocker is explained,
- touched docs and dependency files are consistent with the code,
- the diff stays focused on the task unless expansion is justified.
