import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import math
from datetime import datetime
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")          # set in your environment
DATA_FILE = "ranked_data.json"
STARTING_ELO = 1000
K_FACTOR = 32                               # how much Elo shifts per game

# All Civ 5 civs (BNW + Gods & Kings)
ALL_CIVS = [
    "America", "Arabia", "Assyria", "Austria", "Aztec", "Babylon", "Brazil",
    "Byzantium", "Carthage", "Celts", "China", "Denmark", "Egypt", "England",
    "Ethiopia", "France", "Germany", "Greece", "Huns", "Inca", "India",
    "Indonesia", "Iroquois", "Japan", "Korea", "Maghreb", "Maya", "Mongolia",
    "Morocco", "Netherlands", "Ottomans", "Persia", "Poland", "Polynesia",
    "Portugal", "Rome", "Russia", "Shoshone", "Siam", "Songhai", "Spain",
    "Sweden", "Venice", "Zulu"
]

# ── Data helpers ─────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"players": {}, "challenges": {}, "matches": []}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_player(data: dict, user_id: str) -> dict:
    if user_id not in data["players"]:
        data["players"][user_id] = {
            "elo": STARTING_ELO,
            "wins": 0,
            "losses": 0,
            "civs": {}           # civ_name -> {"wins": n, "losses": n}
        }
    return data["players"][user_id]

def calc_elo(winner_elo: int, loser_elo: int) -> tuple[int, int]:
    """Returns (new_winner_elo, new_loser_elo)."""
    expected_w = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    expected_l = 1 - expected_w
    new_winner = round(winner_elo + K_FACTOR * (1 - expected_w))
    new_loser  = round(loser_elo  + K_FACTOR * (0 - expected_l))
    return new_winner, new_loser

def rank_label(elo: int) -> str:
    if elo >= 1800: return "🏆 Deity"
    if elo >= 1600: return "⚔️  Emperor"
    if elo >= 1400: return "🛡️  King"
    if elo >= 1200: return "⚙️  Prince"
    if elo >= 1000: return "🌿 Chieftain"
    return              "🪨 Settler"

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅  Logged in as {bot.user} — slash commands synced.")

# ── /challenge ───────────────────────────────────────────────────────────────
@bot.tree.command(name="challenge", description="Challenge another player to a ranked 1v1")
@app_commands.describe(opponent="The player you want to challenge")
async def challenge(interaction: discord.Interaction, opponent: discord.Member):
    if opponent.bot or opponent.id == interaction.user.id:
        await interaction.response.send_message("❌ Invalid opponent.", ephemeral=True)
        return

    data = load_data()
    key = f"{interaction.user.id}-{opponent.id}"

    if key in data["challenges"]:
        await interaction.response.send_message("⚠️ You already have an open challenge against that player!", ephemeral=True)
        return

    data["challenges"][key] = {
        "challenger": interaction.user.id,
        "opponent":   opponent.id,
        "created_at": datetime.utcnow().isoformat()
    }
    save_data(data)

    embed = discord.Embed(
        title="⚔️  Ranked Challenge Issued!",
        description=f"{interaction.user.mention} challenges {opponent.mention} to a **Civ 5 1v1**!\n\n"
                    f"{opponent.mention} — accept with `/accept @{interaction.user.display_name}` or ignore to decline.",
        color=0xD4A017
    )
    await interaction.response.send_message(embed=embed)

# ── /accept ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="accept", description="Accept a pending challenge")
@app_commands.describe(challenger="The player who challenged you")
async def accept(interaction: discord.Interaction, challenger: discord.Member):
    data = load_data()
    key = f"{challenger.id}-{interaction.user.id}"

    if key not in data["challenges"]:
        await interaction.response.send_message("❌ No pending challenge from that player.", ephemeral=True)
        return

    del data["challenges"][key]
    save_data(data)

    embed = discord.Embed(
        title="✅  Challenge Accepted!",
        description=f"{interaction.user.mention} has accepted {challenger.mention}'s challenge!\n\n"
                    "🎮 **Go play your game!** When done, the winner reports with:\n"
                    f"`/report_win @loser [your_civ] [their_civ]`",
        color=0x4CAF50
    )
    await interaction.response.send_message(embed=embed)

