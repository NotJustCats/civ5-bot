import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import tempfile
import asyncio
from aiohttp import web
from datetime import datetime
from typing import Optional

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "ranked_data.json"
STARTING_ELO = 1000
K_FACTOR = 32
MAX_LOBBY_SIZE = 8
FLOOR_ELO = 100
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # set this in Railway variables

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
    """Load data from disk. Returns fresh state if file is missing or corrupted."""
    default = {"players": {}, "lobbies": {}, "active_games": {}, "game_groups": {}, "matches": []}
    if not os.path.exists(DATA_FILE):
        return default
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        # Ensure all top-level keys exist (handles old saves missing new keys)
        for key, val in default.items():
            data.setdefault(key, val)
        return data
    except (json.JSONDecodeError, IOError):
        print("⚠️  WARNING: ranked_data.json is corrupted or unreadable. Starting fresh.")
        return default

def save_data(data: dict):
    """Atomic save — writes to a temp file first, then replaces, so a crash never corrupts data."""
    dir_name = os.path.dirname(os.path.abspath(DATA_FILE))
    try:
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, DATA_FILE)
    except IOError as e:
        print(f"❌ Failed to save data: {e}")

def get_player(data: dict, user_id: str) -> dict:
    if user_id not in data["players"]:
        data["players"][user_id] = {
            "elo": STARTING_ELO,
            "wins": 0,
            "losses": 0,
            "civs": {}
        }
    return data["players"][user_id]

def player_in_active_game(data: dict, user_id: str) -> bool:
    """Check if a player is currently in a started game."""
    return user_id in data.get("active_games", {})

def player_in_any_lobby(data: dict, user_id: str) -> bool:
    """Check if a player is in any open lobby."""
    for lobby in data.get("lobbies", {}).values():
        if user_id in lobby["players"]:
            return True
    return False

def calc_multiplayer_elo(players: list) -> list:
    """
    Zero-sum Elo for 2-8 players.
    Soft floor: if a loser is at the floor they give up nothing and the
    winner gains nothing from that pairing. No Elo is ever created.
    """
    n = len(players)
    deltas = [0.0] * n
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            i_beat_j = players[i]["finish"] < players[j]["finish"]
            expected_i = 1 / (1 + 10 ** ((players[j]["elo"] - players[i]["elo"]) / 400))
            k = K_FACTOR / (n - 1)
            raw_delta = k * ((1 if i_beat_j else 0) - expected_i)
            if i_beat_j and players[j]["elo"] <= FLOOR_ELO:
                continue
            deltas[i] += raw_delta
    return [max(round(players[i]["elo"] + deltas[i]), FLOOR_ELO) for i in range(n)]

def rank_label(elo: int) -> str:
    if elo >= 1800: return "🏆 Deity"
    if elo >= 1600: return "⚔️  Emperor"
    if elo >= 1400: return "🛡️  King"
    if elo >= 1200: return "⚙️  Prince"
    if elo >= 1000: return "🌿 Chieftain"
    return              "🪨 Settler"

def build_lobby_embed(lobby: dict) -> discord.Embed:
    lines = [f"• **{name}** — {civ}" for name, civ in zip(lobby["player_names"], lobby["player_civs"])]
    embed = discord.Embed(title="🏛️  Game Lobby — Open", color=0x4CAF50)
    embed.add_field(name=f"Host: {lobby['host_name']}", value="\n".join(lines) or "—", inline=False)
    embed.set_footer(text="Use /join_lobby @host [civ] to join • Host uses /start_game to begin")
    return embed


