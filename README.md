# Video Yukla Bot

A Telegram bot that downloads YouTube and Instagram videos and sends them back as video files.

## Features
- Responds to `/start` with a greeting in Uzbek.
- Accepts a YouTube or Instagram video URL and returns the video.
- Shows a *"Video yuklab olinmoqda..."* status message while downloading.
- Handles errors (invalid URL, download failures, size limits, network issues).
- Cleans up temporary files after each request.
- Works on Windows (no Docker required).

## Prerequisites
1. **Python 3.11+** – make sure it is added to your `PATH`.
2. **FFmpeg** – required by `yt‑dlp` for some formats.
   - Install via Chocolatey:
     ```powershell
     choco install ffmpeg
     ```
   - Or download the static build from https://ffbinaries.com and add the `bin` folder to your `PATH`.
3. **Telegram Bot Token** – create a bot with @BotFather and obtain the token.

## Installation
```powershell
# 1️⃣ Clone or copy the project folder
cd C:\Users\HP\OneDrive\Desktop\video-yukla-bot

# 2️⃣ Create a virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1  # PowerShell
# (or `venv\Scripts\activate.bat` in cmd)

# 3️⃣ Install dependencies
pip install -r requirements.txt
```

## Configuration
1. Copy the example environment file and fill in your token:
```powershell
copy .env.example .env
notepad .env   # replace `your_telegram_bot_token_here` with the real token
```
   The `.env` file is ignored by Git (`.gitignore`).

## Running the Bot
```powershell
python bot.py
```
The bot will start polling Telegram. Send a message with a YouTube or Instagram link and receive the video.

## Limitations
- Telegram limits video files to **50 MB** for bots. If the downloaded video exceeds this size, the bot will inform the user and abort.
- The bot does **not** upload large files to external storage; it only returns what fits within Telegram limits.
- Only publicly accessible videos are supported – no login‑required or private content.

## Project Structure
```
video-yukla-bot/
├─ bot.py          # main source code
├─ requirements.txt
├─ .env.example    # sample environment variables
├─ .gitignore
└─ README.md
```

## Troubleshooting
- **`ffmpeg` not found** – ensure the `ffmpeg` executable is available in your `PATH`.
- **Download hangs / fails** – check your internet connection and verify the URL works in a browser.
- **Bot does not respond** – make sure the bot token is correct and that the bot is not blocked by Telegram.

## License
MIT – feel free to modify and use it as you wish.
