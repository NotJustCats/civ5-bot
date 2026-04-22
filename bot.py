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
K_FACTOR = 48
MAX_LOBBY_SIZE = 8
FLOOR_ELO = 100
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

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


COASTAL_CIVS = {
    "Australia", "Brunei", "Carthage", "Chile", "Denmark", "England",
    "Indonesia", "Japan", "Kilwa", "Korea", "Netherlands", "New Zealand",
    "Norway", "Oman", "Philippines", "Phoenicia", "Polynesia", "Portugal",
    "Spain", "Tonga", "Tunisia", "UAE", "Venice"
}
LAND_CIVS = [c for c in ALL_CIVS if c not in COASTAL_CIVS]
DRAFT_SIZE = 5  # civs offered to each player in the draft
# ── Data helpers ─────────────────────────────────────────────────────────────
def load_all_data() -> dict:
    """Load the full data file. Top level is keyed by guild_id (server ID)."""
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        print("⚠️  WARNING: ranked_data.json is corrupted. Starting fresh.")
        return {}

def save_all_data(all_data: dict):
    """Atomic save."""
    dir_name = os.path.dirname(os.path.abspath(DATA_FILE))
    try:
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp") as tmp:
            json.dump(all_data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, DATA_FILE)
    except IOError as e:
        print(f"❌ Failed to save data: {e}")

def get_server_data(all_data: dict, guild_id: str) -> dict:
    """Get or create the data block for a specific server."""
    default = {"players": {}, "lobbies": {}, "active_games": {}, "game_groups": {}, "matches": []}
    if guild_id not in all_data:
        all_data[guild_id] = default
    else:
        for key, val in default.items():
            all_data[guild_id].setdefault(key, val)
    return all_data[guild_id]

def get_player(data: dict, user_id: str, display_name: str = None) -> dict:
    if user_id not in data["players"]:
        data["players"][user_id] = {
            "elo": STARTING_ELO,
            "wins": 0,
            "losses": 0,
            "civs": {},
            "name": display_name or user_id
        }
    elif display_name:
        data["players"][user_id]["name"] = display_name
    return data["players"][user_id]

def player_in_active_game(data: dict, user_id: str) -> bool:
    return user_id in data.get("active_games", {})

def player_in_any_lobby(data: dict, user_id: str) -> bool:
    for lobby in data.get("lobbies", {}).values():
        if user_id in lobby["players"]:
            return True
    return False


