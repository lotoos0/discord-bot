@echo off
title Discord Bot
cd /d "C:\Users\andii\bot\discord-bot"
:loop
".venv\Scripts\python.exe" "main.py"
echo Bot stopped. Restarting in 5 seconds... (close window to stop)
timeout /t 5
goto loop