import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import tempfile
import asyncio
import secrets
import hashlib
import hmac
from aiohttp import web, ClientSession
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
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
COOKIE_SECRET = os.getenv("COOKIE_SECRET", secrets.token_hex(32))
OAUTH_SCOPES = "identify"

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
def normalise_civ(name: str) -> str | None:
    """Match a civ name case-insensitively. Returns the correctly-cased name or None."""
    name = name.strip()
    for civ in ALL_CIVS:
        if civ.lower() == name.lower():
            return civ
    return None

# ── Session helpers ──────────────────────────────────────────────────────────
def make_session_token(user_id: str, username: str) -> str:
    payload = f"{user_id}:{username}"
    sig = hmac.new(COOKIE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"

def verify_session_token(token: str):
    try:
        parts = token.split(":")
        if len(parts) != 3: return None, None
        user_id, username, sig = parts
        expected = hmac.new(COOKIE_SECRET.encode(), f"{user_id}:{username}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return None, None
        return user_id, username
    except Exception:
        return None, None

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
    difficulty = lobby.get("difficulty", "Prince")
    embed = discord.Embed(title="🏛️  Game Lobby — Open", color=0x4CAF50)
    embed.add_field(name=f"Host: {lobby['host_name']} · {len(lobby['players'])} player(s) · {difficulty} difficulty",
                    value="\n".join(lines) or "—", inline=False)
    embed.set_footer(text="Use /join_lobby @host to join • Host uses /start_game [land/coastal/any] to begin")
    return embed

def guild_id_from(interaction: discord.Interaction) -> str:
    return str(interaction.guild_id)

# ── Web server ────────────────────────────────────────────────────────────────
def build_graph_html(guild_id: str, logged_in_id: str = None, logged_in_name: str = None) -> str:
    all_data = load_all_data()
    data = all_data.get(guild_id, {"players": {}, "matches": [], "active_games": {}, "game_groups": {}, "lobbies": {}})
    matches = data.get("matches", [])
    players = data.get("players", {})
    active_games = data.get("active_games", {})
    game_groups = data.get("game_groups", {})
    lobbies = data.get("lobbies", {})

    # Build Elo timeline
    active_ids = set()
    for m in matches:
        for p in m.get("players", []):
            active_ids.add(p["id"])

    current_elo = {pid: 1000 for pid in active_ids}
    timeline = [{"label": "Start", **{pid: 1000 for pid in active_ids}}]
    sorted_matches = sorted(matches, key=lambda m: m.get("played_at", ""))
    game_num = 0
    match_index = {}  # label -> match index for click-through
    for i, match in enumerate(sorted_matches):
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
        label = f"G{game_num} ({date_str})"
        timeline.append({"label": label, **{pid: current_elo[pid] for pid in active_ids}})
        match_index[label] = i

    player_list = [
        {"id": pid, "name": players.get(pid, {}).get("name", f"Player {i+1}"), "finalElo": players.get(pid, {}).get("elo", 1000)}
        for i, pid in enumerate(sorted(active_ids, key=lambda x: players.get(x, {}).get("elo", 0), reverse=True))
    ]

    # Pie chart data
    civ_counts = {}
    for m in sorted_matches:
        if m.get("type") in ("reset", None): continue
        for p in m.get("players", []):
            civ = p.get("civ")
            if civ: civ_counts[civ] = civ_counts.get(civ, 0) + 1
    top_civs = sorted(civ_counts.items(), key=lambda x: x[1], reverse=True)[:12]
    pie_labels = [c for c, _ in top_civs]
    pie_values = [v for _, v in top_civs]

    # Per-player profile data
    COASTAL_SET = {"Australia","Brunei","Carthage","Chile","Denmark","England",
        "Indonesia","Japan","Kilwa","Korea","Netherlands","New Zealand",
        "Norway","Oman","Philippines","Phoenicia","Polynesia","Portugal","Spain",
        "Tonga","Tunisia","UAE","Venice"}
    lb_data = {}
    for pid in active_ids:
        p = players.get(pid, {})
        civs = p.get("civs", {})
        def civ_wr(item):
            v = item[1]; g = v["wins"] + v["losses"]
            return v["wins"] / g if g > 0 else 0
        top_p_civs = sorted(civs.items(), key=civ_wr, reverse=True)[:5]
        coastal_games = land_games = 0
        peak_elo = 1000
        big_game_win = played_8 = False
        unique_civs = set(); win_civs = set()
        victory_counts = {"Domination": 0, "Science": 0, "Culture": 0, "Diplomatic": 0}
        difficulty_wins = {"Prince": 0, "King": 0}
        for m in sorted_matches:
            if m.get("type") in ("reset", None): continue
            game_players = m.get("players", [])
            game_size = len(game_players)
            winner_id = next((mp["id"] for mp in game_players if mp.get("finish") == 1), None)
            if winner_id == pid:
                vtype = m.get("victory_type")
                diff = m.get("difficulty", "Prince")
                if vtype in victory_counts: victory_counts[vtype] += 1
                if diff in difficulty_wins: difficulty_wins[diff] += 1
            for mp in game_players:
                if mp["id"] == pid:
                    civ_name = mp.get("civ"); finish = mp.get("finish", 99)
                    elo_after = mp.get("elo_after", 1000)
                    if civ_name:
                        unique_civs.add(civ_name)
                        if finish == 1: win_civs.add(civ_name)
                        if civ_name in COASTAL_SET: coastal_games += 1
                        else: land_games += 1
                    if elo_after > peak_elo: peak_elo = elo_after
                    if finish == 1 and game_size >= 6: big_game_win = True
                    if game_size >= 8: played_8 = True
        # Spider scores (0-100)
        wins_n = p.get("wins", 0); losses_n = p.get("losses", 0); total_n = wins_n + losses_n
        spider_wr = round(wins_n / total_n * 100) if total_n > 0 else 0
        spider_variety = min(100, round(len(unique_civs) / 20 * 100))
        map_total_n = coastal_games + land_games
        spider_coastal = round(coastal_games / map_total_n * 100) if map_total_n > 0 else 0
        bg_wins = bg_total = 0
        for m in sorted_matches:
            if m.get("type") in ("reset", None): continue
            gps = m.get("players", [])
            if len(gps) >= 4:
                for mp in gps:
                    if mp["id"] == pid:
                        bg_total += 1
                        if mp.get("finish") == 1: bg_wins += 1
        spider_biggame = round(bg_wins / bg_total * 100) if bg_total > 0 else 0
        elo_changes = []
        for m in sorted_matches:
            if m.get("type") in ("reset", None): continue
            for mp in m.get("players", []):
                if mp["id"] == pid:
                    elo_changes.append(mp.get("elo_after", 1000) - mp.get("elo_before", 1000))
        avg_elo_change = sum(elo_changes) / len(elo_changes) if elo_changes else 0
        spider_growth = max(0, min(100, round(avg_elo_change * 4 + 50)))

        lb_data[pid] = {
            "wins": p.get("wins", 0), "losses": p.get("losses", 0),
            "top_civs": [{"civ": c, "wins": v["wins"], "losses": v["losses"]} for c, v in top_p_civs],
            "coastal_games": coastal_games, "land_games": land_games,
            "unique_civs": len(unique_civs), "win_civs": len(win_civs),
            "peak_elo": peak_elo, "big_game_win": big_game_win, "played_8": played_8,
            "victory_counts": victory_counts, "difficulty_wins": difficulty_wins,
            "spider": {"win_rate": spider_wr, "civ_variety": spider_variety, "coastal_mastery": spider_coastal, "big_game": spider_biggame, "elo_growth": spider_growth},
        }

    # History data (most recent first)
    history = []
    for i, m in enumerate(reversed(sorted_matches)):
        if m.get("type") == "reset": continue
        orig_idx = len(sorted_matches) - 1 - list(reversed(sorted_matches)).index(m)
        game_ps = m.get("players", [])
        winner = next((mp for mp in game_ps if mp.get("finish") == 1), None)
        winner_name = players.get(winner["id"], {}).get("name", "?") if winner else "?"
        winner_civ = winner.get("civ", "?") if winner else "?"
        draft_pools = m.get("draft_pools", {})
        history.append({
            "idx": orig_idx,
            "label": f"G{len(sorted_matches) - list(reversed(sorted_matches)).index(m)}",
            "date": m.get("played_at", "")[:10],
            "type": m.get("type", "?"),
            "difficulty": m.get("difficulty", "Prince"),
            "map_type": m.get("map_type", "any"),
            "victory_type": m.get("victory_type", None),
            "winner_name": winner_name,
            "winner_civ": winner_civ,
            "draft_pools": draft_pools,
            "players": [{"id": mp.get("id",""), "name": players.get(mp["id"], {}).get("name", "?"), "civ": mp.get("civ","?"), "finish": mp.get("finish",0), "elo_before": mp.get("elo_before",1000), "elo_after": mp.get("elo_after",1000)} for mp in sorted(game_ps, key=lambda x: x.get("finish",99))],
        })

    # Live games data
    live_games = []
    for game_id, group in game_groups.items():
        host_name = players.get(game_id, {}).get("name", group.get("player_names", ["?"])[0])
        picks = group.get("picks", {})
        draft = group.get("draft", {})
        ps = []
        for pid, name in zip(group.get("players", []), group.get("player_names", [])):
            chosen = picks.get(pid)
            pool = draft.get(pid, []) if draft else []
            ps.append({"name": name, "chosen": chosen, "pool": pool})
        live_games.append({
            "host": host_name,
            "difficulty": group.get("difficulty", "Prince"),
            "map_type": group.get("map_type", "any"),
            "players": ps,
        })
    # Also include open lobbies
    for host_id, lobby in lobbies.items():
        live_games.append({
            "host": lobby.get("host_name", "?"),
            "host_id": host_id,
            "difficulty": lobby.get("difficulty", "Prince"),
            "status": "lobby",
            "players": [{"name": n, "chosen": None, "pool": []} for n in lobby.get("player_names", [])],
        })

    import json as _json
    timeline_json = _json.dumps(timeline)
    players_json = _json.dumps(player_list)
    pie_labels_json = _json.dumps(pie_labels)
    pie_values_json = _json.dumps(pie_values)
    lb_data_json = _json.dumps(lb_data)
    history_json = _json.dumps(history)
    live_games_json = _json.dumps(live_games)
    match_index_json = _json.dumps(match_index)
    logged_in_id_json = _json.dumps(logged_in_id or "")
    logged_in_name_json = _json.dumps(logged_in_name or "")
    all_civs_json = _json.dumps(ALL_CIVS)
    # Load display name and fav civ from player prefs
    prefs = players.get(logged_in_id or "", {{}}).get("prefs", {{}}) if logged_in_id else {{}}
    display_name_json = _json.dumps(prefs.get("display_name", logged_in_name or ""))
    fav_civ_json = _json.dumps(prefs.get("fav_civ", ""))
    guild_id_json = _json.dumps(guild_id)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Civ 5 Ranked</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ height: 100%; overflow: hidden; }}
  body {{ background: #080a0f; color: #e2e8f0; font-family: 'IBM Plex Mono', monospace; height: 100vh; display: flex; flex-direction: column; padding: 16px; gap: 10px; }}
  .topbar {{ display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; height: 40px; }}
  h1 {{ font-family: 'Cinzel', serif; font-size: 20px; color: #f97316; letter-spacing: 3px; text-shadow: 0 0 30px rgba(249,115,22,0.4); }}
  .subtitle {{ color: #475569; font-size: 10px; letter-spacing: 2px; }}
  .tabs {{ display: flex; gap: 4px; flex-shrink: 0; border-bottom: 1px solid #1e2130; padding-bottom: 0; }}
  .tab {{ padding: 7px 16px; cursor: pointer; font-size: 11px; letter-spacing: 1px; color: #475569; border-bottom: 2px solid transparent; margin-bottom: -1px; transition: all 0.15s; }}
  .tab.active {{ color: #f97316; border-bottom-color: #f97316; }}
  .tab:hover {{ color: #94a3b8; }}
  .page {{ display: none; flex: 1; overflow: hidden; min-height: 0; }}
  .page.active {{ display: flex; flex-direction: column; }}
  /* Stats page */
  .player-grid {{ display: flex; gap: 8px; flex-wrap: wrap; flex-shrink: 0; overflow: hidden; max-height: 72px; }}
  .player-btn {{ border-radius: 8px; padding: 6px 12px; cursor: pointer; border-width: 1px; border-style: solid; background: transparent; font-family: 'IBM Plex Mono', monospace; transition: all 0.15s; text-align: left; }}
  .player-btn.profile-active {{ box-shadow: 0 0 0 2px currentColor; }}
  .player-name {{ font-weight: 600; font-size: 12px; }}
  .player-elo {{ font-size: 10px; color: #64748b; margin-top: 2px; }}
  .rank-toggle {{ display: flex; align-items: center; gap: 6px; color: #475569; font-size: 10px; cursor: pointer; flex-shrink: 0; margin-bottom: 6px; }}
  .main-grid {{ display: grid; grid-template-columns: 1.4fr 1fr; grid-template-rows: 1fr 1fr; gap: 10px; flex: 1; overflow: hidden; min-height: 0; }}
  .card {{ background: #0d1017; border: 1px solid #1e2130; border-radius: 12px; padding: 14px; display: flex; flex-direction: column; overflow: hidden; min-height: 0; }}
  .card-elo {{ grid-row: 1 / 3; }}
  .card-title {{ color: #94a3b8; font-size: 10px; letter-spacing: 2px; margin-bottom: 10px; flex-shrink: 0; }}
  .chart-wrap {{ flex: 1; position: relative; overflow: hidden; min-height: 0; }}
  .chart-wrap canvas {{ position: absolute; top: 0; left: 0; width: 100% !important; height: 100% !important; }}
  .lb-list {{ flex: 1; overflow-y: auto; min-height: 0; }}
  .lb-row {{ display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid #1a1f2e; cursor: pointer; }}
  .lb-row:hover {{ background: #0f1420; border-radius: 4px; padding-left: 4px; padding-right: 4px; }}
  .empty {{ text-align: center; color: #334155; padding: 40px; font-size: 12px; }}
  /* Live & History pages */
  .scroll-page {{ flex: 1; overflow-y: auto; min-height: 0; padding-right: 4px; }}
  .game-card {{ background: #0d1017; border: 1px solid #1e2130; border-radius: 10px; padding: 14px; margin-bottom: 10px; }}
  .game-header {{ display: flex; align-items: center; justify-content: space-between; cursor: pointer; }}
  .game-title {{ font-size: 13px; font-weight: 600; color: #e2e8f0; }}
  .game-meta {{ font-size: 10px; color: #475569; margin-top: 3px; }}
  .game-body {{ margin-top: 12px; border-top: 1px solid #1e2130; padding-top: 12px; display: none; }}
  .game-body.open {{ display: block; }}
  .player-row {{ display: flex; align-items: center; gap: 10px; padding: 5px 0; border-bottom: 1px solid #1a1f2e; font-size: 11px; }}
  .badge {{ font-size: 9px; padding: 2px 6px; border-radius: 4px; background: #1e2130; color: #94a3b8; }}
  .badge.coastal {{ background: #0c2030; color: #06b6d4; }}
  .badge.land {{ background: #0c2010; color: #22c55e; }}
  .badge.dom {{ background: #200c0c; color: #ef4444; }}
  .badge.sci {{ background: #0c1a20; color: #3b82f6; }}
  .badge.cul {{ background: #200c20; color: #a855f7; }}
  .badge.dip {{ background: #1a1a0c; color: #eab308; }}
  .chevron {{ font-size: 10px; color: #475569; transition: transform 0.15s; }}
  .chevron.open {{ transform: rotate(180deg); }}
  .pool-label {{ font-size: 9px; color: #334155; letter-spacing: 1px; margin: 8px 0 4px; }}
  .pool-civs {{ display: flex; flex-wrap: wrap; gap: 4px; }}
  .pool-civ {{ font-size: 9px; padding: 2px 6px; border-radius: 4px; background: #0f1420; color: #475569; }}
  .pool-civ.chosen {{ background: #1a2a1a; color: #22c55e; border: 1px solid #22c55e44; }}
  .hist-elo {{ font-size: 10px; color: #475569; }}
  .hist-elo.pos {{ color: #22c55e; }}
  .hist-elo.neg {{ color: #ef4444; }}
  .no-games {{ text-align: center; color: #334155; padding: 60px; font-size: 12px; letter-spacing: 1px; }}
  .modal-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 100; display: flex; align-items: center; justify-content: center; }}
  .modal {{ background: #0d1017; border: 1px solid #1e2130; border-radius: 14px; padding: 24px; width: 380px; max-width: 90vw; }}
  .modal-title {{ font-size: 14px; font-weight: 700; color: #e2e8f0; margin-bottom: 16px; }}
  .form-label {{ font-size: 10px; color: #64748b; letter-spacing: 1px; margin-bottom: 5px; display: block; }}
  .form-input {{ width: 100%; background: #080a0f; border: 1px solid #1e2130; border-radius: 8px; padding: 8px 12px; color: #e2e8f0; font-family: 'IBM Plex Mono', monospace; font-size: 12px; outline: none; margin-bottom: 12px; }}
  .form-input:focus {{ border-color: #f97316; }}
  .form-select {{ width: 100%; background: #080a0f; border: 1px solid #1e2130; border-radius: 8px; padding: 8px 12px; color: #e2e8f0; font-family: 'IBM Plex Mono', monospace; font-size: 12px; outline: none; margin-bottom: 12px; }}
  .btn {{ padding: 8px 16px; border-radius: 8px; border: none; cursor: pointer; font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 600; transition: opacity 0.15s; }}
  .btn:hover {{ opacity: 0.8; }}
  .btn-primary {{ background: #f97316; color: #080a0f; }}
  .btn-ghost {{ background: transparent; border: 1px solid #1e2130; color: #475569; }}
  .hist-filter {{ display: flex; gap: 6px; flex-shrink: 0; margin-bottom: 8px; }}
  .filter-btn {{ padding: 5px 12px; border-radius: 6px; border: 1px solid #1e2130; background: transparent; color: #475569; font-family: 'IBM Plex Mono', monospace; font-size: 10px; cursor: pointer; }}
  .filter-btn.active {{ border-color: #f97316; color: #f97316; }}
</style>
</head>
<body>
<div class="topbar">
  <div><h1>CIV 5 RANKED</h1><p class="subtitle">LIVE DATA · REFRESH FOR LATEST</p></div>
  <div id="authArea"></div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('stats')">STATS</div>
  <div class="tab" onclick="switchTab('live')">LIVE GAMES</div>
  <div class="tab" onclick="switchTab('history')">HISTORY</div>
</div>

<!-- STATS PAGE -->
<div class="page active" id="page-stats">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-shrink:0;margin-bottom:8px">
    <div class="player-grid" id="playerGrid"></div>
    <label class="rank-toggle" style="flex-shrink:0;margin-left:12px"><input type="checkbox" id="rankToggle" checked onchange="toggleRanks()"> RANK LINES</label>
  </div>
  <div class="main-grid">
    <div class="card card-elo">
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
    <div class="card" id="profileCard" style="display:none;grid-column:2;grid-row:1/3;">
      <div style="flex:1;overflow-y:auto;min-height:0;" id="profileContent"></div>
    </div>
  </div>
</div>

<!-- LIVE GAMES PAGE -->
<div class="page" id="page-live">
  <div class="scroll-page" id="liveContent"></div>
</div>

<!-- HISTORY PAGE -->
<div class="page" id="page-history">
  <div class="hist-filter" id="histFilter" style="display:none">
    <button class="filter-btn active" onclick="filterHistory('all')">ALL GAMES</button>
    <button class="filter-btn" onclick="filterHistory('mine')">MY GAMES</button>
  </div>
  <div class="scroll-page" id="historyContent"></div>
</div>
<div id="modalContainer"></div>

<script>
const TIMELINE = {timeline_json};
const PLAYERS = {players_json};
const PIE_LABELS = {pie_labels_json};
const PIE_VALUES = {pie_values_json};
const LB_DATA = {lb_data_json};
const HISTORY = {history_json};
const LIVE_GAMES = {live_games_json};
const MATCH_INDEX = {match_index_json};
const LOGGED_IN_ID = {logged_in_id_json};
const LOGGED_IN_NAME = {logged_in_name_json};
const ALL_CIVS = {all_civs_json};
const DISPLAY_NAME = {display_name_json};
const FAV_CIV = {fav_civ_json};
const GUILD_ID = {guild_id_json};
const PALETTE = ["#f97316","#3b82f6","#a855f7","#22c55e","#ef4444","#eab308","#06b6d4","#ec4899","#f43f5e","#10b981","#8b5cf6","#0ea5e9"];

const ACHIEVEMENTS = [
  {{id:"civ10",    icon:"🗺️", name:"Explorer",          desc:"Play 10 different civs",          check: d => d.unique_civs >= 10}},
  {{id:"civ20",    icon:"🌍", name:"World Traveller",   desc:"Play 20 different civs",          check: d => d.unique_civs >= 20}},
  {{id:"winciv5",  icon:"⚔️", name:"Tactician",         desc:"Win with 5 different civs",       check: d => d.win_civs >= 5}},
  {{id:"winciv10", icon:"🏛️", name:"Polymath",          desc:"Win with 10 different civs",      check: d => d.win_civs >= 10}},
  {{id:"coastal",  icon:"⛵", name:"Sea Dog",            desc:"Play a coastal game",             check: d => d.coastal_games >= 1}},
  {{id:"land",     icon:"🏕️", name:"Landlubber",         desc:"Play a land game",                check: d => d.land_games >= 1}},
  {{id:"rprince",  icon:"⚙️", name:"Prince",             desc:"Reach Prince rank",               check: d => d.peak_elo >= 1100}},
  {{id:"rking",    icon:"🛡️", name:"King",               desc:"Reach King rank",                 check: d => d.peak_elo >= 1250}},
  {{id:"remperor", icon:"⚔️", name:"Emperor",            desc:"Reach Emperor rank",              check: d => d.peak_elo >= 1400}},
  {{id:"rdeity",   icon:"🏆", name:"Deity",              desc:"Reach Deity rank",                check: d => d.peak_elo >= 1600}},
  {{id:"dprince",  icon:"👑", name:"Prince",             desc:"Win a game on Prince difficulty", check: d => (d.difficulty_wins?.Prince || 0) >= 1}},
  {{id:"dking",    icon:"🏰", name:"King",               desc:"Win a game on King difficulty",   check: d => (d.difficulty_wins?.King || 0) >= 1}},
  {{id:"dom1",     icon:"⚔️", name:"Domination I",       desc:"Win 1 Domination victory",        check: d => (d.victory_counts?.Domination || 0) >= 1}},
  {{id:"dom5",     icon:"⚔️", name:"Domination V",       desc:"Win 5 Domination victories",      check: d => (d.victory_counts?.Domination || 0) >= 5}},
  {{id:"dom10",    icon:"⚔️", name:"Domination X",       desc:"Win 10 Domination victories",     check: d => (d.victory_counts?.Domination || 0) >= 10}},
  {{id:"sci1",     icon:"🚀", name:"Science I",          desc:"Win 1 Science victory",           check: d => (d.victory_counts?.Science || 0) >= 1}},
  {{id:"sci5",     icon:"🚀", name:"Science V",          desc:"Win 5 Science victories",         check: d => (d.victory_counts?.Science || 0) >= 5}},
  {{id:"sci10",    icon:"🚀", name:"Science X",          desc:"Win 10 Science victories",        check: d => (d.victory_counts?.Science || 0) >= 10}},
  {{id:"cul1",     icon:"🎭", name:"Culture I",          desc:"Win 1 Culture victory",           check: d => (d.victory_counts?.Culture || 0) >= 1}},
  {{id:"cul5",     icon:"🎭", name:"Culture V",          desc:"Win 5 Culture victories",         check: d => (d.victory_counts?.Culture || 0) >= 5}},
  {{id:"cul10",    icon:"🎭", name:"Culture X",          desc:"Win 10 Culture victories",        check: d => (d.victory_counts?.Culture || 0) >= 10}},
  {{id:"dip1",     icon:"🕊️", name:"Diplomatic I",       desc:"Win 1 Diplomatic victory",        check: d => (d.victory_counts?.Diplomatic || 0) >= 1}},
  {{id:"dip5",     icon:"🕊️", name:"Diplomatic V",       desc:"Win 5 Diplomatic victories",      check: d => (d.victory_counts?.Diplomatic || 0) >= 5}},
  {{id:"dip10",    icon:"🕊️", name:"Diplomatic X",       desc:"Win 10 Diplomatic victories",     check: d => (d.victory_counts?.Diplomatic || 0) >= 10}},
  {{id:"big6",     icon:"👥", name:"Grand Victor",       desc:"Win a 6+ player game",            check: d => d.big_game_win}},
  {{id:"full8",    icon:"🎖️", name:"Full House",         desc:"Play in an 8-player game",        check: d => d.played_8}},
];

function rankLabel(elo) {{
  if (elo>=1600) return "🏆 Deity";
  if (elo>=1400) return "⚔️ Emperor";
  if (elo>=1250) return "🛡️ King";
  if (elo>=1100) return "⚙️ Prince";
  if (elo>=1000) return "🌿 Chieftain";
  return "🪨 Settler";
}}

function victoryBadge(vt) {{
  if (!vt) return "";
  const cls = {{Domination:"dom",Science:"sci",Culture:"cul",Diplomatic:"dip"}}[vt]||"";
  const icon = {{Domination:"⚔️",Science:"🚀",Culture:"🎭",Diplomatic:"🕊️"}}[vt]||"";
  return `<span class="badge ${{cls}}">${{icon}} ${{vt}}</span>`;
}}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name) {{
  document.querySelectorAll(".tab").forEach((t,i) => {{
    const names = ["stats","live","history"];
    t.classList.toggle("active", names[i] === name);
  }});
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.getElementById("page-"+name).classList.add("active");
  if (name === "live") buildLive();
  if (name === "history") buildHistory();
}}

// ── Elo chart ─────────────────────────────────────────────────────────────────
let activeProfile = null;
const pieCard = document.getElementById("pieCard");
const lbCard  = document.getElementById("lbCard");
const profileCard = document.getElementById("profileCard");
const profileContent = document.getElementById("profileContent");

const hidden = new Set();
let eloChart;
if (PLAYERS.length) {{
  eloChart = new Chart(document.getElementById("eloChart").getContext("2d"), {{
    type: "line",
    data: {{
      labels: TIMELINE.map(t => t.label),
      datasets: PLAYERS.map((p,i) => ({{
        label: p.name,
        data: TIMELINE.map(t => t[p.id] ?? null),
        borderColor: PALETTE[i%PALETTE.length],
        backgroundColor: PALETTE[i%PALETTE.length],
        borderWidth: 2.5, pointRadius: 4, pointHoverRadius: 7, tension: 0.3, spanGaps: true,
      }}))
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{mode:"index",intersect:false}},
      onClick: (evt, elements) => {{
        if (!elements.length) return;
        const label = TIMELINE[elements[0].index]?.label;
        const idx = MATCH_INDEX[label];
        if (idx !== undefined) showHistoryGame(idx);
      }},
      plugins: {{
        legend: {{display:false}},
        tooltip: {{
          backgroundColor:"#0f1117", borderColor:"#2a2d3a", borderWidth:1,
          titleColor:"#64748b", bodyColor:"#e2e8f0",
          titleFont:{{family:"IBM Plex Mono",size:11}}, bodyFont:{{family:"IBM Plex Mono",size:12}},
          filter: item => item.parsed.y !== null,
          callbacks: {{label: ctx => ctx.parsed.y !== null ? ` ${{ctx.dataset.label}}: ${{ctx.parsed.y}} (${{rankLabel(ctx.parsed.y)}})` : null}}
        }}
      }},
      scales: {{
        x: {{ticks:{{color:"#475569",font:{{family:"IBM Plex Mono",size:9}}}},grid:{{color:"#1a1f2e"}}}},
        y: {{ticks:{{color:"#475569",font:{{family:"IBM Plex Mono",size:9}}}},grid:{{color:"#1a1f2e"}},min:750}}
      }}
    }}
  }});
}} else {{
  document.getElementById("eloChart").parentElement.innerHTML = '<p class="empty">No matches yet</p>';
}}

function toggleRanks() {{
  if (eloChart) {{ eloChart.options.scales.y.min = document.getElementById("rankToggle").checked ? 750 : undefined; eloChart.update(); }}
}}

// ── Player buttons ────────────────────────────────────────────────────────────
PLAYERS.forEach((p,i) => {{
  const color = PALETTE[i%PALETTE.length];
  const medals = ["🥇","🥈","🥉"];
  const btn = document.createElement("button");
  btn.className = "player-btn";
  btn.style.borderColor = color; btn.style.color = color;
  btn.innerHTML = `<div class="player-name">${{medals[i]||"#"+(i+1)}} ${{p.name}}</div><div class="player-elo">${{p.finalElo}} · ${{rankLabel(p.finalElo)}}</div>`;
  btn.onclick = () => {{
    if (activeProfile === p.id) {{ activeProfile=null; btn.classList.remove("profile-active"); hideProfile(); }}
    else {{ document.querySelectorAll(".player-btn").forEach(b=>b.classList.remove("profile-active")); activeProfile=p.id; btn.classList.add("profile-active"); showProfile(p,i); }}
  }};
  document.getElementById("playerGrid").appendChild(btn);
}});

// ── Profile panel ─────────────────────────────────────────────────────────────
function hideProfile() {{ profileCard.style.display="none"; pieCard.style.display="flex"; lbCard.style.display="flex"; }}

function showProfile(p, idx) {{
  const color = PALETTE[idx%PALETTE.length];
  const d = LB_DATA[p.id]||{{}};
  const wins=d.wins||0, losses=d.losses||0, total=wins+losses;
  const wr=total>0?Math.round(wins/total*100):0;
  const coastal=d.coastal_games||0, land=d.land_games||0, mapTotal=coastal+land;
  const coastalPct=mapTotal>0?Math.round(coastal/mapTotal*100):0;
  const topCivs=d.top_civs||[];
  const unlocked=ACHIEVEMENTS.filter(a=>a.check(d)).length;
  const civRows=topCivs.length>0?topCivs.map(c=>{{const g=c.wins+c.losses,cwr=g>0?Math.round(c.wins/g*100):0;return`<div class="lb-row"><div style="flex:1;font-size:11px;color:#e2e8f0">${{c.civ}}</div><div style="font-size:10px;color:#475569">${{c.wins}}W/${{c.losses}}L·${{cwr}}%</div></div>`;
  }}).join(""):`<div style="color:#334155;font-size:11px;padding:8px 0">No civ data yet</div>`;
  const achRows=ACHIEVEMENTS.map(a=>{{const ok=a.check(d);return`<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #1a1f2e;opacity:${{ok?1:0.3}}"><span style="font-size:16px;width:22px;text-align:center">${{a.icon}}</span><div style="flex:1"><div style="font-size:11px;font-weight:600;color:${{ok?"#e2e8f0":"#475569"}}">${{a.name}}</div><div style="font-size:9px;color:#475569;margin-top:1px">${{a.desc}}</div></div><span style="font-size:12px;color:${{ok?"#f97316":"#1e2130"}}">${{ok?"✓":"○"}}</span></div>`;
  }}).join("");
  const spider = d.spider || {{}};
  const avgEloRaw = spider.elo_growth ? Math.round((spider.elo_growth - 50) / 4) : 0;
  const avgEloSign = avgEloRaw >= 0 ? "+" : "";
  const avgEloDisplay = avgEloRaw;
  const avgEloColor = avgEloRaw >= 0 ? "#22c55e" : "#ef4444";

  profileContent.innerHTML=`
    <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px">
      <div><div style="font-weight:700;font-size:16px;color:${{color}}">${{p.name}}</div><div style="font-size:11px;color:#94a3b8;margin-top:2px">${{rankLabel(p.finalElo)}} · ${{p.finalElo}} Elo</div></div>
      <span style="font-size:10px;color:#475569;cursor:pointer;padding:4px 8px;border:1px solid #1e2130;border-radius:6px;flex-shrink:0" onclick="closeProfile()">✕ close</span>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:14px">
      <div style="background:#080a0f;border:1px solid #1e2130;border-radius:8px;padding:8px;text-align:center"><div style="font-size:18px;font-weight:700;color:#e2e8f0">${{wins}}</div><div style="font-size:9px;color:#475569;margin-top:2px">WINS</div></div>
      <div style="background:#080a0f;border:1px solid #1e2130;border-radius:8px;padding:8px;text-align:center"><div style="font-size:18px;font-weight:700;color:#e2e8f0">${{losses}}</div><div style="font-size:9px;color:#475569;margin-top:2px">LOSSES</div></div>
      <div style="background:#080a0f;border:1px solid #1e2130;border-radius:8px;padding:8px;text-align:center"><div style="font-size:18px;font-weight:700;color:${{color}}">${{wr}}%</div><div style="font-size:9px;color:#475569;margin-top:2px">WIN RATE</div></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:14px">
      <div style="background:#080a0f;border:1px solid #1e2130;border-radius:8px;padding:8px;text-align:center">
        <div style="font-size:14px;font-weight:700;color:${{avgEloColor}}">${{avgEloSign}}${{avgEloDisplay}}</div>
        <div style="font-size:9px;color:#475569;margin-top:2px">AVG ELO/GAME</div>
      </div>
      <div style="background:#080a0f;border:1px solid #1e2130;border-radius:8px;padding:8px;text-align:center">
        <div style="font-size:14px;font-weight:700;color:#e2e8f0">${{total}}</div>
        <div style="font-size:9px;color:#475569;margin-top:2px">GAMES PLAYED</div>
      </div>
    </div>
    <div style="font-size:10px;color:#64748b;letter-spacing:1px;margin-bottom:6px">MAP PREFERENCE</div>
    <div style="display:flex;gap:6px;margin-bottom:14px">
      <div style="flex:1;background:#080a0f;border:1px solid #1e2130;border-radius:8px;padding:8px;text-align:center"><div style="font-size:14px;font-weight:700;color:#06b6d4">${{coastal}}</div><div style="font-size:9px;color:#475569;margin-top:2px">COASTAL (${{coastalPct}}%)</div></div>
      <div style="flex:1;background:#080a0f;border:1px solid #1e2130;border-radius:8px;padding:8px;text-align:center"><div style="font-size:14px;font-weight:700;color:#22c55e">${{land}}</div><div style="font-size:9px;color:#475569;margin-top:2px">LAND (${{100-coastalPct}}%)</div></div>
    </div>
    <div style="font-size:10px;color:#64748b;letter-spacing:1px;margin-bottom:6px">TOP CIVS BY WIN RATE</div>
    ${{civRows}}
    <div style="font-size:10px;color:#64748b;letter-spacing:1px;margin:14px 0 6px">ACHIEVEMENTS (${{unlocked}}/${{ACHIEVEMENTS.length}})</div>
    <div style="position:relative;height:190px;margin-bottom:14px"><canvas id="spiderChart"></canvas></div>
    ${{achRows}}`;
  pieCard.style.display="none"; lbCard.style.display="none"; profileCard.style.display="flex";

  // Render spider chart
  const spiderCanvas = document.getElementById("spiderChart");
  if (spiderCanvas) {{
    if (window._spiderChart) {{ window._spiderChart.destroy(); window._spiderChart = null; }}
    window._spiderChart = new Chart(spiderCanvas.getContext("2d"), {{
      type: "radar",
      data: {{
        labels: ["Win Rate","Civ Variety","Coastal","Big Game","Elo Growth"],
        datasets: [{{
          data: [spider.win_rate||0, spider.civ_variety||0, spider.coastal_mastery||0, spider.big_game||0, spider.elo_growth||50],
          backgroundColor: color + "33", borderColor: color, borderWidth: 2,
          pointBackgroundColor: color, pointRadius: 3,
        }}]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        scales: {{ r: {{ min:0, max:100, ticks:{{display:false}}, grid:{{color:"#1e2130"}}, angleLines:{{color:"#1e2130"}}, pointLabels:{{color:"#64748b",font:{{family:"IBM Plex Mono",size:9}}}} }} }},
        plugins: {{ legend:{{display:false}}, tooltip:{{backgroundColor:"#0f1117",borderColor:"#2a2d3a",borderWidth:1,titleColor:"#64748b",bodyColor:"#e2e8f0",titleFont:{{family:"IBM Plex Mono",size:10}},bodyFont:{{family:"IBM Plex Mono",size:11}}}} }}
      }}
    }});
  }}
}}

function closeProfile() {{ activeProfile=null; document.querySelectorAll(".player-btn").forEach(b=>b.classList.remove("profile-active")); hideProfile(); }}

// ── Leaderboard ───────────────────────────────────────────────────────────────
function buildLeaderboard() {{
  const lbList=document.getElementById("lbList"); if(!lbList)return;
  const lbMedals=["🥇","🥈","🥉"];
  PLAYERS.forEach((p,i)=>{{
    const color=PALETTE[i%PALETTE.length],wins=LB_DATA[p.id]?.wins||0,losses=LB_DATA[p.id]?.losses||0;
    const wr=(wins+losses)>0?Math.round(wins/(wins+losses)*100):0;
    const row=document.createElement("div"); row.className="lb-row";
    row.innerHTML=`<span style="font-size:18px;width:28px;text-align:center">${{lbMedals[i]||"#"+(i+1)}}</span><div style="flex:1"><div style="font-weight:600;font-size:13px;color:${{color}}">${{p.name}}</div><div style="font-size:10px;color:#475569;margin-top:3px">${{wins}}W/${{losses}}L·${{wr}}%WR</div></div><div style="text-align:right"><div style="font-weight:700;font-size:14px;color:#e2e8f0">${{p.finalElo}}</div><div style="font-size:10px;color:#475569;margin-top:2px">${{rankLabel(p.finalElo)}}</div></div>`;
    row.onclick=()=>{{if(activeProfile===p.id){{activeProfile=null;hideProfile();}}else{{activeProfile=p.id;document.querySelectorAll(".player-btn").forEach(b=>b.classList.remove("profile-active"));const btns=document.querySelectorAll(".player-btn");if(btns[i])btns[i].classList.add("profile-active");showProfile(p,i);}}}};
    lbList.appendChild(row);
  }});
}}
buildLeaderboard();

// ── Pie chart ─────────────────────────────────────────────────────────────────
if (PIE_LABELS.length) {{
  new Chart(document.getElementById("pieChart").getContext("2d"),{{
    type:"doughnut",
    data:{{labels:PIE_LABELS,datasets:[{{data:PIE_VALUES,backgroundColor:PALETTE.concat(PALETTE),borderColor:"#080a0f",borderWidth:2,hoverOffset:6}}]}},
    options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:"right",labels:{{color:"#94a3b8",font:{{family:"IBM Plex Mono",size:9}},boxWidth:10,padding:8}}}},tooltip:{{backgroundColor:"#0f1117",borderColor:"#2a2d3a",borderWidth:1,titleColor:"#64748b",bodyColor:"#e2e8f0",titleFont:{{family:"IBM Plex Mono",size:11}},bodyFont:{{family:"IBM Plex Mono",size:12}},callbacks:{{label:ctx=>` ${{ctx.label}}: ${{ctx.parsed}} games`}}}}}}}}
  }});
}} else {{
  document.getElementById("pieCard").innerHTML='<p class="empty">No civ data yet</p>';
}}

// ── Live Games ────────────────────────────────────────────────────────────────
function buildLive() {{
  const el=document.getElementById("liveContent");
  // Create lobby button for logged-in users
  const createBtn = LOGGED_IN_ID ? `<div style="display:flex;justify-content:flex-end;margin-bottom:10px"><button class="btn btn-primary" onclick="openLobbyModal()">+ Create Lobby</button></div>` : "";
  if(!LIVE_GAMES.length){{el.innerHTML=createBtn+'<p class="no-games">NO ACTIVE GAMES RIGHT NOW</p>';return;}}
  el.innerHTML=createBtn;
  LIVE_GAMES.forEach((g,gi)=>{{
    const isLobby=g.status==="lobby";
    const pickedCount=g.players.filter(p=>p.chosen).length;
    const card=document.createElement("div"); card.className="game-card";
    const liveMapLabel = {{"land":"🏕️ Land","coastal":"⛵ Coastal","any":"🌐 Any","skip":"No draft"}}[g.map_type]||"";
    card.innerHTML=`
      <div class="game-header" onclick="toggleGame('live-${{gi}}')">
        <div>
          <div class="game-title">${{isLobby?"🏛️ Open Lobby":"⚔️ Game In Progress"}} · Host: ${{g.host}}</div>
          <div class="game-meta">${{g.players.length}} players · ${{g.difficulty}}${{liveMapLabel?" · "+liveMapLabel:""}}${{isLobby?"":" · "+pickedCount+"/"+g.players.length+" picked"}}</div>
        </div>
        <span class="chevron" id="chev-live-${{gi}}">▼</span>
      </div>
      <div class="game-body" id="live-${{gi}}">
        ${{g.players.map(p=>`
          <div class="player-row">
            <div style="flex:1;font-weight:600">${{p.name}}</div>
            ${{p.chosen?`<span class="badge" style="color:#22c55e;background:#0c2010;border:1px solid #22c55e44">${{p.chosen}}</span>`:'<span class="badge">picking...</span>'}}
          </div>
          ${{p.pool.length?`<div class="pool-label">DRAFT POOL</div><div class="pool-civs">${{p.pool.map(c=>`<span class="pool-civ ${{c===p.chosen?"chosen":""}}">${{c}}</span>`).join("")}}</div>`:""}}`).join("")}}
      </div>`;
    el.appendChild(card);
  }});
}}

// ── History ───────────────────────────────────────────────────────────────────
function buildHistory() {{
  const el=document.getElementById("historyContent");
  const games = historyFilter === "mine" && LOGGED_IN_ID
    ? HISTORY.filter(g => g.players.some(p => p.id === LOGGED_IN_ID))
    : HISTORY;
  if(!games.length){{el.innerHTML='<p class="no-games">NO GAMES FOUND</p>';return;}}
  el.innerHTML="";
  games.forEach((g,gi)=>{{
    const card=document.createElement("div"); card.className="game-card";
    const vBadge=victoryBadge(g.victory_type);
    const isMine = LOGGED_IN_ID && g.players.some(p => p.id === LOGGED_IN_ID);
    const mapLabel = {{"land":"🏕️ Land","coastal":"⛵ Coastal","any":"🌐 Any","skip":"—"}}[g.map_type]||g.map_type||"";
    card.innerHTML=`
      <div class="game-header" onclick="toggleGame('hist-${{gi}}')">
        <div>
          <div class="game-title">${{g.label}} · ${{g.winner_name}} won${{g.winner_civ?" ("+g.winner_civ+")":""}}</div>
          <div class="game-meta">${{g.date}} · ${{g.type}} · ${{g.difficulty}} · ${{mapLabel}} ${{vBadge}}</div>
        </div>
        <span class="chevron" id="chev-hist-${{gi}}">▼</span>
      </div>
      <div class="game-body" id="hist-${{gi}}">
        ${{g.players.map((p,pi)=>{{
          const diff=p.elo_after-p.elo_before;
          const sign=diff>=0?"+":"";
          const cls=diff>=0?"pos":"neg";
          const medal=["🥇","🥈","🥉"][pi]||"#"+(pi+1);
          const pool=(g.draft_pools&&g.draft_pools[p.id])||[];
          const poolHtml=pool.length?`<div class="pool-label">DRAFT POOL</div><div class="pool-civs">${{pool.map(c=>`<span class="pool-civ ${{c===p.civ?"chosen":""}}">${{c}}</span>`).join("")}}</div>`:"";
          return`<div class="player-row"><span style="width:22px;text-align:center">${{medal}}</span><div style="flex:1;font-weight:600">${{p.name}}</div><span class="badge">${{p.civ||"?"}}</span><span class="hist-elo ${{cls}}" style="min-width:60px;text-align:right">${{p.elo_after}} (${{sign}}${{diff}})</span></div>${{poolHtml}}`;
        }}).join("")}}
      </div>`;
    card.querySelector(".game-header").addEventListener("dblclick", ()=>{{
      switchTab("stats");
      setTimeout(()=>highlightGame(g.label),100);
    }});
    el.appendChild(card);
  }});
}}

function showHistoryGame(idx) {{
  switchTab("history");
  setTimeout(()=>{{
    const gi=HISTORY.findIndex(h=>h.idx===idx);
    if(gi>=0){{const body=document.getElementById("hist-"+gi);if(body){{body.classList.add("open");document.getElementById("chev-hist-"+gi).classList.add("open");body.scrollIntoView({{behavior:"smooth",block:"center"}});}}}}
  }},150);
}}

function highlightGame(label) {{
  if(!eloChart) return;
  const idx=TIMELINE.findIndex(t=>t.label===label);
  if(idx<0) return;
  eloChart.tooltip.setActiveElements(PLAYERS.map((_,di)=>{{return{{datasetIndex:di,index:idx}}}}),{{x:0,y:0}});
  eloChart.update();
}}

function toggleGame(id) {{
  const body=document.getElementById(id);
  const chev=document.getElementById("chev-"+id);
  body.classList.toggle("open");
  chev.classList.toggle("open");
}}

// ── Auth ─────────────────────────────────────────────────────────────────────
const authArea = document.getElementById("authArea");
const guild = new URLSearchParams(window.location.search).get("guild") || GUILD_ID || "";

if (LOGGED_IN_ID && LOGGED_IN_NAME) {{
  const displayLabel = DISPLAY_NAME || LOGGED_IN_NAME;
  const favLabel = FAV_CIV ? ` · ${{FAV_CIV}}` : "";
  authArea.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px">
      <button class="btn btn-ghost" onclick="openSettingsModal()" style="font-size:10px;padding:4px 10px">👤 ${{displayLabel}}${{favLabel}}</button>
      <a href="/logout" style="font-size:10px;color:#475569;text-decoration:none;padding:4px 10px;border:1px solid #1e2130;border-radius:6px">logout</a>
    </div>`;

  // Show history filter
  const hf = document.getElementById("histFilter");
  if (hf) hf.style.display = "flex";

  // Auto-open this player's profile
  const myIdx = PLAYERS.findIndex(p => p.id === LOGGED_IN_ID);
  if (myIdx >= 0) {{
    setTimeout(() => {{
      document.querySelectorAll(".player-btn").forEach(b => b.classList.remove("profile-active"));
      const btns = document.querySelectorAll(".player-btn");
      if (btns[myIdx]) btns[myIdx].classList.add("profile-active");
      activeProfile = PLAYERS[myIdx].id;
      showProfile(PLAYERS[myIdx], myIdx);
    }}, 200);
  }}
}} else {{
  authArea.innerHTML = `<a href="/login?guild=${{guild}}" style="font-size:11px;color:#f97316;text-decoration:none;padding:5px 12px;border:1px solid #f97316;border-radius:6px;transition:opacity 0.15s" onmouseover="this.style.opacity=0.7" onmouseout="this.style.opacity=1">Login with Discord</a>`;
}}

// ── Settings Modal ─────────────────────────────────────────────────────────
function openSettingsModal() {{
  const mc = document.getElementById("modalContainer");
  mc.innerHTML = `
    <div class="modal-overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="modal-title">⚙️ My Settings</div>
        <label class="form-label">DISPLAY NAME</label>
        <input class="form-input" id="settingName" placeholder="Your display name" value="${{DISPLAY_NAME || LOGGED_IN_NAME}}">
        <label class="form-label">FAVOURITE CIV</label>
        <select class="form-select" id="settingCiv">
          <option value="">— None —</option>
          ${{ALL_CIVS.map(c => `<option value="${{c}}"${{c===FAV_CIV?" selected":""}}>${{c}}</option>`).join("")}}
        </select>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
          <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" onclick="saveSettings()">Save</button>
        </div>
      </div>
    </div>`;
}}

async function saveSettings() {{
  const name = document.getElementById("settingName").value.trim();
  const civ  = document.getElementById("settingCiv").value;
  const res = await fetch("/api/prefs", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, display_name: name, fav_civ: civ}})
  }});
  if (res.ok) {{ closeModal(); location.reload(); }}
  else {{ alert("Failed to save settings."); }}
}}

// ── Lobby Modal ────────────────────────────────────────────────────────────
function openLobbyModal() {{
  if (!LOGGED_IN_ID) {{ alert("Please log in to create a lobby."); return; }}
  const mc = document.getElementById("modalContainer");
  mc.innerHTML = `
    <div class="modal-overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="modal-title">🏛️ Create Lobby</div>
        <label class="form-label">DIFFICULTY</label>
        <select class="form-select" id="lobbyDiff">
          <option value="Prince">Prince</option>
          <option value="King">King</option>
        </select>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
          <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" onclick="createLobby()">Create</button>
        </div>
      </div>
    </div>`;
}}

async function createLobby() {{
  const difficulty = document.getElementById("lobbyDiff").value;
  const res = await fetch("/api/lobby/create", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, difficulty}})
  }});
  if (res.ok) {{ closeModal(); switchTab("live"); location.reload(); }}
  else {{ alert("Failed to create lobby."); }}
}}

async function joinLobby(hostId) {{
  if (!LOGGED_IN_ID) {{ alert("Please log in to join a lobby."); return; }}
  const res = await fetch("/api/lobby/join", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId}})
  }});
  if (res.ok) {{ location.reload(); }}
  else {{ const t = await res.text(); alert("Could not join: " + t); }}
}}

async function leaveLobby(hostId) {{
  const res = await fetch("/api/lobby/leave", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId}})
  }});
  if (res.ok) {{ location.reload(); }}
  else {{ alert("Failed to leave lobby."); }}
}}

function closeModal() {{ document.getElementById("modalContainer").innerHTML = ""; }}

// ── History filter ─────────────────────────────────────────────────────────
let historyFilter = "all";
function filterHistory(mode) {{
  historyFilter = mode;
  document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
  event.target.classList.add("active");
  buildHistory();
}}
</script>
</body>
</html>"""


async def handle_graph(request):
    guild_id = request.query.get("guild")
    if not guild_id:
        return web.Response(text="<h2>Missing ?guild= parameter</h2>", content_type="text/html", status=400)
    # Read session cookie
    session_token = request.cookies.get("session")
    logged_in_id = logged_in_name = None
    if session_token:
        logged_in_id, logged_in_name = verify_session_token(session_token)
    html = build_graph_html(guild_id, logged_in_id, logged_in_name)
    return web.Response(text=html, content_type="text/html")

async def handle_login(request):
    guild_id = request.query.get("guild", "")
    if not DISCORD_CLIENT_ID:
        return web.Response(text="OAuth not configured — set DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET.", status=500)
    redirect_uri = f"{PUBLIC_URL}/callback"
    state = f"{guild_id}:{secrets.token_urlsafe(16)}"
    url = (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={OAUTH_SCOPES}"
        f"&state={state}"
    )
    return web.HTTPFound(url)

async def handle_callback(request):
    code = request.query.get("code")
    state = request.query.get("state", ":")
    guild_id = state.split(":")[0]
    if not code:
        return web.Response(text="OAuth error — no code returned.", status=400)
    redirect_uri = f"{PUBLIC_URL}/callback"
    # Exchange code for token
    async with ClientSession() as session:
        async with session.post("https://discord.com/api/oauth2/token", data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }) as resp:
            token_data = await resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return web.Response(text="OAuth error — could not get access token.", status=400)
        async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
            user_data = await resp.json()
    user_id = user_data.get("id", "")
    username = user_data.get("username", "unknown")
    session_token = make_session_token(user_id, username)
    response = web.HTTPFound(f"/graph?guild={guild_id}")
    response.set_cookie("session", session_token, max_age=60*60*24*30, httponly=True, samesite="Lax")
    return response

async def handle_logout(request):
    response = web.HTTPFound(request.headers.get("Referer", "/"))
    response.del_cookie("session")
    return response

async def handle_data(request):
    all_data = load_all_data()
    return web.Response(text=json.dumps(all_data), content_type="application/json")

async def handle_api_prefs(request):
    """Save display name and favourite civ for logged-in user."""
    session_token = request.cookies.get("session")
    if not session_token:
        return web.Response(text="Not logged in", status=401)
    user_id, _ = verify_session_token(session_token)
    if not user_id:
        return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    display_name = body.get("display_name", "").strip()[:32]
    fav_civ = body.get("fav_civ", "")
    if fav_civ and fav_civ not in ALL_CIVS:
        return web.Response(text="Invalid civ", status=400)
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    if user_id not in data["players"]:
        return web.Response(text="Player not found", status=404)
    data["players"][user_id].setdefault("prefs", {})
    if display_name:
        data["players"][user_id]["prefs"]["display_name"] = display_name
        data["players"][user_id]["name"] = display_name
    data["players"][user_id]["prefs"]["fav_civ"] = fav_civ
    save_all_data(all_data)
    return web.Response(text="OK")

async def handle_api_lobby_create(request):
    """Create a lobby from the website."""
    session_token = request.cookies.get("session")
    if not session_token:
        return web.Response(text="Not logged in", status=401)
    user_id, username = verify_session_token(session_token)
    if not user_id:
        return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    difficulty = body.get("difficulty", "Prince")
    if difficulty not in ("Prince", "King"):
        return web.Response(text="Invalid difficulty", status=400)
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    if user_id in data["lobbies"]:
        return web.Response(text="You already have an open lobby", status=400)
    if player_in_any_lobby(data, user_id):
        return web.Response(text="You are already in a lobby", status=400)
    if player_in_active_game(data, user_id):
        return web.Response(text="You are in an active game", status=400)
    display_name = data["players"].get(user_id, {}).get("name", username)
    get_player(data, user_id, display_name)
    data["lobbies"][user_id] = {
        "host": user_id, "host_name": display_name,
        "players": [user_id], "player_names": [display_name],
        "difficulty": difficulty, "created_at": datetime.utcnow().isoformat()
    }
    save_all_data(all_data)
    return web.Response(text="OK")

async def handle_api_lobby_join(request):
    """Join a lobby from the website."""
    session_token = request.cookies.get("session")
    if not session_token:
        return web.Response(text="Not logged in", status=401)
    user_id, username = verify_session_token(session_token)
    if not user_id:
        return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    host_id = body.get("host_id", "")
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    if host_id not in data["lobbies"]:
        return web.Response(text="Lobby not found", status=404)
    lobby = data["lobbies"][host_id]
    if user_id in lobby["players"]:
        return web.Response(text="Already in lobby", status=400)
    if player_in_any_lobby(data, user_id):
        return web.Response(text="Already in another lobby", status=400)
    if player_in_active_game(data, user_id):
        return web.Response(text="Already in an active game", status=400)
    if len(lobby["players"]) >= MAX_LOBBY_SIZE:
        return web.Response(text="Lobby full", status=400)
    display_name = data["players"].get(user_id, {}).get("name", username)
    get_player(data, user_id, display_name)
    lobby["players"].append(user_id)
    lobby["player_names"].append(display_name)
    save_all_data(all_data)
    return web.Response(text="OK")

async def handle_api_lobby_leave(request):
    """Leave a lobby from the website."""
    session_token = request.cookies.get("session")
    if not session_token:
        return web.Response(text="Not logged in", status=401)
    user_id, _ = verify_session_token(session_token)
    if not user_id:
        return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    host_id = body.get("host_id", "")
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    if host_id not in data["lobbies"]:
        return web.Response(text="Lobby not found", status=404)
    lobby = data["lobbies"][host_id]
    if user_id not in lobby["players"]:
        return web.Response(text="Not in lobby", status=400)
    if user_id == host_id:
        del data["lobbies"][host_id]
    else:
        idx = lobby["players"].index(user_id)
        lobby["players"].pop(idx)
        lobby["player_names"].pop(idx)
    save_all_data(all_data)
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/graph", handle_graph)
    app.router.add_get("/login", handle_login)
    app.router.add_get("/callback", handle_callback)
    app.router.add_get("/logout", handle_logout)
    app.router.add_get("/data", handle_data)
    app.router.add_post("/api/prefs", handle_api_prefs)
    app.router.add_post("/api/lobby/create", handle_api_lobby_create)
    app.router.add_post("/api/lobby/join", handle_api_lobby_join)
    app.router.add_post("/api/lobby/leave", handle_api_lobby_leave)
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
@app_commands.describe(difficulty="The AI difficulty level being played")
@app_commands.choices(difficulty=[
    app_commands.Choice(name="Prince", value="Prince"),
    app_commands.Choice(name="King",   value="King"),
])
async def open_lobby(interaction: discord.Interaction, difficulty: str = "Prince"):
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
        "difficulty": difficulty,
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
        "picks": {},
        "difficulty": lobby.get("difficulty", "Prince"),
        "map_type": map_type,
    }

    del data["lobbies"][host_id]
    save_all_data(all_data)

    map_label = {"land": "Land", "coastal": "Coastal", "any": "Any"}.get(map_type, "Any")
    difficulty = lobby.get("difficulty", "Prince")

    # Build draft display — one field per player
    embed = discord.Embed(
        title=f"⚔️  {len(lobby['players'])}-Player Game — Civ Draft ({map_label} · {difficulty})",
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

    # Must pick from their own draft (case-insensitive)
    player_draft = draft.get(caller_id, [])
    matched_civ = next((c for c in player_draft if c.lower() == civ.lower()), None)
    if not matched_civ:
        options = " · ".join(f"`{c}`" for c in player_draft)
        await interaction.response.send_message(
            f"❌ **{civ}** is not in your draft. Your options are:\n{options}", ephemeral=True)
        return
    civ = matched_civ  # use correctly cased name

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
    victory_type="How the winner won",
)
@app_commands.choices(victory_type=[
    app_commands.Choice(name="Domination", value="Domination"),
    app_commands.Choice(name="Science",    value="Science"),
    app_commands.Choice(name="Culture",    value="Culture"),
    app_commands.Choice(name="Diplomatic", value="Diplomatic"),
])
async def report_results(
    interaction: discord.Interaction,
    first: discord.Member, second: discord.Member,
    third: Optional[discord.Member] = None, fourth: Optional[discord.Member] = None,
    fifth: Optional[discord.Member] = None, sixth: Optional[discord.Member] = None,
    seventh: Optional[discord.Member] = None, eighth: Optional[discord.Member] = None,
    victory_type: Optional[str] = None,
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

    group = game_groups.get(game_id, {})
    difficulty = group.get("difficulty", "Prince")

    game_groups.pop(game_id, None)
    data["active_games"] = active_games
    data["game_groups"] = game_groups
    # Save draft pools alongside match for history display
    draft_pools = {}
    if group.get("draft"):
        draft_pools = group["draft"]

    data["matches"].append({
        "type": f"{len(members)}-player",
        "difficulty": difficulty,
        "map_type": group.get("map_type", "any"),
        "victory_type": victory_type,
        "draft_pools": draft_pools,
        "players": [
            {"id": info["id"], "finish": info["finish"], "civ": info["civ"],
             "elo_before": info["old_elo"], "elo_after": new_elos[i]}
            for i, info in enumerate(player_info)
        ],
        "played_at": datetime.utcnow().isoformat()
    })

    save_all_data(all_data)

    victory_icons = {"Domination": "⚔️", "Science": "🚀", "Culture": "🎭", "Diplomatic": "🕊️"}
    victory_str = f"{victory_icons.get(victory_type, '')} {victory_type} Victory" if victory_type else "Victory"

    embed = discord.Embed(
        title=f"🏛️  {len(members)}-Player Match Recorded!",
        description="\n".join(result_lines),
        color=0xD4A017
    )
    embed.add_field(name="Victory", value=victory_str, inline=True)
    embed.add_field(name="Difficulty", value=difficulty, inline=True)
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