# ── Web server (serves the Elo graph page) ───────────────────────────────────
def build_graph_html() -> str:
    """Read current match data and bake it into a self-contained HTML page."""
    data = load_data()
    matches = data.get("matches", [])
    players = data.get("players", {})

    # Only include players who have played at least one game
    active_ids = set()
    for m in matches:
        for p in m.get("players", []):
            active_ids.add(p["id"])

    # Build timeline: start at 1000, then step through each match
    timeline = []
    current_elo = {pid: 1000 for pid in active_ids}

    timeline.append({
        "label": "Start",
        **{pid: 1000 for pid in active_ids}
    })

    sorted_matches = sorted(matches, key=lambda m: m.get("played_at", ""))
    for i, match in enumerate(sorted_matches):
        for p in match.get("players", []):
            if p["id"] in active_ids:
                current_elo[p["id"]] = p["elo_after"]
        date_str = match.get("played_at", "")[:10]
        timeline.append({
            "label": f"G{i+1} ({date_str})",
            **{pid: current_elo[pid] for pid in active_ids}
        })

    # Build player name map — use Discord IDs as fallback
    player_list = [
        {"id": pid, "name": f"Player {i+1}", "finalElo": players.get(pid, {}).get("elo", 1000)}
        for i, pid in enumerate(sorted(active_ids, key=lambda x: players.get(x, {}).get("elo", 0), reverse=True))
    ]

    import json as _json
    timeline_json = _json.dumps(timeline)
    players_json = _json.dumps(player_list)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Civ 5 Ranked — Elo Graph</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #080a0f; color: #e2e8f0; font-family: 'IBM Plex Mono', monospace; min-height: 100vh; padding: 24px; }}
  h1 {{ font-family: 'Cinzel', serif; font-size: 24px; color: #f97316; letter-spacing: 3px; text-shadow: 0 0 30px rgba(249,115,22,0.4); margin-bottom: 4px; }}
  .subtitle {{ color: #475569; font-size: 11px; letter-spacing: 2px; margin-bottom: 24px; }}
  .card {{ background: #0d1017; border: 1px solid #1e2130; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
  .player-grid {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }}
  .player-btn {{ border-radius: 8px; padding: 8px 14px; cursor: pointer; border-width: 1px; border-style: solid; background: transparent; font-family: 'IBM Plex Mono', monospace; transition: opacity 0.15s; text-align: left; }}
  .player-btn.hidden {{ opacity: 0.3; }}
  .player-name {{ font-weight: 600; font-size: 13px; }}
  .player-elo {{ font-size: 11px; color: #64748b; margin-top: 2px; }}
  .rank-toggle {{ display: flex; align-items: center; gap: 8px; color: #475569; font-size: 11px; cursor: pointer; margin-bottom: 16px; }}
  canvas {{ width: 100% !important; }}
  .footer {{ text-align: center; color: #1e2130; font-size: 10px; letter-spacing: 3px; margin-top: 16px; }}
</style>
</head>
<body>
<h1>⚔️ CIV 5 ELO TRACKER</h1>
<p class="subtitle">RANKED LADDER · LIVE DATA</p>

<div class="player-grid" id="playerGrid"></div>

<label class="rank-toggle">
  <input type="checkbox" id="rankToggle" checked onchange="toggleRanks()"> SHOW RANK LINES
</label>

<div class="card">
  <canvas id="eloChart"></canvas>
</div>

<p class="footer">CIV 5 RANKED · CLICK PLAYERS TO TOGGLE · AUTO-UPDATES ON REFRESH</p>

<script>
const TIMELINE = {timeline_json};
const PLAYERS = {players_json};
const PALETTE = ["#f97316","#3b82f6","#a855f7","#22c55e","#ef4444","#eab308","#06b6d4","#ec4899"];
const RANKS = [
  {{y:1800, label:"Deity",    color:"rgba(255,215,0,0.2)"}},
  {{y:1600, label:"Emperor",  color:"rgba(192,132,252,0.2)"}},
  {{y:1400, label:"King",     color:"rgba(96,165,250,0.2)"}},
  {{y:1200, label:"Prince",   color:"rgba(74,222,128,0.2)"}},
  {{y:1000, label:"Chieftain",color:"rgba(148,163,184,0.15)"}},
];

function rankLabel(elo) {{
  if (elo>=1800) return "🏆 Deity";
  if (elo>=1600) return "⚔️ Emperor";
  if (elo>=1400) return "🛡️ King";
  if (elo>=1200) return "⚙️ Prince";
  if (elo>=1000) return "🌿 Chieftain";
  return "🪨 Settler";
}}

const hidden = new Set();
const labels = TIMELINE.map(t => t.label);

const datasets = PLAYERS.map((p, i) => ({{
  label: p.name,
  data: TIMELINE.map(t => t[p.id] ?? null),
  borderColor: PALETTE[i % PALETTE.length],
  backgroundColor: PALETTE[i % PALETTE.length],
  borderWidth: 2.5,
  pointRadius: 4,
  pointHoverRadius: 7,
  tension: 0.3,
}}));

const rankAnnotations = {{}};
RANKS.forEach((r, i) => {{
  rankAnnotations[`rank${{i}}`] = {{
    type: "line", yMin: r.y, yMax: r.y,
    borderColor: r.color, borderWidth: 1, borderDash: [5,5],
    label: {{ content: r.label, display: true, color: r.color, font: {{size:9, family:"IBM Plex Mono"}}, position:"end" }}
  }};
}});

const ctx = document.getElementById("eloChart").getContext("2d");
const chart = new Chart(ctx, {{
  type: "line",
  data: {{ labels, datasets }},
  options: {{
    responsive: true,
    interaction: {{ mode: "index", intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: "#0f1117",
        borderColor: "#2a2d3a",
        borderWidth: 1,
        titleColor: "#64748b",
        bodyColor: "#e2e8f0",
        titleFont: {{family:"IBM Plex Mono", size:11}},
        bodyFont: {{family:"IBM Plex Mono", size:12}},
        callbacks: {{
          label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y}} Elo (${{rankLabel(ctx.parsed.y)}})`
        }}
      }}
    }},
    scales: {{
      x: {{ ticks: {{ color:"#475569", font:{{family:"IBM Plex Mono",size:10}} }}, grid: {{ color:"#1a1f2e" }} }},
      y: {{ ticks: {{ color:"#475569", font:{{family:"IBM Plex Mono",size:10}} }}, grid: {{ color:"#1a1f2e" }}, min: 800 }}
    }}
  }}
}});

// Render player buttons
const grid = document.getElementById("playerGrid");
PLAYERS.forEach((p, i) => {{
  const color = PALETTE[i % PALETTE.length];
  const medals = ["🥇","🥈","🥉"];
  const btn = document.createElement("button");
  btn.className = "player-btn";
  btn.style.borderColor = color;
  btn.style.color = color;
  btn.innerHTML = `<div class="player-name">${{medals[i]||"#"+(i+1)}} ${{p.name}}</div><div class="player-elo">${{p.finalElo}} Elo · ${{rankLabel(p.finalElo)}}</div>`;
  btn.onclick = () => {{
    const ds = chart.data.datasets[i];
    if (hidden.has(p.id)) {{
      hidden.delete(p.id);
      ds.borderWidth = 2.5;
      ds.pointRadius = 4;
      btn.classList.remove("hidden");
    }} else {{
      hidden.add(p.id);
      ds.borderWidth = 0;
      ds.pointRadius = 0;
      btn.classList.add("hidden");
    }}
    chart.update();
  }};
  grid.appendChild(btn);
}});

function toggleRanks() {{
  const show = document.getElementById("rankToggle").checked;
  RANKS.forEach((r, i) => {{
    const ds = chart.data.datasets;
    // toggle via y-axis min trick
  }});
  // Simple approach: add/remove rank reference lines by rebuilding
  chart.options.scales.y.min = show ? 800 : undefined;
  chart.update();
}}
</script>
</body>
</html>"""

async def handle_graph(request):
    html = build_graph_html()
    return web.Response(text=html, content_type="text/html")

async def handle_data(request):
    data = load_data()
    return web.Response(text=json.dumps(data), content_type="application/json")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/graph", handle_graph)
    app.router.add_get("/data", handle_data)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐  Web server running on port {port}")

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅  Logged in as {bot.user} — slash commands synced.")

# ── /open_lobby ───────────────────────────────────────────────────────────────
@bot.tree.command(name="open_lobby", description="Open a ranked game lobby")
@app_commands.describe(civ="The civilization you are playing")
async def open_lobby(interaction: discord.Interaction, civ: str):
    if civ not in ALL_CIVS:
        await interaction.response.send_message(f"❌ Unknown civ `{civ}`. Use `/civs` to see the full list.", ephemeral=True)
        return

    data = load_data()
    host_id = str(interaction.user.id)

    if host_id in data["lobbies"]:
        await interaction.response.send_message("⚠️ You already have an open lobby! Use `/cancel_game` to close it first.", ephemeral=True)
        return
    if player_in_any_lobby(data, host_id):
        await interaction.response.send_message("⚠️ You're already in an open lobby. Use `/leave_lobby` first.", ephemeral=True)
        return
    if player_in_active_game(data, host_id):
        await interaction.response.send_message("⚠️ You're already in an active game. Use `/cancel_game` to cancel it first.", ephemeral=True)
        return

    data["lobbies"][host_id] = {
        "host": host_id,
        "host_name": interaction.user.display_name,
        "players": [host_id],
        "player_names": [interaction.user.display_name],
        "player_civs": [civ],
        "created_at": datetime.utcnow().isoformat()
    }
    save_data(data)

    embed = build_lobby_embed(data["lobbies"][host_id])
    await interaction.response.send_message(embed=embed)

# ── /join_lobby ───────────────────────────────────────────────────────────────
@bot.tree.command(name="join_lobby", description="Join an open ranked lobby")
@app_commands.describe(host="The player who opened the lobby", civ="The civilization you are playing")
async def join_lobby(interaction: discord.Interaction, host: discord.Member, civ: str):
    if civ not in ALL_CIVS:
        await interaction.response.send_message(f"❌ Unknown civ `{civ}`. Use `/civs` to see the full list.", ephemeral=True)
        return

    data = load_data()
    host_id = str(host.id)
    joiner_id = str(interaction.user.id)

    if host_id not in data["lobbies"]:
        await interaction.response.send_message(f"❌ {host.display_name} doesn't have an open lobby.", ephemeral=True)
        return

    lobby = data["lobbies"][host_id]

    if joiner_id in lobby["players"]:
        await interaction.response.send_message("⚠️ You're already in this lobby!", ephemeral=True)
        return
    if player_in_any_lobby(data, joiner_id):
        await interaction.response.send_message("⚠️ You're already in another lobby. Use `/leave_lobby` first.", ephemeral=True)
        return
    if player_in_active_game(data, joiner_id):
        await interaction.response.send_message("⚠️ You're already in an active game. Use `/cancel_game` to cancel it first.", ephemeral=True)
        return
    if len(lobby["players"]) >= MAX_LOBBY_SIZE:
        await interaction.response.send_message(f"❌ Lobby is full ({MAX_LOBBY_SIZE} players max).", ephemeral=True)
        return
    if civ in lobby["player_civs"]:
        await interaction.response.send_message(f"❌ **{civ}** is already taken. Pick a different civ!", ephemeral=True)
        return

    lobby["players"].append(joiner_id)
    lobby["player_names"].append(interaction.user.display_name)
    lobby["player_civs"].append(civ)
    save_data(data)

    embed = build_lobby_embed(lobby)
    await interaction.response.send_message(embed=embed)

# ── /leave_lobby ──────────────────────────────────────────────────────────────
@bot.tree.command(name="leave_lobby", description="Leave a lobby you have joined")
async def leave_lobby(interaction: discord.Interaction):
    data = load_data()
    leaver_id = str(interaction.user.id)
    found_lobby_id = None

    for lid, lobby in data["lobbies"].items():
        if leaver_id in lobby["players"]:
            found_lobby_id = lid
            break

    if not found_lobby_id:
        await interaction.response.send_message("❌ You're not in any open lobby.", ephemeral=True)
        return

    lobby = data["lobbies"][found_lobby_id]

    # If host leaves, close the whole lobby
    if leaver_id == found_lobby_id:
        del data["lobbies"][found_lobby_id]
        save_data(data)
        embed = discord.Embed(
            title="🚫  Lobby Closed",
            description=f"{interaction.user.mention} (host) left — lobby closed. No Elo changes.",
            color=0x888888
        )
        await interaction.response.send_message(embed=embed)
        return

    idx = lobby["players"].index(leaver_id)
    lobby["players"].pop(idx)
    lobby["player_names"].pop(idx)
    lobby["player_civs"].pop(idx)
    save_data(data)

    embed = build_lobby_embed(lobby)
    embed.description = f"{interaction.user.mention} left the lobby."
    await interaction.response.send_message(embed=embed)

# ── /start_game ───────────────────────────────────────────────────────────────
@bot.tree.command(name="start_game", description="Start your lobby — locks it in and begins the ranked game")
async def start_game(interaction: discord.Interaction):
    data = load_data()
    host_id = str(interaction.user.id)

    if host_id not in data["lobbies"]:
        await interaction.response.send_message("❌ You don't have an open lobby. Use `/open_lobby` first.", ephemeral=True)
        return

    lobby = data["lobbies"][host_id]

    if len(lobby["players"]) < 2:
        await interaction.response.send_message("❌ You need at least 2 players to start.", ephemeral=True)
        return

    # Save civ map and game group so /report_results can look them up
    game_id = host_id
    for pid, civ in zip(lobby["players"], lobby["player_civs"]):
        data["active_games"][pid] = {"civ": civ, "game_id": game_id}
    data["game_groups"][game_id] = {
        "players": lobby["players"],
        "player_names": lobby["player_names"],
        "player_civs": lobby["player_civs"],
    }

    player_lines = "\n".join(
        f"• <@{pid}> — **{civ}**"
        for pid, civ in zip(lobby["players"], lobby["player_civs"])
    )

    del data["lobbies"][host_id]
    save_data(data)

    embed = discord.Embed(
        title=f"⚔️  {len(lobby['players'])}-Player Ranked Game Started!",
        description=f"{player_lines}\n\n"
                    f"When the game ends, the host reports finishing order with:\n"
                    f"`/report_results @1st @2nd @3rd ...`",
        color=0xD4A017
    )
    embed.set_footer(text=f"Started by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

# ── /cancel_game ─────────────────────────────────────────────────────────────
@bot.tree.command(name="cancel_game", description="Cancel your open lobby or active game — no Elo changes")
async def cancel_game(interaction: discord.Interaction):
    data = load_data()
    caller_id = str(interaction.user.id)

    # Case 1: They have an open lobby (as host)
    if caller_id in data["lobbies"]:
        lobby = data["lobbies"][caller_id]
        player_names = lobby["player_names"]
        del data["lobbies"][caller_id]
        save_data(data)
        embed = discord.Embed(
            title="🚫  Lobby Cancelled",
            description=f"{interaction.user.mention} cancelled the lobby.\n"
                        f"Players: {', '.join(player_names)}\n\n"
                        "No Elo changes have been made.",
            color=0x888888
        )
        await interaction.response.send_message(embed=embed)
        return

    # Case 2: They're in a started game
    if caller_id in data["active_games"]:
        game_entry = data["active_games"][caller_id]
        game_id = game_entry.get("game_id") if isinstance(game_entry, dict) else None
        if game_id and game_id in data["game_groups"]:
            group = data["game_groups"][game_id]
            for pid in group["players"]:
                data["active_games"].pop(pid, None)
            del data["game_groups"][game_id]
            save_data(data)
            names_str = ", ".join(group["player_names"])
            embed = discord.Embed(
                title="🚫  Game Cancelled",
                description=f"{interaction.user.mention} cancelled the in-progress game.\n"
                            f"Players: {names_str}\n\nNo Elo changes have been made.",
                color=0x888888
            )
            await interaction.response.send_message(embed=embed)
            return

    # Case 3: They're in someone else's lobby (non-host)
    for lid, lobby in data["lobbies"].items():
        if caller_id in lobby["players"]:
            await interaction.response.send_message(
                "❌ Only the host can cancel a lobby. Use `/leave_lobby` to leave instead.",
                ephemeral=True)
            return

    await interaction.response.send_message("❌ You're not in any active lobby or game to cancel.", ephemeral=True)

# ── /report_results ───────────────────────────────────────────────────────────
@bot.tree.command(
    name="report_results",
    description="Report finishing positions for a ranked game (host only)"
)
@app_commands.describe(
    first="Player who finished 1st",
    second="Player who finished 2nd",
    third="3rd place (optional)",
    fourth="4th place (optional)",
    fifth="5th place (optional)",
    sixth="6th place (optional)",
    seventh="7th place (optional)",
    eighth="8th place (optional)",
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
        await interaction.response.send_message("❌ Duplicate players detected.", ephemeral=True)
        return
    if any(m.bot for m in members):
        await interaction.response.send_message("❌ Bots can't be ranked players.", ephemeral=True)
        return

    data = load_data()
    active_games = data.get("active_games", {})
    game_groups = data.get("game_groups", {})
    caller_id = str(interaction.user.id)

    # Must be the host of an active game
    caller_entry = active_games.get(caller_id)
    if not caller_entry:
        await interaction.response.send_message("❌ You don't have an active game to report.", ephemeral=True)
        return
    game_id = caller_entry.get("game_id") if isinstance(caller_entry, dict) else None
    if game_id != caller_id:
        await interaction.response.send_message("❌ Only the host can report results.", ephemeral=True)
        return

    # Verify reported players match the actual game group
    group = game_groups.get(game_id, {})
    expected_ids = set(group.get("players", []))
    reported_ids = set(str(m.id) for m in members)
    if expected_ids != reported_ids:
        missing = expected_ids - reported_ids
        extra = reported_ids - expected_ids
        msg = "❌ Reported players don't match the game lobby.\n"
        if missing:
            msg += f"Missing: {', '.join(f'<@{p}>' for p in missing)}\n"
        if extra:
            msg += f"Not in game: {', '.join(f'<@{p}>' for p in extra)}"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    player_info = []
    for i, member in enumerate(members):
        p = get_player(data, str(member.id))
        entry = active_games.get(str(member.id), {})
        civ = entry.get("civ") if isinstance(entry, dict) else entry
        player_info.append({
            "id": str(member.id),
            "member": member,
            "finish": i + 1,
            "elo": p["elo"],
            "old_elo": p["elo"],
            "civ": civ
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

        civ = info["civ"]
        if civ:
            p["civs"].setdefault(civ, {"wins": 0, "losses": 0})
            if i == 0:
                p["civs"][civ]["wins"] += 1
            else:
                p["civs"][civ]["losses"] += 1

        active_games.pop(info["id"], None)

        civ_tag = f" ({civ})" if civ else ""
        result_lines.append(
            f"{medals[i]} **{info['member'].display_name}**{civ_tag} — "
            f"{old_elo} → **{new_elo}** ({sign}{diff})  {rank_label(new_elo)}"
        )

    # Clean up game group
    game_groups.pop(game_id, None)
    data["active_games"] = active_games
    data["game_groups"] = game_groups
    data["matches"].append({
        "type": f"{len(members)}-player",
        "players": [
            {
                "id": info["id"],
                "finish": info["finish"],
                "civ": info["civ"],
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
        except discord.NotFound:
            name = f"<@{uid}>"
        except discord.HTTPException:
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
        def civ_score(item):
            v = item[1]
            games = v["wins"] + v["losses"]
            return v["wins"] / games if games > 0 else 0

        top_civs = sorted(civs.items(), key=civ_score, reverse=True)[:5]
        civ_text = "\n".join(
            f"**{c}** — {v['wins']}W / {v['losses']}L "
            f"({round(v['wins'] / (v['wins'] + v['losses']) * 100)}% WR)"
            for c, v in top_civs
        )
        embed.add_field(name="🗺️  Top 5 Civs by Win Rate", value=civ_text, inline=False)

    await interaction.response.send_message(embed=embed)

# ── /civs ─────────────────────────────────────────────────────────────────────
@bot.tree.command(name="civs", description="List all valid civilization names")
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


# ── /graph ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="graph", description="Show the Elo progression graph for all ranked players")
async def graph(interaction: discord.Interaction):
    data = load_data()
    active_ids = set()
    for m in data.get("matches", []):
        for p in m.get("players", []):
            active_ids.add(p["id"])

    if not active_ids:
        await interaction.response.send_message("No matches played yet — nothing to graph!", ephemeral=True)
        return

    if not PUBLIC_URL:
        await interaction.response.send_message(
            "⚠️ `PUBLIC_URL` is not set in Railway environment variables. "
            "Add it in the Variables tab (e.g. `https://your-app.up.railway.app`) then redeploy.",
            ephemeral=True
        )
        return

    url = f"{PUBLIC_URL}/graph"
    count = len(active_ids)
    embed = discord.Embed(
        title="📈  Civ 5 Elo Graph",
        description=f"Live Elo progression for all **{count} ranked players**.\n\n[**Open Graph →**]({url})",
        color=0xf97316,
        url=url
    )
    embed.set_footer(text="Updates automatically on each page refresh")
    await interaction.response.send_message(embed=embed)

# ── Run ───────────────────────────────────────────────────────────────────────
async def main():
    if not TOKEN:
        raise RuntimeError("Set the DISCORD_TOKEN environment variable!")
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
