# CAS Bot — Setup Guide (about 30 minutes, one time)

**How it works each day:**
- **7 AM IST.** The bot scans Google News, Google Alerts, Telegram channels and watched pages, then keeps only CAS-relevant items.
- **8 AM to 8 PM, every 2 hours.** It sends a few items at a time, so alerts are spread through the day.

**Each opportunity arrives as one short message:**
```
🎓 One-week FDP on Green Chemistry
🏛 UGC-MMTTC, University of Mumbai
📍 Online   💰 ₹1,500
⏰ Deadline: 10th November 2026
🔗 Open link
```
If a detail isn't mentioned in the source, the message says **"Check link"**.

## What you need (all free except the optional Claude key)
| Thing | Cost | Purpose |
|---|---|---|
| GitHub account | Free | Stores the code and runs it daily |
| Telegram bot | Free | Main digest |
| CallMeBot key | Free | WhatsApp summary |
| Gemini API key | Free | Smart AI filtering + summaries (optional — without it, free keyword filtering is used) |

---

## Step 1 — Create the Telegram bot
1. In Telegram, search **@BotFather**, then send `/newbot`.
2. Give it a name (e.g. *CAS Alerts*) and a username ending in `bot`.
3. Copy the **token** it gives you. It looks like `123456:ABC-xyz...`.
4. Open your new bot and send it any message, e.g. "hi".
5. In a browser, open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`.
6. Find `"chat":{"id":123456789` and copy that number. This is your **chat ID**.

## Step 2 — WhatsApp key (CallMeBot)
1. Go to callmebot.com and open the WhatsApp API page.
2. Save their WhatsApp number shown there in your contacts.
3. Send it this exact message: `I allow callmebot to send me messages`
4. You will receive your **apikey** on WhatsApp.
5. Your phone number goes in the format `+91XXXXXXXXXX`.

## Step 3 — Free Gemini key (optional, for smart filtering)
1. Go to **aistudio.google.com** and sign in with your Gmail.
2. Click **Get API key**, then **Create API key**.
3. Copy the key. No card or billing is needed for the free tier.
4. Skip this step to use keyword mode only.

Note: on the free tier, Google may use what is sent to improve its models. The bot only sends public news headlines, so this is fine.

## Step 4 — Put the code on GitHub
1. Create a free account at github.com.
2. Click **New repository** and name it `cas-bot`.
3. Choose **Private**, then click Create.
4. Click **Add file → Upload files** and upload: `bot.py`, `requirements.txt`, `queries.txt`, `feeds.txt`, `watch_pages.txt`, `seen.json`, `README.md`.
5. Click **Add file → Create new file**. Type the name exactly: `.github/workflows/cas-bot.yml`
6. Paste the contents of `cas-bot.yml` into it, then click Commit.

## Step 5 — Add your secret keys
1. In the repo, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** and add each of these:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from Step 1 |
| `TELEGRAM_CHAT_ID` | chat ID from Step 1 |
| `WHATSAPP_PHONE` | +91XXXXXXXXXX |
| `CALLMEBOT_APIKEY` | key from Step 2 |
| `GEMINI_API_KEY` | key from Step 3 (optional) |

Keys stored as secrets are encrypted, and nobody can see them, not even in logs.

## Step 6 — Test it
1. Open the **Actions** tab, enable workflows if asked, and click **CAS Bot**.
2. Click **Run workflow**.
3. Within 2–3 minutes your first digest should arrive.

After that it runs by itself every day.

---

## Changing the sending times
In `.github/workflows/cas-bot.yml`, the second cron line controls when messages go out. The times are in UTC, and IST = UTC + 5:30.

If you change it, also update `SEND_HOURS_IST` near the top of `bot.py` so both match. GitHub sometimes starts scheduled runs 5–30 minutes late, which is normal.

Each sending time delivers at most 8 messages. Change `MAX_PER_SLOT` in `bot.py` to adjust this.

## Customising (edit files directly on GitHub)
- **queries.txt**: add or remove search topics.
- **feeds.txt**: paste Google Alerts RSS links. This is highly recommended because it covers college websites and PDF notices.
- **telegram_channels.txt**: add public Telegram channels that post FDPs, conferences, calls for chapters and patents. The bot reads their latest posts daily.
- **watch_pages.txt**: add notice or circular pages (UGC-MMTTCs, MSFDA, University of Mumbai, ANRF calls).
- **PROFILE** at the top of `bot.py`: describe yourself so the AI judges relevance better.
- **Time**: change `cron: "30 1 * * *"` in the workflow file. The time is in UTC, and IST = UTC + 5:30.

## Troubleshooting
- **No message arrives.** Check the Actions tab, then the latest run's log.
- **"Permission denied" on push.** Go to Settings → Actions → General → Workflow permissions and select **Read and write**.
- **Too many irrelevant items.** Add the free Gemini key, or remove broad queries.
- **Gemini error about the model name.** Add a secret or edit `GEMINI_MODEL` in bot.py to a current Flash model listed in AI Studio.
- **Too few items.** Add Google Alerts RSS feeds.