def build_draft(players: list, map_type: str) -> dict:
    """
    Build a civ draft for all players.
    map_type: "coastal", "land", or "any"
    Each player gets DRAFT_SIZE unique civs.
    For coastal: distribute coastal civs evenly first, fill remainder with land civs.
    Returns {player_id: [civ1, civ2, ...]}
    """
    import random as _random
    n = len(players)
    total_needed = n * DRAFT_SIZE

    coastal_list = list(COASTAL_CIVS & set(ALL_CIVS))
    land_list = list(LAND_CIVS)
    _random.shuffle(coastal_list)
    _random.shuffle(land_list)

    if map_type == "any":
        pool = ALL_CIVS[:]
        _random.shuffle(pool)
        pool = pool[:total_needed]
    elif map_type == "coastal":
        # Distribute coastal evenly — at least one per player if possible
        coastal_per_player = min(len(coastal_list) // n, DRAFT_SIZE)
        coastal_needed = coastal_per_player * n
        coastal_used = coastal_list[:coastal_needed]
        land_needed = total_needed - coastal_needed
        land_used = land_list[:land_needed]
        pool = coastal_used + land_used
        _random.shuffle(pool)
    else:  # land
        pool = land_list[:total_needed]
        _random.shuffle(pool)

    # Assign DRAFT_SIZE civs to each player
    draft = {}
    for i, pid in enumerate(players):
        draft[pid] = pool[i * DRAFT_SIZE:(i + 1) * DRAFT_SIZE]
    return draft

def calc_multiplayer_elo(players: list) -> list:
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
    if elo >= 1600: return "🏆 Deity"
    if elo >= 1400: return "⚔️  Emperor"
    if elo >= 1250: return "🛡️  King"
    if elo >= 1100: return "⚙️  Prince"
    if elo >= 1000: return "🌿 Chieftain"
    return              "🪨 Settler"

def build_lobby_embed(lobby: dict) -> discord.Embed:
    lines = [f"• **{name}**" for name in lobby["player_names"]]
    embed = discord.Embed(title="🏛️  Game Lobby — Open", color=0x4CAF50)
    embed.add_field(name=f"Host: {lobby['host_name']} · {len(lobby['players'])} player(s)",
                    value="\n".join(lines) or "—", inline=False)
    embed.set_footer(text="Use /join_lobby @host to join • Host uses /start_game [land/coastal/any] to begin")
    return embed

def guild_id_from(interaction: discord.Interaction) -> str:
    return str(interaction.guild_id)

# ── Web server ────────────────────────────────────────────────────────────────
def build_graph_html(guild_id: str) -> str:
    all_data = load_all_data()
    data = all_data.get(guild_id, {"players": {}, "matches": []})
    matches = data.get("matches", [])
    players = data.get("players", {})

    active_ids = set()
    for m in matches:
        for p in m.get("players", []):
            active_ids.add(p["id"])

    # Build Elo timeline
    current_elo = {pid: 1000 for pid in active_ids}
    timeline = [{"label": "Start", **{pid: 1000 for pid in active_ids}}]
    sorted_matches = sorted(matches, key=lambda m: m.get("played_at", ""))
    game_num = 0
    for match in sorted_matches:
        date_str = match.get("played_at", "")[:10]
        if match.get("type") == "reset":
            for pid in active_ids:
                current_elo[pid] = 1000
            timeline.append({"label": f"RESET ({date_str})", **{pid: current_elo[pid] for pid in active_ids}})
            game_num = 0
            continue
        game_num += 1
        for p in match.get("players", []):
            if p["id"] in active_ids:
                current_elo[p["id"]] = p["elo_after"]
        timeline.append({"label": f"G{game_num} ({date_str})", **{pid: current_elo[pid] for pid in active_ids}})

    player_list = [
        {
            "id": pid,
            "name": players.get(pid, {}).get("name", f"Player {i+1}"),
            "finalElo": players.get(pid, {}).get("elo", 1000)
        }
        for i, pid in enumerate(sorted(active_ids, key=lambda x: players.get(x, {}).get("elo", 0), reverse=True))
    ]

    # Build civ play counts for pie chart (top 12)
    civ_counts = {}
    for m in sorted_matches:
        if m.get("type") in ("reset", None):
            continue
        for p in m.get("players", []):
            civ = p.get("civ")
            if civ:
                civ_counts[civ] = civ_counts.get(civ, 0) + 1
    top_civs = sorted(civ_counts.items(), key=lambda x: x[1], reverse=True)[:12]
    pie_labels = [c for c, _ in top_civs]
    pie_values = [v for _, v in top_civs]

    # Build leaderboard data (wins/losses per player)
    lb_data = {
        pid: {
            "wins": players.get(pid, {}).get("wins", 0),
            "losses": players.get(pid, {}).get("losses", 0),
        }
        for pid in active_ids
    }

    import json as _json
    timeline_json = _json.dumps(timeline)
    players_json = _json.dumps(player_list)
    pie_labels_json = _json.dumps(pie_labels)
    pie_values_json = _json.dumps(pie_values)
    lb_data_json = _json.dumps(lb_data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Civ 5 Ranked — Stats</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ height: 100%; }}
  body {{ background: #080a0f; color: #e2e8f0; font-family: 'IBM Plex Mono', monospace; height: 100vh; display: flex; flex-direction: column; padding: 16px; gap: 10px; overflow: hidden; }}
  .topbar {{ display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }}
  h1 {{ font-family: 'Cinzel', serif; font-size: 20px; color: #f97316; letter-spacing: 3px; text-shadow: 0 0 30px rgba(249,115,22,0.4); }}
  .subtitle {{ color: #475569; font-size: 10px; letter-spacing: 2px; }}
  .player-grid {{ display: flex; gap: 8px; flex-wrap: wrap; flex-shrink: 0; }}
  .player-btn {{ border-radius: 8px; padding: 6px 12px; cursor: pointer; border-width: 1px; border-style: solid; background: transparent; font-family: 'IBM Plex Mono', monospace; transition: opacity 0.15s; text-align: left; }}
  .player-btn.hidden {{ opacity: 0.3; }}
  .player-name {{ font-weight: 600; font-size: 12px; }}
  .player-elo {{ font-size: 10px; color: #64748b; margin-top: 2px; }}
  .rank-toggle {{ display: flex; align-items: center; gap: 6px; color: #475569; font-size: 10px; cursor: pointer; flex-shrink: 0; }}
  .main-grid {{ display: grid; grid-template-columns: 1.4fr 1fr; grid-template-rows: 1fr 1fr; gap: 10px; flex: 1; min-height: 0; max-height: calc(100vh - 120px); }}
  .card {{ background: #0d1017; border: 1px solid #1e2130; border-radius: 12px; padding: 14px; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }}
  .card-elo {{ grid-row: 1 / 3; }}
  .card-title {{ color: #94a3b8; font-size: 10px; letter-spacing: 2px; margin-bottom: 10px; flex-shrink: 0; }}
  .chart-wrap {{ flex: 1; min-height: 0; max-height: 100%; position: relative; overflow: hidden; }}
  .chart-wrap canvas {{ position: absolute; inset: 0; width: 100% !important; height: 100% !important; }}
  .lb-list {{ flex: 1; overflow-y: auto; min-height: 0; }}
  .lb-row {{ display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid #1a1f2e; }}
  .empty {{ text-align: center; color: #334155; padding: 40px; font-size: 12px; }}
  @media (max-width: 800px) {{
    body {{ overflow: auto; height: auto; }}
    .main-grid {{ grid-template-columns: 1fr; grid-template-rows: auto; }}
    .card-elo {{ grid-row: auto; }}
    .chart-wrap {{ height: 260px; position: relative; }}
  }}
</style>
</head>
<body>
<div class="topbar">
  <div>
    <h1>CIV 5 RANKED STATS</h1>
    <p class="subtitle">LIVE DATA · REFRESH FOR LATEST</p>
  </div>
  <label class="rank-toggle"><input type="checkbox" id="rankToggle" checked onchange="toggleRanks()"> RANK LINES</label>
</div>

<div class="player-grid" id="playerGrid"></div>

<div class="main-grid">
  <div class="card card-elo" id="eloCard">
    <p class="card-title">ELO PROGRESSION OVER TIME</p>
    <div class="chart-wrap"><canvas id="eloChart"></canvas></div>
  </div>
  <div class="card" id="pieCard">
    <p class="card-title">MOST PLAYED CIVILIZATIONS</p>
    <div class="chart-wrap"><canvas id="pieChart"></canvas></div>
  </div>
  <div class="card" id="lbCard">
    <p class="card-title">LEADERBOARD</p>
    <div class="lb-list" id="lbList"></div>
  </div>
</div>

<script>
const TIMELINE = {timeline_json};
const PLAYERS = {players_json};
const PIE_LABELS = {pie_labels_json};
const PIE_VALUES = {pie_values_json};
const LB_DATA = {lb_data_json};
const PALETTE = ["#f97316","#3b82f6","#a855f7","#22c55e","#ef4444","#eab308","#06b6d4","#ec4899","#f43f5e","#10b981","#8b5cf6","#0ea5e9"];

function rankLabel(elo) {{
  if (elo>=1600) return "🏆 Deity";
  if (elo>=1400) return "⚔️ Emperor";
  if (elo>=1250) return "🛡️ King";
  if (elo>=1100) return "⚙️ Prince";
  if (elo>=1000) return "🌿 Chieftain";
  return "🪨 Settler";
}}

// ── Elo line chart ────────────────────────────────────────────────────────────
if (!PLAYERS.length) {{
  document.getElementById("eloCard").innerHTML = '<p class="empty">No matches played yet in this server.</p>';
  document.getElementById("pieCard").innerHTML = '<p class="empty">No civ data yet.</p>';
}} else {{
  const hidden = new Set();
  const labels = TIMELINE.map(t => t.label);
  const datasets = PLAYERS.map((p, i) => ({{
    label: p.name,
    data: TIMELINE.map(t => t[p.id] ?? null),
    borderColor: PALETTE[i % PALETTE.length],
    backgroundColor: PALETTE[i % PALETTE.length],
    borderWidth: 2.5, pointRadius: 4, pointHoverRadius: 7, tension: 0.3,
  }}));

  const eloCtx = document.getElementById("eloChart").getContext("2d");
  const eloChart = new Chart(eloCtx, {{
    type: "line",
    data: {{ labels, datasets }},
    options: {{
      responsive: true,
      interaction: {{ mode: "index", intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          backgroundColor: "#0f1117", borderColor: "#2a2d3a", borderWidth: 1,
          titleColor: "#64748b", bodyColor: "#e2e8f0",
          titleFont: {{family:"IBM Plex Mono",size:11}},
          bodyFont: {{family:"IBM Plex Mono",size:12}},
          callbacks: {{ label: ctx => ` ${{ctx.dataset.label}}: ${{ctx.parsed.y}} Elo (${{rankLabel(ctx.parsed.y)}})` }}
        }}
      }},
      scales: {{
        x: {{ ticks: {{ color:"#475569", font:{{family:"IBM Plex Mono",size:10}} }}, grid: {{ color:"#1a1f2e" }} }},
        y: {{ ticks: {{ color:"#475569", font:{{family:"IBM Plex Mono",size:10}} }}, grid: {{ color:"#1a1f2e" }}, min: 800 }}
      }}
    }}
  }});

  // Player toggle buttons
  const grid = document.getElementById("playerGrid");
  PLAYERS.forEach((p, i) => {{
    const color = PALETTE[i % PALETTE.length];
    const medals = ["🥇","🥈","🥉"];
    const btn = document.createElement("button");
    btn.className = "player-btn";
    btn.style.borderColor = color;
    btn.style.color = color;
    btn.innerHTML = `<div class="player-name">${{medals[i] || "#"+(i+1)}} ${{p.name}}</div><div class="player-elo">${{p.finalElo}} Elo · ${{rankLabel(p.finalElo)}}</div>`;
    btn.onclick = () => {{
      const ds = eloChart.data.datasets[i];
      if (hidden.has(p.id)) {{
        hidden.delete(p.id); ds.borderWidth = 2.5; ds.pointRadius = 4; btn.classList.remove("hidden");
      }} else {{
        hidden.add(p.id); ds.borderWidth = 0; ds.pointRadius = 0; btn.classList.add("hidden");
      }}
      eloChart.update();
    }};
    grid.appendChild(btn);
  }});

  function toggleRanks() {{
    eloChart.options.scales.y.min = document.getElementById("rankToggle").checked ? 800 : undefined;
    eloChart.update();
  }}

  // ── Pie chart ───────────────────────────────────────────────────────────────
  if (PIE_LABELS.length) {{
    const pieCtx = document.getElementById("pieChart").getContext("2d");
    new Chart(pieCtx, {{
      type: "doughnut",
      data: {{
        labels: PIE_LABELS,
        datasets: [{{
          data: PIE_VALUES,
          backgroundColor: PALETTE.concat(PALETTE),
          borderColor: "#080a0f",
          borderWidth: 2,
          hoverOffset: 8,
        }}]
      }},
      options: {{
        responsive: true,
        plugins: {{
          legend: {{
            position: "right",
            labels: {{
              color: "#94a3b8",
              font: {{family:"IBM Plex Mono", size:10}},
              boxWidth: 12,
              padding: 10,
            }}
          }},
          tooltip: {{
            backgroundColor: "#0f1117", borderColor: "#2a2d3a", borderWidth: 1,
            titleColor: "#64748b", bodyColor: "#e2e8f0",
            titleFont: {{family:"IBM Plex Mono",size:11}},
            bodyFont: {{family:"IBM Plex Mono",size:12}},
            callbacks: {{
              label: ctx => ` ${{ctx.label}}: ${{ctx.parsed}} game${{ctx.parsed !== 1 ? "s" : ""}}`
            }}
          }}
        }}
      }}
    }});
  }} else {{
    document.getElementById("pieCard").innerHTML = '<p class="empty">No civ data yet.</p>';
  }}

  // ── Leaderboard ─────────────────────────────────────────────────────────────
  const lbList = document.getElementById("lbList");
  const medals = ["🥇","🥈","🥉"];
  PLAYERS.forEach((p, i) => {{
    const color = PALETTE[i % PALETTE.length];
    const wins = LB_DATA[p.id]?.wins || 0;
    const losses = LB_DATA[p.id]?.losses || 0;
    const total = wins + losses;
    const wr = total > 0 ? Math.round(wins / total * 100) : 0;
    const row = document.createElement("div");
    row.className = "lb-row";
    row.innerHTML = `
      <span style="font-size:18px;width:28px;text-align:center">${{medals[i] || "#"+(i+1)}}</span>
      <div style="flex:1">
        <div style="font-weight:600;font-size:13px;color:${{color}}">${{p.name}}</div>
        <div style="font-size:10px;color:#475569;margin-top:3px">${{wins}}W / ${{losses}}L · ${{wr}}% WR</div>
      </div>
      <div style="text-align:right">
        <div style="font-weight:700;font-size:14px;color:#e2e8f0">${{p.finalElo}}</div>
        <div style="font-size:10px;color:#475569;margin-top:2px">${{rankLabel(p.finalElo)}}</div>
      </div>
    `;
    lbList.appendChild(row);
  }});
}}
</script>
</body>
</html>"""

async def handle_graph(request):
    guild_id = request.query.get("guild")
    if not guild_id:
        return web.Response(text="<h2>Missing ?guild= parameter</h2>", content_type="text/html", status=400)
    html = build_graph_html(guild_id)
    return web.Response(text=html, content_type="text/html")

async def handle_data(request):
    all_data = load_all_data()
    return web.Response(text=json.dumps(all_data), content_type="application/json")

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
async def open_lobby(interaction: discord.Interaction):
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
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

    get_player(data, host_id, interaction.user.display_name)
    data["lobbies"][host_id] = {
        "host": host_id,
        "host_name": interaction.user.display_name,
        "players": [host_id],
        "player_names": [interaction.user.display_name],
        "created_at": datetime.utcnow().isoformat()
    }
    save_all_data(all_data)

    embed = build_lobby_embed(data["lobbies"][host_id])
    await interaction.response.send_message(embed=embed)

# ── /join_lobby ───────────────────────────────────────────────────────────────
@bot.tree.command(name="join_lobby", description="Join an open ranked lobby")
@app_commands.describe(host="The player who opened the lobby")
async def join_lobby(interaction: discord.Interaction, host: discord.Member):
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
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

    get_player(data, joiner_id, interaction.user.display_name)
    lobby["players"].append(joiner_id)
    lobby["player_names"].append(interaction.user.display_name)
    save_all_data(all_data)

    embed = build_lobby_embed(lobby)
    await interaction.response.send_message(embed=embed)

# ── /leave_lobby ──────────────────────────────────────────────────────────────
@bot.tree.command(name="leave_lobby", description="Leave a lobby you have joined")
async def leave_lobby(interaction: discord.Interaction):
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
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

    if leaver_id == found_lobby_id:
        del data["lobbies"][found_lobby_id]
        save_all_data(all_data)
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
    save_all_data(all_data)

    embed = build_lobby_embed(lobby)
    embed.description = f"{interaction.user.mention} left the lobby."
    await interaction.response.send_message(embed=embed)

# ── /start_game ───────────────────────────────────────────────────────────────
@bot.tree.command(name="start_game", description="Start your lobby and draft civs")
@app_commands.describe(map_type="The type of map being played — affects which civs are drafted")
@app_commands.choices(map_type=[
    app_commands.Choice(name="Land — mostly land civs", value="land"),
    app_commands.Choice(name="Coastal — coastal civs spread evenly, filled with land", value="coastal"),
    app_commands.Choice(name="Any — all civs in the pool", value="any"),
    app_commands.Choice(name="Skip draft — pick civs manually later", value="skip"),
])
async def start_game(interaction: discord.Interaction, map_type: str = "any"):
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
    host_id = str(interaction.user.id)

    if host_id not in data["lobbies"]:
        await interaction.response.send_message("❌ You don't have an open lobby. Use `/open_lobby` first.", ephemeral=True)
        return

    lobby = data["lobbies"][host_id]

    if len(lobby["players"]) < 2:
        await interaction.response.send_message("❌ You need at least 2 players to start.", ephemeral=True)
        return

    game_id = host_id

    if map_type == "skip":
        # No draft — move straight to active game, civs will be None until reported
        for pid in lobby["players"]:
            data["active_games"][pid] = {"civ": None, "game_id": game_id}
        data["game_groups"][game_id] = {
            "players": lobby["players"],
            "player_names": lobby["player_names"],
            "player_civs": [None] * len(lobby["players"]),
            "draft": None,
            "picks": {},
        }
        del data["lobbies"][host_id]
        save_all_data(all_data)

        mentions = " ".join(f"<@{pid}>" for pid in lobby["players"])
        embed = discord.Embed(
            title=f"⚔️  {len(lobby['players'])}-Player Ranked Game Started!",
            description=f"Players: {mentions}\n\nDraft skipped — pick your civs in-game.\nWhen done, report finishing order with:\n`/report_results @1st @2nd @3rd ...`",
            color=0xD4A017
        )
        embed.set_footer(text=f"Started by {interaction.user.display_name}")
        await interaction.response.send_message(embed=embed)
        return

    # Build draft
    draft = build_draft(lobby["players"], map_type)

    for pid in lobby["players"]:
        data["active_games"][pid] = {"civ": None, "game_id": game_id}
    data["game_groups"][game_id] = {
        "players": lobby["players"],
        "player_names": lobby["player_names"],
        "player_civs": [None] * len(lobby["players"]),
        "draft": draft,
        "picks": {},  # pid -> chosen civ
    }

    del data["lobbies"][host_id]
    save_all_data(all_data)

    map_label = {"land": "Land", "coastal": "Coastal", "any": "Any"}.get(map_type, "Any")

    # Build draft display — one field per player
    embed = discord.Embed(
        title=f"⚔️  {len(lobby['players'])}-Player Game — Civ Draft ({map_label})",
        description="Each player use `/pick_civ [civ]` to choose from your options below.\nAll players must pick before the game begins.",
        color=0xD4A017
    )
    for pid, name in zip(lobby["players"], lobby["player_names"]):
        civs_list = " · ".join(f"`{c}`" for c in draft[pid])
        embed.add_field(name=f"{name}", value=civs_list, inline=False)

    embed.set_footer(text=f"Started by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


# ── /pick_civ ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="pick_civ", description="Pick your civilization from your draft options")
@app_commands.describe(civ="The civilization you want to play (must be in your draft)")
async def pick_civ(interaction: discord.Interaction, civ: str):
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
    caller_id = str(interaction.user.id)

    # Must be in an active game with a draft
    if caller_id not in data.get("active_games", {}):
        await interaction.response.send_message("❌ You're not in an active game.", ephemeral=True)
        return

    game_entry = data["active_games"][caller_id]
    game_id = game_entry.get("game_id") if isinstance(game_entry, dict) else None
    group = data.get("game_groups", {}).get(game_id)

    if not group or not group.get("draft"):
        await interaction.response.send_message("❌ This game has no draft — civs are being picked in-game.", ephemeral=True)
        return

    draft = group["draft"]
    picks = group.get("picks", {})

    # Check player is in this game
    if caller_id not in group["players"]:
        await interaction.response.send_message("❌ You're not in this game.", ephemeral=True)
        return

    # Already picked
    if caller_id in picks:
        await interaction.response.send_message(f"⚠️ You already picked **{picks[caller_id]}**.", ephemeral=True)
        return

    # Must pick from their own draft
    player_draft = draft.get(caller_id, [])
    if civ not in player_draft:
        options = " · ".join(f"`{c}`" for c in player_draft)
        await interaction.response.send_message(
            f"❌ **{civ}** is not in your draft. Your options are:\n{options}", ephemeral=True)
        return

    # Check civ not already picked by someone else
    if civ in picks.values():
        await interaction.response.send_message(f"❌ **{civ}** has already been picked by another player.", ephemeral=True)
        return

    # Record pick
    picks[caller_id] = civ
    group["picks"] = picks

    # Update active_games civ
    data["active_games"][caller_id]["civ"] = civ

    # Update player_civs in group
    idx = group["players"].index(caller_id)
    group["player_civs"][idx] = civ

    # Check if all players have picked
    all_picked = all(pid in picks for pid in group["players"])

    save_all_data(all_data)

    if all_picked:
        # All done — show final lineup
        lines = "\n".join(
            f"• **{group['player_names'][i]}** — **{group['player_civs'][i]}**"
            for i in range(len(group["players"]))
        )
        embed = discord.Embed(
            title="✅  All Civs Picked — Game On!",
            description=f"{lines}\n\nGo play! When done, the host reports with:\n`/report_results @1st @2nd @3rd ...`",
            color=0x4CAF50
        )
        await interaction.response.send_message(embed=embed)
    else:
        # Show who still needs to pick
        waiting = [
            group["player_names"][i]
            for i, pid in enumerate(group["players"])
            if pid not in picks
        ]
        caller_name = interaction.user.display_name
        embed = discord.Embed(
            title=f"✅  {caller_name} picked {civ}",
            description=f"Still waiting for: {', '.join(waiting)}",
            color=0x4CAF50
        )
        await interaction.response.send_message(embed=embed)

# ── /cancel_game ─────────────────────────────────────────────────────────────
@bot.tree.command(name="cancel_game", description="Cancel your open lobby or active game — no Elo changes")
async def cancel_game(interaction: discord.Interaction):
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
    caller_id = str(interaction.user.id)

    if caller_id in data["lobbies"]:
        lobby = data["lobbies"][caller_id]
        player_names = lobby["player_names"]
        del data["lobbies"][caller_id]
        save_all_data(all_data)
        embed = discord.Embed(
            title="🚫  Lobby Cancelled",
            description=f"{interaction.user.mention} cancelled the lobby.\nPlayers: {', '.join(player_names)}\n\nNo Elo changes have been made.",
            color=0x888888
        )
        await interaction.response.send_message(embed=embed)
        return

    if caller_id in data["active_games"]:
        game_entry = data["active_games"][caller_id]
        game_id = game_entry.get("game_id") if isinstance(game_entry, dict) else None
        if game_id and game_id in data["game_groups"]:
            group = data["game_groups"][game_id]
            for pid in group["players"]:
                data["active_games"].pop(pid, None)
            del data["game_groups"][game_id]
            save_all_data(all_data)
            names_str = ", ".join(group["player_names"])
            embed = discord.Embed(
                title="🚫  Game Cancelled",
                description=f"{interaction.user.mention} cancelled the in-progress game.\nPlayers: {names_str}\n\nNo Elo changes have been made.",
                color=0x888888
            )
            await interaction.response.send_message(embed=embed)
            return

    for lid, lobby in data["lobbies"].items():
        if caller_id in lobby["players"]:
            await interaction.response.send_message(
                "❌ Only the host can cancel a lobby. Use `/leave_lobby` to leave instead.",
                ephemeral=True)
            return

    await interaction.response.send_message("❌ You're not in any active lobby or game to cancel.", ephemeral=True)

# ── /report_results ───────────────────────────────────────────────────────────
@bot.tree.command(name="report_results", description="Report finishing positions for a ranked game (host only)")
@app_commands.describe(
    first="Player who finished 1st", second="Player who finished 2nd",
    third="3rd place (optional)", fourth="4th place (optional)",
    fifth="5th place (optional)", sixth="6th place (optional)",
    seventh="7th place (optional)", eighth="8th place (optional)",
)
async def report_results(
    interaction: discord.Interaction,
    first: discord.Member, second: discord.Member,
    third: Optional[discord.Member] = None, fourth: Optional[discord.Member] = None,
    fifth: Optional[discord.Member] = None, sixth: Optional[discord.Member] = None,
    seventh: Optional[discord.Member] = None, eighth: Optional[discord.Member] = None,
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

    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
    active_games = data.get("active_games", {})
    game_groups = data.get("game_groups", {})
    caller_id = str(interaction.user.id)

    caller_entry = active_games.get(caller_id)
    if not caller_entry:
        await interaction.response.send_message("❌ You don't have an active game to report.", ephemeral=True)
        return
    game_id = caller_entry.get("game_id") if isinstance(caller_entry, dict) else None
    if game_id != caller_id:
        await interaction.response.send_message("❌ Only the host can report results.", ephemeral=True)
        return

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
            "id": str(member.id), "member": member, "finish": i + 1,
            "elo": p["elo"], "old_elo": p["elo"], "civ": civ
        })

    new_elos = calc_multiplayer_elo(player_info)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
    result_lines = []

    for i, info in enumerate(player_info):
        p = get_player(data, info["id"])
        old_elo, new_elo = info["old_elo"], new_elos[i]
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

    game_groups.pop(game_id, None)
    data["active_games"] = active_games
    data["game_groups"] = game_groups
    data["matches"].append({
        "type": f"{len(members)}-player",
        "players": [
            {"id": info["id"], "finish": info["finish"], "civ": info["civ"],
             "elo_before": info["old_elo"], "elo_after": new_elos[i]}
            for i, info in enumerate(player_info)
        ],
        "played_at": datetime.utcnow().isoformat()
    })

    save_all_data(all_data)

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
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
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
        name = stats.get("name", f"<@{uid}>")
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
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
    stats = get_player(data, str(target.id), target.display_name)

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
            f"**{c}** — {v['wins']}W / {v['losses']}L ({round(v['wins'] / (v['wins'] + v['losses']) * 100)}% WR)"
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
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
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


# ── /reset_elo ────────────────────────────────────────────────────────────────
@bot.tree.command(name="reset_elo", description="Reset all Elo ratings to 1000 (admin only)")
@app_commands.describe(password="Admin password")
async def reset_elo(interaction: discord.Interaction, password: str):
    # Always ephemeral so password and result are only visible to the caller
    if password != "NotJustCats":
        await interaction.response.send_message("❌ Incorrect password.", ephemeral=True)
        return

    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
    players = data.get("players", {})

    if not players:
        await interaction.response.send_message("No players to reset.", ephemeral=True)
        return

    count = len(players)
    for uid in players:
        players[uid]["elo"] = STARTING_ELO
        players[uid]["wins"] = 0
        players[uid]["losses"] = 0
        players[uid]["civs"] = {}

    # Log the reset as a special match event so the graph reflects it
    data["matches"].append({
        "type": "reset",
        "played_at": datetime.utcnow().isoformat()
    })

    save_all_data(all_data)

    await interaction.response.send_message(
        f"✅ Reset **{count} players** back to {STARTING_ELO} Elo. Match history preserved.",
        ephemeral=True
    )

# ── /graph ────────────────────────────────────────────────────────────────────
@bot.tree.command(name="graph", description="Show the Elo progression graph for all ranked players")
async def graph(interaction: discord.Interaction):
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id_from(interaction))
    active_ids = set()
    for m in data.get("matches", []):
        for p in m.get("players", []):
            active_ids.add(p["id"])

    if not active_ids:
        await interaction.response.send_message("No matches played yet — nothing to graph!", ephemeral=True)
        return

    if not PUBLIC_URL:
        await interaction.response.send_message(
            "⚠️ `PUBLIC_URL` is not set in Railway environment variables.",
            ephemeral=True)
        return

    guild_id = guild_id_from(interaction)
    url = f"{PUBLIC_URL}/graph?guild={guild_id}"
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
