import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "ranked_data.json"
STARTING_ELO = 1000
K_FACTOR = 32

# All Civ 5 civs (BNW + Gods & Kings + Lek mod)
ALL_CIVS = sorted([
    "America", "Arabia", "Assyria", "Austria", "Aztec", "Babylon", "Brazil",
    "Byzantium", "Carthage", "Celts", "China", "Denmark", "Egypt", "England",
    "Ethiopia", "France", "Germany", "Greece", "Huns", "Inca", "India",
    "Indonesia", "Iroquois", "Japan", "Korea", "Maghreb", "Maya", "Mongolia",
    "Morocco", "Netherlands", "Ottomans", "Persia", "Poland", "Polynesia",
    "Portugal", "Rome", "Russia", "Shoshone", "Siam", "Songhai", "Spain",
    "Sweden", "Venice", "Zulu",
    "Akkad", "Aksum", "Argentina", "Armenia", "Australia", "Ayyubids",
    "Belgium", "Boers", "Bolivia", "Brunei", "Bulgaria", "Burma", "Canada",
    "Chile", "Colombia", "Cuba", "Finland", "Franks", "Gaul", "Georgia",
    "Golden Horde", "Goths", "Hittites", "Hungary", "Ireland", "Israel",
    "Italy", "Jerusalem", "Khmer", "Kilwa", "Kongo", "Lithuania", "Macedonia",
    "Madagascar", "Manchuria", "Maori", "Maurya", "Mexico", "Moors", "Mughals",
    "Mysore", "Nabataea", "New Zealand", "Normandy", "Norway", "Nubia", "Oman",
    "Palmyra", "Papal States", "Philippines", "Phoenicia", "Prussia", "Romania",
    "Scotland", "Sioux", "Sumeria", "Switzerland", "Tibet", "Timurids", "Tonga",
    "Turkey", "UAE", "Ukraine", "Vietnam", "Wales", "Yugoslavia", "Zimbabwe"
])

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
            "civs": {}
        }
    return data["players"][user_id]

def calc_multiplayer_elo(players: list) -> list:
    """
    Elo for 2-8 players based on finishing position.
    Every player is compared against every other player pairwise.
    K factor scales down in larger games so total swing stays fair.
    """
    n = len(players)
    deltas = [0.0] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            score = 1 if players[i]["finish"] < players[j]["finish"] else 0
            expected = 1 / (1 + 10 ** ((players[j]["elo"] - players[i]["elo"]) / 400))
            k = K_FACTOR / (n - 1)
            deltas[i] += k * (score - expected)
    return [max(round(players[i]["elo"] + deltas[i]), 100) for i in range(n)]

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
                    "🎮 **Go play your game!** When done, report results with:\n"
                    "`/report_results @1st @2nd` (add more players for multiplayer)",
        color=0x4CAF50
    )
    await interaction.response.send_message(embed=embed)

# ── /cancel_game ─────────────────────────────────────────────────────────────
@bot.tree.command(name="cancel_game", description="Cancel an unfinished game — no Elo changes")
@app_commands.describe(
    player2="Another player in the game",
    player3="(optional)",
    player4="(optional)",
    player5="(optional)",
    player6="(optional)",
    player7="(optional)",
    player8="(optional)",
)
async def cancel_game(
    interaction: discord.Interaction,
    player2: discord.Member,
    player3: Optional[discord.Member] = None,
    player4: Optional[discord.Member] = None,
    player5: Optional[discord.Member] = None,
    player6: Optional[discord.Member] = None,
    player7: Optional[discord.Member] = None,
    player8: Optional[discord.Member] = None,
):
    raw = [interaction.user, player2, player3, player4, player5, player6, player7, player8]
    members = [m for m in raw if m is not None]
    caller_id = interaction.user.id

    # Make sure the caller is actually one of the players (always true since
    # interaction.user is always included, but good to be explicit)
    member_ids = [m.id for m in members]
    if caller_id not in member_ids:
        await interaction.response.send_message("❌ You can only cancel games you're part of.", ephemeral=True)
        return

    data = load_data()

    # Look for any open challenge involving these players and remove it
    keys_to_delete = []
    for key in data.get("challenges", {}):
        parts = key.split("-")
        if len(parts) == 2:
            a, b = int(parts[0]), int(parts[1])
            if a in member_ids and b in member_ids:
                keys_to_delete.append(key)

    for key in keys_to_delete:
        del data["challenges"][key]

    if keys_to_delete:
        save_data(data)

    # Build mention list for the embed (everyone except the caller)
    others = [m.mention for m in members if m.id != caller_id]
    others_str = ", ".join(others) if others else "the other players"

    embed = discord.Embed(
        title="🚫  Game Cancelled",
        description=f"{interaction.user.mention} has cancelled the game with {others_str}.\n\n"
                    "No Elo changes have been made.",
        color=0x888888
    )
    await interaction.response.send_message(embed=embed)