# ── /report_win ───────────────────────────────────────────────────────────────
@bot.tree.command(name="report_win", description="Report that you won a ranked match")
@app_commands.describe(
    loser="The player you beat",
    your_civ="The civ you played (optional)",
    their_civ="The civ they played (optional)"
)
async def report_win(
    interaction: discord.Interaction,
    loser: discord.Member,
    your_civ: Optional[str] = None,
    their_civ: Optional[str] = None
):
    if loser.bot or loser.id == interaction.user.id:
        await interaction.response.send_message("❌ Invalid opponent.", ephemeral=True)
        return

    # Validate civs
    if your_civ and your_civ not in ALL_CIVS:
        await interaction.response.send_message(
            f"❌ Unknown civ `{your_civ}`. Check `/civs` for the full list.", ephemeral=True)
        return
    if their_civ and their_civ not in ALL_CIVS:
        await interaction.response.send_message(
            f"❌ Unknown civ `{their_civ}`. Check `/civs` for the full list.", ephemeral=True)
        return

    data = load_data()
    winner_id = str(interaction.user.id)
    loser_id  = str(loser.id)

    w = get_player(data, winner_id)
    l = get_player(data, loser_id)

    old_w_elo = w["elo"]
    old_l_elo = l["elo"]
    new_w_elo, new_l_elo = calc_elo(old_w_elo, old_l_elo)

    w["elo"]   = new_w_elo
    w["wins"] += 1
    l["elo"]    = max(new_l_elo, 100)   # floor at 100
    l["losses"] += 1

    # Track civ stats
    if your_civ:
        w["civs"].setdefault(your_civ, {"wins": 0, "losses": 0})["wins"] += 1
    if their_civ:
        l["civs"].setdefault(their_civ, {"wins": 0, "losses": 0})["losses"] += 1

    # Log match
    data["matches"].append({
        "winner": winner_id,
        "loser":  loser_id,
        "winner_civ": your_civ,
        "loser_civ":  their_civ,
        "winner_elo_before": old_w_elo,
        "loser_elo_before":  old_l_elo,
        "winner_elo_after":  new_w_elo,
        "loser_elo_after":   new_l_elo,
        "played_at": datetime.utcnow().isoformat()
    })

    save_data(data)

    civ_line = ""
    if your_civ or their_civ:
        civ_line = f"\n🗺️  **{your_civ or '?'}** vs **{their_civ or '?'}**"

    embed = discord.Embed(
        title="🏅  Match Recorded!",
        color=0xD4A017
    )
    embed.add_field(
        name=f"🥇 {interaction.user.display_name}",
        value=f"Elo: **{old_w_elo}** → **{new_w_elo}** (+{new_w_elo - old_w_elo})\n{rank_label(new_w_elo)}",
        inline=True
    )
    embed.add_field(
        name=f"💀 {loser.display_name}",
        value=f"Elo: **{old_l_elo}** → **{new_l_elo}** ({new_l_elo - old_l_elo})\n{rank_label(new_l_elo)}",
        inline=True
    )
    if civ_line:
        embed.set_footer(text=f"Civs played:{civ_line.strip()}")

    await interaction.response.send_message(embed=embed)

# ── /leaderboard ──────────────────────────────────────────────────────────────
@bot.tree.command(name="leaderboard", description="Show the Civ 5 ranked leaderboard")
async def leaderboard(interaction: discord.Interaction):
    data = load_data()
    players = data["players"]

    if not players:
        await interaction.response.send_message("No matches played yet — be the first!", ephemeral=True)
        return

    sorted_players = sorted(players.items(), key=lambda x: x[1]["elo"], reverse=True)

    embed = discord.Embed(title="🌍  Civ 5 Ranked Leaderboard", color=0x8B4513)
    medals = ["🥇", "🥈", "🥉"]
    lines = []

    for i, (uid, stats) in enumerate(sorted_players[:10]):
        medal = medals[i] if i < 3 else f"`#{i+1}`"
        try:
            user = await bot.fetch_user(int(uid))
            name = user.display_name
        except:
            name = f"<@{uid}>"
        w, l = stats["wins"], stats["losses"]
        winrate = round(w / (w + l) * 100) if (w + l) > 0 else 0
        lines.append(
            f"{medal} **{name}** — {stats['elo']} Elo  {rank_label(stats['elo'])}\n"
            f"    W/L: {w}/{l}  ({winrate}% WR)"
        )

    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Top {min(len(sorted_players), 10)} players • Updated just now")
    await interaction.response.send_message(embed=embed)

# ── /profile ──────────────────────────────────────────────────────────────────
@bot.tree.command(name="profile", description="View your ranked profile (or another player's)")
@app_commands.describe(player="Leave blank to see your own profile")
async def profile(interaction: discord.Interaction, player: Optional[discord.Member] = None):
    target = player or interaction.user
    data = load_data()
    stats = get_player(data, str(target.id))

    w, l = stats["wins"], stats["losses"]
    winrate = round(w / (w + l) * 100) if (w + l) > 0 else 0

    embed = discord.Embed(title=f"📜  {target.display_name}'s Profile", color=0x4682B4)
    embed.add_field(name="Elo",      value=f"**{stats['elo']}**",          inline=True)
    embed.add_field(name="Rank",     value=rank_label(stats["elo"]),        inline=True)
    embed.add_field(name="Record",   value=f"{w}W / {l}L ({winrate}% WR)", inline=True)

    # Top 3 civs by games played
    civs = stats.get("civs", {})
    if civs:
        civ_lines = sorted(civs.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True)[:5]
        civ_text = "\n".join(
            f"**{c}** — {v['wins']}W / {v['losses']}L" for c, v in civ_lines
        )
        embed.add_field(name="🗺️  Most Played Civs", value=civ_text, inline=False)

    await interaction.response.send_message(embed=embed)

# ── /civs ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="civs", description="List all valid civilization names for reporting")
async def civs(interaction: discord.Interaction):
    chunks = [ALL_CIVS[i:i+10] for i in range(0, len(ALL_CIVS), 10)]
    embed = discord.Embed(title="🗺️  Valid Civilizations", color=0x2F4F4F)
    for chunk in chunks:
        embed.add_field(name="\u200b", value=", ".join(chunk), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── /stats ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="stats", description="Server-wide match stats")
async def stats(interaction: discord.Interaction):
    data = load_data()
    matches = data["matches"]
    players = data["players"]

    total = len(matches)
    total_players = len(players)

    civ_wins: dict[str, int] = {}
    for m in matches:
        if m.get("winner_civ"):
            civ_wins[m["winner_civ"]] = civ_wins.get(m["winner_civ"], 0) + 1

    top_civ = max(civ_wins, key=civ_wins.get) if civ_wins else "N/A"

    embed = discord.Embed(title="📊  Server Stats", color=0x6B238E)
    embed.add_field(name="Total Matches",  value=str(total),         inline=True)
    embed.add_field(name="Ranked Players", value=str(total_players), inline=True)
    embed.add_field(name="🏆 Best Civ",    value=top_civ,            inline=True)
    await interaction.response.send_message(embed=embed)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set the DISCORD_TOKEN environment variable!")
    bot.run(TOKEN)
