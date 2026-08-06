# Safiya Speaking Partner — matching backend + mini app

This is a SEPARATE service from the Safiya bot. It does NOT touch the bot code.

## What's here
- `server.py` — matching backend (pairs two students at the same level)
- `public/index.html` — the mini app page students see
- `requirements.txt`, `Procfile` — for Railway

## Deploy (Railway)
1. Put these files in a NEW GitHub repo (e.g. `safiya-speaking`).
2. Railway → New Project → Deploy from GitHub → pick that repo.
3. Railway auto-detects Python and runs the Procfile.
4. Once deployed, Railway gives a public URL like `https://safiya-speaking-production.up.railway.app`
5. In BotFather → your bot → Menu Button → set the URL to that Railway URL.

## Test
- Open the Railway URL in a browser → you should see the level picker.
- Open on two phones (or two Telegram accounts), both pick the same level → they match.

## Next step (voice)
Voice is added with LiveKit — a separate step once matching is confirmed working.
