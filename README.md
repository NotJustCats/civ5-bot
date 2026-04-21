# 🏛️ Civ 5 Ranked Bot

A Discord bot for running a 1v1 Elo-rated ranked ladder among your friends in Civilization 5.

---

## Features

| Command | Description |
|---|---|
| `/challenge @player` | Issue a ranked challenge |
| `/accept @player` | Accept a pending challenge |
| `/report_win @loser [your_civ] [their_civ]` | Record a match result |
| `/leaderboard` | Top 10 players by Elo |
| `/profile [@player]` | Full stats + civ history |
| `/stats` | Server-wide match stats |
| `/civs` | List all valid civ names |

### Elo System
- Everyone starts at **1000 Elo**
- Uses standard K=32 Elo formula (same as FIDE chess)
- Floor at 100 Elo so nobody goes negative

### Rank Tiers
| Tier | Elo |
|---|---|
| 🏆 Deity | 1800+ |
| ⚔️ Emperor | 1600–1799 |
| 🛡️ King | 1400–1599 |
| ⚙️ Prince | 1200–1399 |
| 🌿 Chieftain | 1000–1199 |
| 🪨 Settler | below 1000 |

---

## Setup

### 1. Create a Discord Bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → give it a name
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Copy your **Bot Token** (keep this secret!)
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`
7. Use the generated URL to invite the bot to your server

### 2. Install & Run

```bash
# Install dependencies
pip install -r requirements.txt

# Set your bot token
export DISCORD_TOKEN="your-token-here"   # Mac/Linux
set DISCORD_TOKEN=your-token-here        # Windows

# Run the bot
python bot.py
```

On first startup you'll see:
```
✅  Logged in as YourBot#1234 — slash commands synced.
```
Slash commands may take up to a minute to appear in Discord.

### 3. Keep It Running (optional)

To run 24/7, use a free host like **Railway**, **Fly.io**, or a cheap VPS.
Set `DISCORD_TOKEN` as an environment variable in your host's dashboard.

---

## Data Storage

All data is saved to `ranked_data.json` in the same folder as `bot.py`.
Back this file up occasionally — it contains all Elo ratings and match history.

---

## Example Flow

```
User A:  /challenge @UserB
UserB:   /accept @UserA
         ... they play the game ...
UserA:   /report_win @UserB America Poland
         → Elo updates shown for both players
UserA:   /leaderboard
         → Rankings displayed
```
