# KSM POS Telegram Store Bot — Python version

Same shop, same Google Sheet as the Apps Script V7 bot, rewritten in Python.
Why: the Apps Script version wakes up cold on every tap (slow). This version
runs 24/7 on Railway, keeps the catalog in memory, and answers instantly.

**No Google Cloud console, no service account needed.** The Python bot talks
to your Google Sheet through your existing Apps Script project (`/exec` URL)
using the included `TelegramSheetBridge.gs`.

## Files

- `bot.py` — the Telegram bot (aiogram, long polling)
- `store.py` — shop logic + sheet access through the bridge
- `bridge.py` — talks to the Apps Script `/exec` URL
- `TelegramSheetBridge.gs` — ADD THIS to your Apps Script project
- `Code_gs_patch.txt` — tiny edit to your Code.gs `doPost(e)`
- `requirements.txt`, `Procfile` — for Railway

## Part 1 — Apps Script side (5 minutes, one time)

1. Open your Apps Script project (the POS one).
2. Add a new script file named `TelegramSheetBridge`, paste the entire
   contents of `TelegramSheetBridge.gs` into it.
3. Open `Code_gs_patch.txt` and follow it: it adds one small block inside
   your `doPost(e)` (right before the "Existing GitHub POS API" line).
4. Go to **Project Settings → Script Properties** and add:
   - Name: `TG_BRIDGE_SECRET`
   - Value: pick a long random password (e.g. 32 letters/numbers). The bot
     and the script must use the SAME secret.
5. **Deploy → Manage deployments → Edit (pencil) → Version: New version →
   Deploy.** (Without this, the old code stays live.)
6. Copy the **Web App URL** (ends with `/exec`). The Python bot needs it.

Quick check (optional): open this in your browser — it should say
`{"success":false,"error":"Forbidden"}` (that means the bridge is live and
the secret check works):
`https://script.google.com/macros/s/YOUR_ID/exec`
(replace with your real URL; a POST with the right secret is what the bot uses)

## Part 2 — GitHub

1. On https://github.com create a new repository, e.g. `ksm-pos-bot`.
2. Upload: `bot.py`, `store.py`, `bridge.py`, `requirements.txt`, `Procfile`.
   (The `.gs` / `.txt` files stay in Apps Script — do NOT upload them.)

## Part 3 — Railway

1. https://railway.app → log in with GitHub → **New Project** →
   **Deploy from GitHub repo** → choose your repo.
2. Click the service → **Variables** tab → add:
   - `BOT_TOKEN` = the ksmpos_bot token from @BotFather
   - `APPS_SCRIPT_URL` = your Web App URL from Part 1 (ends with `/exec`)
   - `TG_BRIDGE_SECRET` = the SAME secret you put in Script Properties
   - `ADMIN_IDS` = your numeric Telegram ID (send `/id` to the old bot)
3. Railway redeploys automatically. **Deployments** → newest → **View Logs** →
   look for `KSM POS bot is running (polling)...`
4. On first start the bot deletes the old Apps Script webhook, so only this
   Python bot receives messages from now on.

## Part 4 — Test

Send `/start` to ksmpos_bot in Telegram. Browse products — it should feel
much faster than before.

## Notes

- Catalog cache: 30 min, product cache: 15 min. Checkout always re-reads
  fresh prices/stock, so money is never wrong.
- Tap **🔄 Refresh Products** in the bot for an instant reload after editing
  products in the sheet.
- Top-up slips: the customer sends the KBZPay slip as a photo; admins get
  the photo with ✅ Approve / ❌ Reject buttons, same as before.
- Keep the old Apps Script project as a backup, but do NOT run its
  `setupTelegramStoreWebhook()` again — that would steal the bot back from
  the Python version.
- If the bot ever says "bridge" errors in the logs, check: (1) the
  TG_BRIDGE_SECRET matches on both sides, (2) you deployed a NEW version
  of the Apps Script web app after adding the bridge.