# ── /report_results ───────────────────────────────────────────────────────────
@bot.tree.command(
    name="report_results",
    description="Report finishing positions for a ranked game (1v1 up to 8 players)"
)
@app_commands.describe(
    first="Player who finished 1st",
    second="Player who finished 2nd",
    third="Player who finished 3rd (optional)",
    fourth="Player who finished 4th (optional)",
    fifth="Player who finished 5th (optional)",
    sixth="Player who finished 6th (optional)",
    seventh="Player who finished 7th (optional)",
    eighth="Player who finished 8th (optional)",
)
async def report_results(
    interaction: discord.Interaction,
    first: discord.Member,
    second: discord.Member,
    third: Optional[discord.Member] = None,
    fourth: Optional[discord.Member] = None,
    fifth: Optional[discord.Member] = None,
    sixth: Optional[discord.Member] = None,
    seventh: Optional[discord.Member] = None,
    eighth: Optional[discord.Member] = None,
):
    raw = [first, second, third, fourth, fifth, sixth, seventh, eighth]
    members = [m for m in raw if m is not None]

    ids = [m.id for m in members]
    if len(ids) != len(set(ids)):
        await interaction.response.send_message("❌ Duplicate players detected — each player can only appear once.", ephemeral=True)
        return
    if any(m.bot for m in members):
        await interaction.response.send_message("❌ Bots can't be ranked players.", ephemeral=True)
        return

    data = load_data()

    player_info = []
    for i, member in enumerate(members):
        p = get_player(data, str(member.id))
        player_info.append({
            "id": str(member.id),
            "member": member,
            "finish": i + 1,
            "elo": p["elo"],
            "old_elo": p["elo"],
        })

    new_elos = calc_multiplayer_elo(player_info)

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
    result_lines = []

    for i, info in enumerate(player_info):
        p = get_player(data, info["id"])
        old_elo = info["old_elo"]
        new_elo = new_elos[i]
        diff = new_elo - old_elo
        sign = "+" if diff >= 0 else ""
        p["elo"] = new_elo
        if i == 0:
            p["wins"] += 1
        else:
            p["losses"] += 1
        result_lines.append(
            f"{medals[i]} **{info['member'].display_name}** — "
            f"{old_elo} → **{new_elo}** ({sign}{diff})  {rank_label(new_elo)}"
        )

    data["matches"].append({
        "type": f"{len(members)}-player",
        "players": [
            {
                "id": info["id"],
                "finish": info["finish"],
                "elo_before": info["old_elo"],
                "elo_after": new_elos[i]
            }
            for i, info in enumerate(player_info)
        ],
        "played_at": datetime.utcnow().isoformat()
    })

    save_data(data)

    embed = discord.Embed(
        title=f"🏛️  {len(members)}-Player Match Recorded!",
        description="\n".join(result_lines),
        color=0xD4A017
    )
    embed.set_footer(text=f"Reported by {interaction.user.display_name}")
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
    embed.add_field(name="Elo",    value=f"**{stats['elo']}**",          inline=True)
    embed.add_field(name="Rank",   value=rank_label(stats["elo"]),        inline=True)
    embed.add_field(name="Record", value=f"{w}W / {l}L ({winrate}% WR)", inline=True)

    civs = stats.get("civs", {})
    if civs:
        civ_lines = sorted(civs.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True)[:5]
        civ_text = "\n".join(f"**{c}** — {v['wins']}W / {v['losses']}L" for c, v in civ_lines)
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

    sizes = {}
    for m in matches:
        t = m.get("type", "unknown")
        sizes[t] = sizes.get(t, 0) + 1

    size_text = "\n".join(f"{k}: {v}" for k, v in sorted(sizes.items())) or "N/A"

    embed = discord.Embed(title="📊  Server Stats", color=0x6B238E)
    embed.add_field(name="Total Matches",  value=str(total),         inline=True)
    embed.add_field(name="Ranked Players", value=str(total_players), inline=True)
    embed.add_field(name="Game Types",     value=size_text,          inline=False)
    await interaction.response.send_message(embed=embed)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set the DISCORD_TOKEN environment variable!")
    bot.run(TOKEN)
