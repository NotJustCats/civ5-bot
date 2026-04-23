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
            ps.append({"id": pid, "name": name, "chosen": chosen, "pool": pool})
        live_games.append({
            "host": host_name,
            "host_id": game_id,
            "difficulty": group.get("difficulty", "Prince"),
            "map_type": group.get("map_type", "any"),
            "players": ps,
        })
    # Also include open lobbies
    for host_id, lobby in lobbies.items():
        lobby_player_ids = lobby.get("players", [])
        lobby_player_names = lobby.get("player_names", [])
        live_games.append({
            "host": lobby.get("host_name", "?"),
            "host_id": host_id,
            "difficulty": lobby.get("difficulty", "Prince"),
            "status": "lobby",
            "players": [{"id": pid, "name": name, "chosen": None, "pool": []} for pid, name in zip(lobby_player_ids, lobby_player_names)],
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
    prefs = players.get(logged_in_id or "", {}).get("prefs", {}) if logged_in_id else {}
    display_name_json = _json.dumps(prefs.get("display_name", logged_in_name or ""))
    fav_civ_json = _json.dumps(prefs.get("fav_civ", ""))
    guild_id_json = _json.dumps(guild_id)
    # Build per-player prefs for display
    player_prefs_json = _json.dumps({pid: p.get("prefs", {}) for pid, p in players.items()})
    my_prefs = players.get(logged_in_id or "", {}).get("prefs", {}) if logged_in_id else {}
    # civ_wins: how many times this player has won with each civ
    my_civ_wins = {}
    for pid, p in players.items():
        if pid == (logged_in_id or ""):
            for civ_name, civ_stats in p.get("civs", {}).items():
                my_civ_wins[civ_name] = civ_stats.get("wins", 0)
    civ_wins_json = _json.dumps(my_civ_wins)
    # Card tiers derived from civ wins: 1win=bronze, 3=silver, 6=gold, 10=diamond
    def wins_to_tier(w):
        if w >= 4: return "diamond"
        if w >= 3: return "gold"
        if w >= 2: return "silver"
        if w >= 1: return "bronze"
        return "normal"
    auto_card_tiers = {civ: wins_to_tier(w) for civ, w in my_civ_wins.items()}
    card_tiers_json = _json.dumps(auto_card_tiers)

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
  /* Global custom scrollbars — thin, non-overlaying */
  * {{ scrollbar-width: thin; scrollbar-color: #2a3040 transparent; }}
  *::-webkit-scrollbar {{ width: 4px; height: 4px; }}
  *::-webkit-scrollbar-track {{ background: transparent; }}
  *::-webkit-scrollbar-thumb {{ background: #2a3040; border-radius: 4px; }}
  *::-webkit-scrollbar-thumb:hover {{ background: #f97316; }}
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
  .scroll-page {{ flex: 1; overflow-y: auto; min-height: 0; padding-right: 2px; }}
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
  /* Player cards */
  .player-cards-row {{ display: flex; gap: 8px; flex-shrink: 0; overflow-x: auto; padding-bottom: 2px; }}
  .player-cards-row::-webkit-scrollbar {{ height: 3px; }}
  .player-cards-row::-webkit-scrollbar-track {{ background: #1e2130; }}
  .player-cards-row::-webkit-scrollbar-thumb {{ background: #f97316; border-radius: 2px; }}
  .pcard {{ background: #0d1017; border-radius: 10px; padding: 8px 14px; position: relative; overflow: hidden; transition: transform 0.2s, box-shadow 0.2s; cursor: pointer; flex-shrink: 0; border: 1px solid #1e2130; min-width: 130px; }}
  .pcard:hover, .pcard.profile-active {{ transform: translateY(-2px); }}
  .pcard.profile-active {{ box-shadow: 0 0 0 1px currentColor; }}
  .pcard-glow {{ position: absolute; bottom: 0; left: 0; right: 0; height: 60%; opacity: 0.08; transition: opacity 0.3s; border-radius: 0 0 10px 10px; }}
  .pcard:hover .pcard-glow, .pcard.profile-active .pcard-glow {{ opacity: 0.2; }}
  .pcard-rank {{ font-size: 9px; letter-spacing: 1px; margin-bottom: 3px; opacity: 0.7; }}
  .pcard-name {{ font-weight: 700; font-size: 12px; margin-bottom: 2px; }}
  .pcard-elo  {{ font-size: 18px; font-weight: 700; }}
  .pcard-sub  {{ font-size: 9px; color: #475569; margin-top: 3px; }}
  /* Rank strip */
  .rank-strip {{ display: flex; gap: 5px; flex-wrap: wrap; flex-shrink: 0; margin-top: 8px; padding-top: 8px; border-top: 1px solid #1e2130; }}
  .rank-badge {{ display: flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 20px; font-size: 9px; letter-spacing: 1px; border: 1px solid; opacity: 0.35; transition: opacity 0.3s; }}
  .rank-badge.active {{ opacity: 1; }}
  /* Panel switcher */
  .panel-nav {{ display: flex; align-items: center; gap: 6px; }}
  .panel-arrow {{ background: transparent; border: none; color: #475569; font-size: 18px; cursor: pointer; padding: 0 2px; transition: color 0.15s; line-height: 1; font-family: inherit; }}
  .panel-arrow:hover {{ color: #e2e8f0; }}
  /* H2H */
  .h2h-scroll {{ flex: 1; overflow: auto; min-height: 0; }}
  .h2h-table {{ border-collapse: collapse; width: 100%; }}
  .h2h-table th, .h2h-table td {{ padding: 5px 8px; font-size: 10px; text-align: center; border: 1px solid #1e2130; white-space: nowrap; }}
  .h2h-table th {{ color: #475569; font-size: 9px; letter-spacing: 1px; background: #080a0f; position: sticky; top: 0; }}
  .h2h-table td.h2h-win  {{ background: #0c2010; color: #22c55e; font-weight: 700; }}
  .h2h-table td.h2h-loss {{ background: #200c0c; color: #ef4444; font-weight: 700; }}
  .h2h-table td.h2h-even {{ background: #1a1a0c; color: #eab308; }}
  .h2h-table td.h2h-self {{ background: #080a0f; color: #1e2130; }}
  .h2h-name {{ font-weight: 600; color: #e2e8f0; text-align: left !important; font-size: 10px; }}
  /* Toasts */
  .toast-container {{ position: fixed; bottom: 20px; right: 20px; display: flex; flex-direction: column; gap: 10px; z-index: 999; pointer-events: none; }}
  .toast {{ display: flex; align-items: center; gap: 14px; background: #0d1017; border: 1px solid #1e2130; border-radius: 10px; padding: 14px 16px; width: 290px; position: relative; overflow: hidden; pointer-events: all; transform: translateX(320px); transition: transform 0.4s cubic-bezier(0.4,0,0.2,1); }}
  .toast.show {{ transform: translateX(0); }}
  .toast-bar {{ position: absolute; left: 0; top: 0; bottom: 0; width: 3px; border-radius: 10px 0 0 10px; }}
  .toast-shine {{ position: absolute; inset: 0; background: linear-gradient(90deg,transparent,rgba(255,255,255,0.03),transparent); animation: tshine 2s ease infinite; }}
  @keyframes tshine {{ 0%{{transform:translateX(-100%)}} 100%{{transform:translateX(100%)}} }}
  .toast-icon {{ font-size: 24px; flex-shrink: 0; }}
  .toast-label {{ font-size: 9px; color: #64748b; letter-spacing: 2px; margin-bottom: 3px; }}
  .toast-name  {{ font-family: 'Cinzel', serif; font-size: 13px; font-weight: 700; color: #e2e8f0; }}
  .toast-desc  {{ font-size: 10px; color: #475569; margin-top: 2px; }}
  .toast-prog  {{ position: absolute; bottom: 0; left: 0; height: 2px; border-radius: 0 0 0 10px; animation: tcountdown 4s linear forwards; }}
  @keyframes tcountdown {{ from{{width:100%}} to{{width:0%}} }}
  .host-section {{ background: #0d1017; border: 1px solid #1e2130; border-radius: 12px; padding: 20px; margin-bottom: 14px; }}
  .host-section-title {{ font-size: 11px; color: #64748b; letter-spacing: 2px; margin-bottom: 14px; }}
  .player-card {{ display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: #080a0f; border: 1px solid #1e2130; border-radius: 8px; margin-bottom: 8px; }}
  .player-card-name {{ font-weight: 600; font-size: 13px; flex: 1; }}
  .civ-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; margin-top: 8px; }}
  .civ-option {{ padding: 8px 12px; border-radius: 8px; border: 1px solid #1e2130; background: #080a0f; color: #94a3b8; font-family: IBM Plex Mono, monospace; font-size: 11px; cursor: pointer; text-align: center; transition: all 0.15s; }}
  .civ-option:hover {{ border-color: #f97316; color: #f97316; }}
  /* Draft civ tooltip */
  .civ-tooltip {{ position: fixed; z-index: 9999; background: #0d1017; border: 1px solid #2a3040; border-radius: 10px; padding: 12px 14px; width: 240px; pointer-events: none; opacity: 0; transition: opacity 0.15s; box-shadow: 0 8px 32px rgba(0,0,0,0.6); }}
  .civ-tooltip.visible {{ opacity: 1; }}
  .civ-tooltip-name {{ font-family: 'Cinzel', serif; font-size: 13px; font-weight: 700; color: #e2e8f0; margin-bottom: 2px; }}
  .civ-tooltip-leader {{ font-size: 9px; color: #475569; margin-bottom: 8px; letter-spacing: 1px; }}
  .civ-tooltip-row {{ margin-bottom: 7px; }}
  .civ-tooltip-type {{ font-size: 8px; letter-spacing: 2px; margin-bottom: 2px; }}
  .civ-tooltip-title {{ font-size: 11px; font-weight: 700; color: #e2e8f0; margin-bottom: 2px; }}
  .civ-tooltip-desc {{ font-size: 9px; color: #64748b; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }}
  .civ-option.picked {{ border-color: #22c55e; color: #22c55e; background: #0c2010; }}
  .finish-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .finish-medal {{ font-size: 18px; width: 28px; text-align: center; flex-shrink: 0; }}
  /* Civilopedia grid — 5 per row */
  .civ-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; padding-bottom: 12px; }}
  /* 3D tilt card — playing card ratio 5:7 */
  .civ-tile {{
    background: #0d1017; border: 1px solid #1e2130; border-radius: 12px; padding: 14px 12px;
    cursor: pointer; position: relative; overflow: hidden;
    aspect-ratio: 5 / 7; display: flex; flex-direction: column; justify-content: flex-start;
    transform-style: preserve-3d; transform: perspective(600px) rotateX(0deg) rotateY(0deg);
    transition: transform 0.08s ease, border-color 0.2s, box-shadow 0.2s;
    will-change: transform;
  }}
  .civ-tile:hover {{ border-color: #2a3040; box-shadow: 0 16px 48px rgba(0,0,0,0.5); }}
  /* Tier backgrounds */
  .tier-bronze {{ background: linear-gradient(160deg, #1a0e05 0%, #0d0805 40%, #1a1005 100%); border-color: #7c4a1e; }}
  .tier-bronze:hover {{ border-color: #c8762e; box-shadow: 0 16px 48px rgba(160,80,20,0.35); }}
  .tier-silver {{ background: linear-gradient(160deg, #111418 0%, #0a0d10 40%, #111318 100%); border-color: #4a5568; }}
  .tier-silver:hover {{ border-color: #8fa3b8; box-shadow: 0 16px 48px rgba(100,140,180,0.25); }}
  .tier-gold {{ background: linear-gradient(160deg, #1a1500 0%, #0d0e00 40%, #1a1200 100%); border-color: #8a6a00; }}
  .tier-gold:hover {{ border-color: #d4a500; box-shadow: 0 16px 48px rgba(200,160,0,0.35); }}
  .tier-diamond {{ background: linear-gradient(160deg, #020d1a 0%, #010810 40%, #020a18 100%); border-color: #0a4a6e; }}
  .tier-diamond:hover {{ border-color: #22d3ee; box-shadow: 0 16px 48px rgba(0,200,255,0.3); }}
  /* Tier badge on card */
  .tier-badge {{ position: absolute; bottom: 8px; right: 8px; font-size: 13px; pointer-events: none; }}
  /* Upgrade button */
  .upgrade-btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; border-radius: 8px; border: 1px solid; cursor: pointer; font-family: IBM Plex Mono, monospace; font-size: 11px; font-weight: 700; transition: opacity 0.15s; background: transparent; }}
  .upgrade-btn:hover {{ opacity: 0.8; }}
  .upgrade-btn:disabled {{ opacity: 0.35; cursor: not-allowed; }}
  .civ-tile.expanded {{
    border-color: #f97316; grid-column: 1 / -1;
    aspect-ratio: unset; justify-content: flex-start;
    transform: perspective(600px) rotateX(0deg) rotateY(0deg) !important;
    box-shadow: 0 0 0 1px #f97316;
  }}
  /* Hide card-only elements when expanded */
  .civ-tile.expanded .civ-card-content {{ display: none !important; }}
  /* Hide detail when collapsed */
  .civ-tile:not(.expanded) .civ-detail {{ display: none !important; }}
  /* Anime-style shine — single diagonal sweep band */
  .civ-tile-shine {{
    position: absolute; inset: 0; border-radius: 12px; pointer-events: none; opacity: 0;
    transition: opacity 0.15s;
    /* Two soft diagonal lines like light catching a flat surface */
    background:
      linear-gradient(115deg,
        transparent 20%,
        rgba(255,255,255,0.03) 38%,
        rgba(255,255,255,0.10) 42%,
        rgba(255,255,255,0.03) 46%,
        transparent 60%),
      linear-gradient(115deg,
        transparent 50%,
        rgba(255,255,255,0.02) 62%,
        rgba(255,255,255,0.06) 65%,
        rgba(255,255,255,0.02) 68%,
        transparent 80%);
  }}
  .civ-tile:not(.expanded):hover .civ-tile-shine {{ opacity: 1; }}
  /* Card content */
  .civ-tile-top {{ display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 6px; }}
  .civ-tile-map {{ font-size: 22px; line-height: 1; }}
  .civ-tile-played {{ font-size: 8px; color: #f97316; font-weight: 700; background: #1a0f00; border: 1px solid #f9731644; border-radius: 5px; padding: 1px 5px; }}
  .civ-tile-name {{ font-family: 'Cinzel', serif; font-size: 12px; font-weight: 700; color: #e2e8f0; margin-bottom: 1px; line-height: 1.2; }}
  .civ-tile-leader {{ font-size: 8px; color: #475569; margin-bottom: 6px; letter-spacing: 1px; }}
  .civ-tile-ability {{ font-size: 8px; color: #94a3b8; margin-bottom: 7px; line-height: 1.4; padding: 4px 7px; background: #080a0f; border-radius: 5px; border-left: 2px solid #f97316; }}
  .civ-tile-divider {{ height: 1px; background: #1e2130; margin: 6px 0; }}
  .civ-tile-tags {{ display: flex; flex-wrap: wrap; gap: 3px; }}
  .civ-tile-tag {{ font-size: 7px; padding: 1px 5px; border-radius: 4px; line-height: 1.6; }}
  .civ-tile-desc {{ font-size: 7px; color: #475569; line-height: 1.5; margin-top: 3px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  /* Expanded detail */
  .civ-detail {{ display: none; margin-top: 16px; padding-top: 16px; border-top: 1px solid #1e2130; }}
  .civ-tile.expanded .civ-detail {{ display: block; }}
  .civ-detail-header {{ display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: 14px; gap: 12px; }}
  .civ-detail-title {{ font-family: 'Cinzel', serif; font-size: 18px; font-weight: 700; color: #e2e8f0; }}
  .civ-detail-leader {{ font-size: 10px; color: #64748b; margin-top: 3px; }}
  .civ-detail-stats {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }}
  .civ-detail-stat {{ background: #080a0f; border: 1px solid #1e2130; border-radius: 8px; padding: 8px 12px; text-align: center; flex: 1; min-width: 60px; }}
  .civ-detail-stat-val {{ font-size: 14px; font-weight: 700; color: #f97316; }}
  .civ-detail-stat-label {{ font-size: 8px; color: #475569; margin-top: 1px; letter-spacing: 1px; }}
  .civ-section {{ margin-bottom: 10px; padding: 12px; background: #080a0f; border: 1px solid #1e2130; border-radius: 8px; }}
  .civ-section-type {{ font-size: 9px; letter-spacing: 2px; margin-bottom: 3px; }}
  .civ-section-name {{ font-size: 12px; font-weight: 700; color: #e2e8f0; margin-bottom: 5px; }}
  .civ-section-desc {{ font-size: 10px; color: #94a3b8; line-height: 1.7; }}
  .civ-section-desc span {{ color: #64748b; }}
  .civ-bias {{ display: inline-block; padding: 2px 8px; border-radius: 12px; background: #1e2130; color: #475569; font-size: 9px; margin-top: 4px; }}
  .civ-suggest {{ padding: 10px 14px; cursor: pointer; font-size: 12px; border-bottom: 1px solid #1e2130; transition: background 0.1s; }}
  .civ-suggest:hover {{ background: #1e2130; }}
  .civ-suggest-name {{ color: #e2e8f0; font-weight: 600; }}
  .civ-suggest-leader {{ color: #475569; font-size: 10px; margin-top: 2px; }}
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
  <div class="tab" id="hostTab" onclick="switchTab('host')" style="display:none">HOST GAME</div>
  <div class="tab" onclick="switchTab('civpedia')">CIVILOPEDIA</div>
</div>

<!-- STATS PAGE -->
<div class="page active" id="page-stats">
  <div style="display:flex;align-items:center;justify-content:space-between;flex-shrink:0;margin-bottom:8px">
    <div class="player-cards-row" id="playerGrid"></div>
    <label class="rank-toggle" style="flex-shrink:0;margin-left:12px"><input type="checkbox" id="rankToggle" checked onchange="toggleRanks()"> RANK LINES</label>
  </div>
  <div class="main-grid">
    <div class="card card-elo" style="gap:0">
      <p class="card-title" style="margin-bottom:6px">ELO PROGRESSION OVER TIME</p>
      <div class="chart-wrap" style="flex:1"><canvas id="eloChart"></canvas></div>
      <div class="rank-strip" id="rankStrip">
        <div class="rank-badge" id="rb-deity"     style="color:#f97316;border-color:#f97316">🏆 DEITY 1600+</div>
        <div class="rank-badge" id="rb-emperor"   style="color:#a855f7;border-color:#a855f7">⚔️ EMPEROR 1400</div>
        <div class="rank-badge" id="rb-king"      style="color:#06b6d4;border-color:#06b6d4">🛡️ KING 1250</div>
        <div class="rank-badge" id="rb-prince"    style="color:#3b82f6;border-color:#3b82f6">⚙️ PRINCE 1100</div>
        <div class="rank-badge" id="rb-chieftain" style="color:#22c55e;border-color:#22c55e">🌿 CHIEFTAIN 1000</div>
        <div class="rank-badge" id="rb-settler"   style="color:#78716c;border-color:#78716c">🪨 SETTLER</div>
      </div>
    </div>
    <div class="card" id="pieCard">
      <div class="card-title">
        <div class="panel-nav">
          <button class="panel-arrow" onclick="prevPanel()">‹</button>
          <span id="panelLabel">MOST PLAYED CIVS</span>
          <button class="panel-arrow" onclick="nextPanel()">›</button>
        </div>
      </div>
      <div class="chart-wrap" id="pieWrap"><canvas id="pieChart"></canvas></div>
      <div class="h2h-scroll" id="h2hWrap" style="display:none">
        <table class="h2h-table" id="h2hTable"></table>
      </div>
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
<div class="toast-container" id="toastContainer"></div>

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
<!-- CIVILOPEDIA PAGE -->
<div class="page" id="page-civpedia">
  <div style="display:flex;flex-direction:column;height:100%;min-height:0;gap:10px">
    <div style="flex-shrink:0;position:relative">
      <input id="civSearch" class="form-input" placeholder="🔍  Search civilizations..." autocomplete="off"
        style="margin-bottom:0;font-size:13px;padding:10px 16px;border-radius:10px"
        oninput="onCivSearch(this.value)" onfocus="showAllSuggestions()">
      <div id="civSuggestions" style="position:absolute;top:100%;left:0;right:0;background:#0d1017;border:1px solid #1e2130;border-top:none;border-radius:0 0 10px 10px;z-index:50;display:none;max-height:200px;overflow-y:auto"></div>
    </div>
    <div class="scroll-page" style="flex:1">
      <div class="civ-grid" id="civGrid"></div>
    </div>
  </div>
</div>

<!-- HOST GAME PAGE -->
<div class="page" id="page-host">
  <div class="scroll-page" id="hostContent"></div>
</div>
<div id="civTooltip" class="civ-tooltip"></div>
<div id="modalContainer"></div>
<div id="easterEgg" style="display:none;position:fixed;inset:0;background:#080a0f;z-index:500;overflow-y:auto;padding:40px">
  <div style="max-width:900px;margin:0 auto">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <div>
        <h1 style="font-family:Cinzel,serif;font-size:24px;color:#f97316;letter-spacing:4px;margin-bottom:4px">✦ SECRET LAB ✦</h1>
        <p style="font-size:10px;color:#334155;letter-spacing:2px">YOU FOUND THE EASTER EGG · CARD TIER DEMO</p>
      </div>
      <button onclick="closeEgg()" style="background:transparent;border:1px solid #1e2130;color:#475569;border-radius:8px;padding:6px 14px;font-family:inherit;font-size:11px;cursor:pointer">✕ Close</button>
    </div>
    <p style="font-size:10px;color:#475569;margin-bottom:24px">All five card tiers shown as Rome. Click any card to expand.</p>
    <div id="eggCards" style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px"></div>
  </div>
</div>

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
const PLAYER_PREFS = {player_prefs_json};
const CIV_WINS = {civ_wins_json};
const CARD_TIERS = {card_tiers_json};
const PALETTE = ["#f97316","#3b82f6","#a855f7","#22c55e","#ef4444","#eab308","#06b6d4","#ec4899","#f43f5e","#10b981","#8b5cf6","#0ea5e9"];

const TITLES = [
  {{id:"settler",   label:"🪨 Settler",          req: null}},
  {{id:"chieftain", label:"🌿 Chieftain",         req: d => !!(d && ((d.wins||0)+(d.losses||0) >= 1))}},
  {{id:"sea_dog",   label:"⛵ Sea Dog",            req: d => !!(d && (d.coastal_games||0) >= 1)}},
  {{id:"landlubber",label:"🏕️ Landlubber",         req: d => !!(d && (d.land_games||0) >= 1)}},
  {{id:"explorer",  label:"🗺️ Explorer",           req: d => !!(d && (d.unique_civs||0) >= 10)}},
  {{id:"tactician", label:"⚔️ Tactician",          req: d => !!(d && (d.win_civs||0) >= 5)}},
  {{id:"polymath",  label:"🏛️ Polymath",           req: d => !!(d && (d.win_civs||0) >= 10)}},
  {{id:"dom_i",     label:"⚔️ Conqueror",          req: d => !!(d && d.victory_counts && (d.victory_counts.Domination||0) >= 1)}},
  {{id:"sci_i",     label:"🚀 Space Pioneer",      req: d => !!(d && d.victory_counts && (d.victory_counts.Science||0) >= 1)}},
  {{id:"cul_i",     label:"🎭 Patron of the Arts", req: d => !!(d && d.victory_counts && (d.victory_counts.Culture||0) >= 1)}},
  {{id:"dip_i",     label:"🕊️ Diplomat",           req: d => !!(d && d.victory_counts && (d.victory_counts.Diplomatic||0) >= 1)}},
  {{id:"prince",    label:"⚙️ Prince",             req: d => !!(d && (d.peak_elo||0) >= 1100)}},
  {{id:"king",      label:"🛡️ King",               req: d => !!(d && (d.peak_elo||0) >= 1250)}},
  {{id:"emperor",   label:"⚔️ Emperor",            req: d => !!(d && (d.peak_elo||0) >= 1400)}},
  {{id:"deity",     label:"🏆 Deity",              req: d => !!(d && (d.peak_elo||0) >= 1600)}},
  {{id:"grand_vic", label:"👥 Grand Victor",        req: d => !!(d && d.big_game_win)}},
  {{id:"full_house",label:"🎖️ Full House",          req: d => !!(d && d.played_8)}},
  {{id:"d_prince",  label:"👑 Prince (difficulty)", req: d => !!(d && d.difficulty_wins && (d.difficulty_wins.Prince||0) >= 1)}},
  {{id:"d_king",    label:"🏰 King Slayer",        req: d => !!(d && d.difficulty_wins && (d.difficulty_wins.King||0) >= 1)}},
  {{id:"trav",      label:"🌍 World Traveller",    req: d => !!(d && (d.unique_civs||0) >= 20)}},
];

function getPlayerColour(pid, fallbackIdx) {{
  const prefs = PLAYER_PREFS[pid] || {{}};
  return prefs.colour || PALETTE[fallbackIdx % PALETTE.length];
}}

function getPlayerTitle(pid) {{
  const prefs = PLAYER_PREFS[pid] || {{}};
  const chosen = prefs.title;
  if (!chosen) return "";
  const t = TITLES.find(t => t.id === chosen);
  return t ? t.label : "";
}}

function getPlayerFavCiv(pid) {{
  return (PLAYER_PREFS[pid] || {{}}).fav_civ || "";
}}

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
let _pollInterval = null;
function switchTab(name) {{
  document.querySelectorAll(".tab").forEach((t,i) => {{
    const names = ["stats","live","history","host","civpedia"];
    t.classList.toggle("active", names[i] === name);
  }});
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.getElementById("page-"+name).classList.add("active");
  if (name === "live") buildLive();
  if (name === "history") buildHistory();
  if (name === "host") buildHostPage();
  if (name === "civpedia") buildCivGrid("");
  // Auto-poll on live/host tabs
  clearInterval(_pollInterval);
  if (name === "live" || name === "host") {{
    _pollInterval = setInterval(async () => {{
      try {{
        const res = await fetch("/data");
        const all = await res.json();
        const serverData = all[GUILD_ID] || {{}};
        const groups = serverData.game_groups || {{}};
        const lobbies = serverData.lobbies || {{}};
        // Rebuild live data
        const newLive = [];
        for (const [gid, grp] of Object.entries(groups)) {{
          const picks = grp.picks || {{}};
          const draft = grp.draft || {{}};
          const ps = (grp.players||[]).map((pid,i) => ({{
            id: pid, name: (grp.player_names||[])[i]||pid,
            chosen: picks[pid]||null, pool: draft[pid]||[]
          }}));
          newLive.push({{host:(serverData.players||{{}})[gid]?.name||ps[0]?.name||"?",host_id:gid,difficulty:grp.difficulty||"Prince",map_type:grp.map_type||"any",players:ps}});
        }}
        for (const [hid, lob] of Object.entries(lobbies)) {{
          const pids = lob.players||[]; const pnames = lob.player_names||[];
          newLive.push({{host:lob.host_name||"?",host_id:hid,difficulty:lob.difficulty||"Prince",status:"lobby",map_type:"lobby",players:pids.map((pid,i)=>(({{id:pid,name:pnames[i]||pid,chosen:null,pool:[]}}))),}});
        }}
        LIVE_GAMES.length = 0;
        newLive.forEach(g => LIVE_GAMES.push(g));
        if (name === "live") buildLive();
        if (name === "host") buildHostPage();
  if (name === "civpedia") buildCivGrid("");
      }} catch(e) {{}}
    }}, 8000);
  }}
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
        borderColor: getPlayerColour(p.id, i),
        backgroundColor: getPlayerColour(p.id, i),
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
// ── Player cards ──────────────────────────────────────────────────────────────
PLAYERS.forEach((p,i) => {{
  const pCol = getPlayerColour(p.id, i);
  const medals = ["🥇","🥈","🥉"];
  const d = LB_DATA[p.id] || {{}};
  const wins = d.wins||0, losses = d.losses||0, total = wins+losses;
  const wr = total>0 ? Math.round(wins/total*100) : 0;
  const fav = getPlayerFavCiv(p.id);
  const title = getPlayerTitle(p.id);
  const card = document.createElement("div");
  card.className = "pcard";
  card.id = "pcard-"+p.id;
  card.style.color = pCol;
  card.innerHTML = `
    <div class="pcard-glow" style="background:radial-gradient(circle at 50% 100%,${{pCol}},transparent)"></div>
    <div class="pcard-rank" style="color:${{pCol}}88">${{rankLabel(p.finalElo)}}${{title?" · "+title:""}}</div>
    <div class="pcard-name" style="color:${{pCol}}">${{medals[i]||"#"+(i+1)}} ${{p.name}}</div>
    <div class="pcard-elo" style="color:${{pCol}}">${{p.finalElo}}</div>
    <div class="pcard-sub">${{wins}}W/${{losses}}L·${{wr}}%${{fav?" · ⭐"+fav:""}}</div>`;
  card.onclick = () => {{
    if (activeProfile === p.id) {{ activeProfile=null; card.classList.remove("profile-active"); hideProfile(); }}
    else {{ document.querySelectorAll(".pcard").forEach(b=>b.classList.remove("profile-active")); activeProfile=p.id; card.classList.add("profile-active"); showProfile(p,i); }}
  }};
  document.getElementById("playerGrid").appendChild(card);
}});

// Update rank badges based on player Elos
function updateRankBadges() {{
  const elos = PLAYERS.map(p => p.finalElo);
  const max = Math.max(...elos);
  const hasTiers = {{
    deity:    elos.some(e=>e>=1600),
    emperor:  elos.some(e=>e>=1400),
    king:     elos.some(e=>e>=1250),
    prince:   elos.some(e=>e>=1100),
    chieftain:elos.some(e=>e>=1000),
    settler:  elos.some(e=>e<1000),
  }};
  Object.entries(hasTiers).forEach(([tier,has]) => {{
    const el = document.getElementById("rb-"+tier);
    if (el) el.classList.toggle("active", has);
  }});
}}
updateRankBadges();

// ── Profile panel ─────────────────────────────────────────────────────────────
function hideProfile() {{ profileCard.style.display="none"; pieCard.style.display="flex"; lbCard.style.display="flex"; }}

function showProfile(p, idx) {{
  const color = getPlayerColour(p.id, idx);
  const pTitle = getPlayerTitle(p.id);
  const pFav = getPlayerFavCiv(p.id);
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
      <div>
        <div style="font-weight:700;font-size:16px;color:${{color}}">${{p.name}}${{pTitle?` <span style="font-size:11px;color:#64748b;font-weight:400">${{pTitle}}</span>`:""}}</div>
        <div style="font-size:11px;color:#94a3b8;margin-top:2px">${{rankLabel(p.finalElo)}} · ${{p.finalElo}} Elo${{pFav?` · ⭐ ${{pFav}}`:""}}</div>
      </div>
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

function closeProfile() {{ activeProfile=null; document.querySelectorAll(".pcard").forEach(b=>b.classList.remove("profile-active")); hideProfile(); }}

// ── Leaderboard ───────────────────────────────────────────────────────────────
function buildLeaderboard() {{
  const lbList=document.getElementById("lbList"); if(!lbList)return;
  const lbMedals=["🥇","🥈","🥉"];
  PLAYERS.forEach((p,i)=>{{
    const color=PALETTE[i%PALETTE.length],wins=LB_DATA[p.id]?.wins||0,losses=LB_DATA[p.id]?.losses||0;
    const wr=(wins+losses)>0?Math.round(wins/(wins+losses)*100):0;
    const row=document.createElement("div"); row.className="lb-row";
    const pColour = getPlayerColour(p.id, i);
    const pTitle = getPlayerTitle(p.id);
    const pFav = getPlayerFavCiv(p.id);
    row.innerHTML=`<span style="font-size:18px;width:28px;text-align:center">${{lbMedals[i]||"#"+(i+1)}}</span><div style="flex:1"><div style="font-weight:600;font-size:13px;color:${{pColour}}">${{p.name}}${{pTitle?` <span style="font-size:9px;color:#64748b">${{pTitle}}</span>`:""}}</div><div style="font-size:10px;color:#475569;margin-top:3px">${{wins}}W/${{losses}}L·${{wr}}%WR${{pFav?` · ${{pFav}}`:""}}</div></div><div style="text-align:right"><div style="font-weight:700;font-size:14px;color:#e2e8f0">${{p.finalElo}}</div><div style="font-size:10px;color:#475569;margin-top:2px">${{rankLabel(p.finalElo)}}</div></div>`;
    row.onclick=()=>{{if(activeProfile===p.id){{activeProfile=null;hideProfile();}}else{{activeProfile=p.id;document.querySelectorAll(".pcard").forEach(b=>b.classList.remove("profile-active"));const myCard=document.getElementById("pcard-"+p.id);if(myCard)myCard.classList.add("profile-active");showProfile(p,i);}}}};
    lbList.appendChild(row);
  }});
}}
buildLeaderboard();

// ── Pie chart + H2H panel switcher ───────────────────────────────────────────
const PANELS = ["MOST PLAYED CIVS","HEAD TO HEAD"];
let panelIdx = 0;

if (PIE_LABELS.length) {{
  new Chart(document.getElementById("pieChart").getContext("2d"),{{
    type:"doughnut",
    data:{{labels:PIE_LABELS,datasets:[{{data:PIE_VALUES,backgroundColor:PALETTE.concat(PALETTE),borderColor:"#080a0f",borderWidth:2,hoverOffset:6}}]}},
    options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{position:"right",labels:{{color:"#94a3b8",font:{{family:"IBM Plex Mono",size:9}},boxWidth:10,padding:8}}}},tooltip:{{backgroundColor:"#0f1117",borderColor:"#2a2d3a",borderWidth:1,titleColor:"#64748b",bodyColor:"#e2e8f0",titleFont:{{family:"IBM Plex Mono",size:11}},bodyFont:{{family:"IBM Plex Mono",size:12}},callbacks:{{label:ctx=>` ${{ctx.label}}: ${{ctx.parsed}} games`}}}}}}}}
  }});
}}

// Build H2H table from match history
(function buildH2H() {{
  const table = document.getElementById("h2hTable");
  if (!table || !PLAYERS.length) return;
  // Count head to head from HISTORY
  const h2h = {{}};
  PLAYERS.forEach(p => {{ h2h[p.id] = {{}}; PLAYERS.forEach(q => {{ if(p.id!==q.id) h2h[p.id][q.id]={{w:0,l:0}}; }}); }});
  HISTORY.forEach(g => {{
    const ps = g.players;
    for (let i=0; i<ps.length; i++) {{
      for (let j=0; j<ps.length; j++) {{
        if (i===j) continue;
        const a=ps[i], b=ps[j];
        if (!h2h[a.id]||!h2h[a.id][b.id]) continue;
        if (a.finish < b.finish) h2h[a.id][b.id].w++;
        else h2h[a.id][b.id].l++;
      }}
    }}
  }});
  const shortNames = PLAYERS.map(p => p.name.slice(0,6));
  table.innerHTML = "<tr><th></th>" + shortNames.map(n=>`<th>${{n}}</th>`).join("") + "</tr>" +
    PLAYERS.map((p,i) => `<tr><td class="h2h-name" style="color:${{getPlayerColour(p.id,i)}}">${{p.name}}</td>` +
      PLAYERS.map(q => {{
        if (p.id===q.id) return `<td class="h2h-self">—</td>`;
        const r = h2h[p.id]?.[q.id]||{{w:0,l:0}};
        if (r.w===0&&r.l===0) return `<td class="h2h-even" style="opacity:0.3">0–0</td>`;
        const cls = r.w>r.l?"h2h-win":r.w<r.l?"h2h-loss":"h2h-even";
        return `<td class="${{cls}}">${{r.w}}–${{r.l}}</td>`;
      }}).join("")+"</tr>"
    ).join("");
}})();

function prevPanel() {{
  panelIdx = (panelIdx - 1 + PANELS.length) % PANELS.length;
  showPanel();
}}
function nextPanel() {{
  panelIdx = (panelIdx + 1) % PANELS.length;
  showPanel();
}}
function showPanel() {{
  document.getElementById("panelLabel").textContent = PANELS[panelIdx];
  document.getElementById("pieWrap").style.display = panelIdx===0 ? "block" : "none";
  document.getElementById("h2hWrap").style.display = panelIdx===1 ? "block" : "none";
}}

// ── Live Games ────────────────────────────────────────────────────────────────
function buildLive() {{
  const el = document.getElementById("liveContent");
  if (!LIVE_GAMES.length) {{
    el.innerHTML = '<p class="no-games">NO ACTIVE GAMES RIGHT NOW</p>';
    return;
  }}
  el.innerHTML = "";

  LIVE_GAMES.forEach((g, gi) => {{
    const isLobby = g.status === "lobby";
    const pickedCount = g.players.filter(p => p.chosen).length;
    const mapLabel = {{"land":"🏕️ Land","coastal":"⛵ Coastal","any":"🌐 Any","skip":"No draft"}}[g.map_type] || "";
    const amIHost = LOGGED_IN_ID && g.host_id === LOGGED_IN_ID;
    const amIIn = LOGGED_IN_ID && g.players.some(p => p.id === LOGGED_IN_ID);
    const amIInAnyGame = LOGGED_IN_ID && LIVE_GAMES.some(x => x.players.some(p => p.id === LOGGED_IN_ID));
    const myData = g.players.find(p => p.id === LOGGED_IN_ID);
    const iHavePicked = myData && myData.chosen;

    const card = document.createElement("div");
    card.className = "game-card";
    // Auto-expand if I'm in this game
    const bodyOpen = amIIn ? "open" : "";

    // Player rows
    const playerRowsHtml = g.players.map(p => {{
      const isMe = p.id === LOGGED_IN_ID;
      const civBadge = p.chosen
        ? `<span class="badge" style="color:#22c55e;background:#0c2010;border:1px solid #22c55e44">${{p.chosen}}</span>`
        : `<span class="badge" style="color:#475569">picking...</span>`;

      // Show clickable draft pool only for me in draft phase
      let poolHtml = "";
      if (p.pool && p.pool.length) {{
        if (isMe && !iHavePicked && !isLobby) {{
          poolHtml = `<div class="pool-label" style="color:#f97316;margin-top:8px">YOUR DRAFT — click to pick</div>
            <div class="pool-civs" style="margin-top:4px">
              ${{p.pool.map(c => `<button onclick="pickCiv('${{g.host_id}}','${{c}}')" style="padding:4px 10px;border-radius:6px;border:1px solid #1e2130;background:#080a0f;color:#94a3b8;font-family:inherit;font-size:10px;cursor:pointer;transition:all 0.15s" onmouseover="showCivTooltip('${{c}}',this);this.style.borderColor='#f97316';this.style.color='#f97316'" onmouseout="hideCivTooltip();this.style.borderColor='#1e2130';this.style.color='#94a3b8'">${{c}}</button>`).join("")}}
            </div>`;
        }} else {{
          poolHtml = `<div class="pool-label">DRAFT POOL</div>
            <div class="pool-civs">${{p.pool.map(c => `<span class="pool-civ ${{c===p.chosen?"chosen":""}}" onmouseenter="showCivTooltip('${{c}}',this)" onmouseleave="hideCivTooltip()">${{c}}</span>`).join("")}}</div>`;
        }}
      }}

      return `<div class="player-row" style="flex-wrap:wrap">
        <div style="flex:1;font-weight:600;${{isMe?"color:#f97316":""}}">${{p.name}}${{isMe?" (you)":""}}</div>
        ${{civBadge}}
      </div>${{poolHtml}}`;
    }}).join("");

    // Action buttons for non-hosts
    let actionHtml = "";
    if (LOGGED_IN_ID && !amIHost) {{
      if (isLobby && !amIIn && !amIInAnyGame) {{
        actionHtml = `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #1e2130">
          <button class="btn btn-primary" onclick="joinLobby('${{g.host_id}}')">Join Lobby</button>
        </div>`;
      }} else if (isLobby && amIIn) {{
        actionHtml = `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #1e2130">
          <button class="btn btn-ghost" onclick="leaveLobby('${{g.host_id}}')">Leave Lobby</button>
        </div>`;
      }} else if (!isLobby && amIIn && iHavePicked) {{
        actionHtml = `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #1e2130">
          <p style="font-size:11px;color:#475569;margin-bottom:8px">✅ You picked <strong style="color:#22c55e">${{myData.chosen}}</strong> — waiting for others...</p>
          <button class="btn btn-ghost" onclick="cancelGame('${{g.host_id}}','game')">✕ Cancel Game</button>
        </div>`;
      }} else if (!isLobby && amIIn && !iHavePicked) {{
        actionHtml = `<div style="margin-top:12px;padding-top:12px;border-top:1px solid #1e2130">
          <button class="btn btn-ghost" onclick="cancelGame('${{g.host_id}}','game')">✕ Cancel Game</button>
        </div>`;
      }}
    }}

    // Host badge in header
    const hostNote = amIHost ? ` <span style="font-size:9px;color:#f97316;border:1px solid #f97316;border-radius:4px;padding:1px 5px">YOU HOST</span>` : "";

    card.innerHTML = `
      <div class="game-header" onclick="toggleGame('live-${{gi}}')">
        <div>
          <div class="game-title">${{isLobby?"🏛️ Open Lobby":"⚔️ In Progress"}} · ${{g.host}}${{hostNote}}</div>
          <div class="game-meta">${{g.players.length}} players · ${{g.difficulty}}${{mapLabel?" · "+mapLabel:""}}${{isLobby?"":" · "+pickedCount+"/"+g.players.length+" picked"}}</div>
        </div>
        <span class="chevron ${{bodyOpen}}" id="chev-live-${{gi}}">▼</span>
      </div>
      <div class="game-body ${{bodyOpen}}" id="live-${{gi}}">
        ${{playerRowsHtml}}
        ${{actionHtml}}
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

// ── Civilopedia ───────────────────────────────────────────────────────────────
const CIVPEDIA = {{"Akkad":{{"leader":"Sargon","entries":[{{"type":"Ability","name":"The Great Unification","desc":"+3 Culture, +2 Happiness and +1 Production from conquered cities. Great Generals grant nearby Units (from any domain) a +15% Combat Bonus when initiating combat with enemy Cities."}},{{"type":"Building","name":"Akkadian Library","desc":"Replaces the Library. Provides +1 additional Science and does not require Gold maintenance. Costs less to produce (40 hammers vs. 50)."}},{{"type":"Unit","name":"Laputtu","desc":"Replaces Spearmen. 3 Movement (vs. 2). Units that start their turn on the same tile as this unit copy this unit\u2019s Movement stat. Ability is kept on upgrade."}},{{"type":"Bias","name":"River","desc":""}}]}},"Aksum":{{"leader":"Ezana","entries":[{{"type":"Ability","name":"Saint Elesbaan's Blessing","desc":"Upon founding a Religion, your cities immediately follow it. Your religion does not exert Religious pressure on foreign cities."}},{{"type":"Improvement","name":"Rock-Hewn Church","desc":"Available at Mining. Provides +2 Faith. +1 Faith at Theology . May not be built adjacent to each other nor on resource tiles. Quarries adjacent to this improvement yield +1 Faith for each adjacent. Units healing on or next to this improvement yield +2 Faith for the unit owner."}},{{"type":"Building","name":"King Ezana\u2019s Stele","desc":"Replaces the National Epic. Provides +3 Culture and +4 Faith in addition to its base yields. All monuments in the empire provide an additional +1 Food, +1 Production, +1 Gold and +1 Faith."}},{{"type":"Bias","name":"Hill","desc":""}}]}},"America":{{"leader":"Washington","entries":[{{"type":"Ability","name":"Manifest Destiny","desc":"Military Land Units gain +1 Sight. Gold costs for purchasing territory are halved."}},{{"type":"Unit","name":"Minuteman","desc":"Replaces Musketmen. Ignores terrain costs, arrives trained with Drill I (+15% bonus in Rough terrain) , and earns 100% of foe\u2019s Strength as points towards a Golden Age from kills. All abilities are kept on upgrade."}},{{"type":"Unit","name":"Pioneer","desc":"Replaces the Settler. Has the same combat and movement properties as the Scout (preventing instant capture) and may Settle."}},{{"type":"Bias","name":"None","desc":""}}]}},"Arabia":{{"leader":"Harun al-Rashid","entries":[{{"type":"Ability","name":"Ships of the Desert","desc":"+1 Culture and +1 Gold from Luxury Buildings. Buildings that improve luxury resources also receive this bonus if the city has the appropriate Luxury Resource nearby. Receive twice as many Oil resources\u201d"}},{{"type":"Unit","name":"Camel Archer","desc":"Replaces the Knight. A Mounted Ranged Unit with 20 Ranged Strength (from 21) and 14 Combat Strength (from 17, vs. 20) . Receives no penalty when initiating combat with an enemy City."}},{{"type":"Building","name":"Bazaar","desc":"Replaces the Market. Provides an additional happiness. Additionally every luxury resource worked by the city provides +1 Gold."}},{{"type":"Bias","name":"Desert, avoid wetlands","desc":""}}]}},"Argentina":{{"leader":"Eva Per\u00f3n","entries":[{{"type":"Ability","name":"Pride of the People","desc":"Construct Pastures and Farms twice as fast."}},{{"type":"Unit","name":"Gaucho","desc":"Replaces the Knight. Receives a 50% chance to withdraw from Melee attacks. Flanking bonuses are tripled. Capable of building Pastures and Farms. Abilities are lost on upgrade."}},{{"type":"Building","name":"Ocupada Estable","desc":"Gain +1 Food from Pasture resources and +1 Production from Maize in addition to the regular benefits of the Stable."}},{{"type":"Bias","name":"None","desc":""}}]}},"Armenia":{{"leader":"Tiridates III","entries":[{{"type":"Ability","name":"Splendor of the Caucasus","desc":"+1 Food, +2 Production and +1 Gold from Mountains (including mountainous Natural Wonders - i.e. Kilimanjaro, Uluru, Mt. Fuji, Mt. Sinai, Mt. Kailash, Rock of Gibraltar and Sri Pada )."}},{{"type":"Unit","name":"Sparapet","desc":"Replaces Horsemen. Requires 80 Production and 2 Horse resources. Boasts a mighty 17 Combat Strength (vs. 12) and grants the Great General Combat Bonus to nearby allies. Does not obsolete. Keeps the ability on upgrade and only costs 1 strategic resource."}},{{"type":"Building","name":"Darbas","desc":"Replaces the Observatory. In addition to the typical perks, provides +4 Culture in the City and +2 Culture on Mountain tiles and Wonders for a total of +5 Science and +2 Culture on those tiles."}},{{"type":"Bias","name":"Hill, Avoid Jungle","desc":""}}]}},"Assyria":{{"leader":"Ashurbanipal","entries":[{{"type":"Ability","name":"Siege Warfare","desc":"Siege Units receive +1 Movement. Receive a free Great Writer at Philosophy, and receive a free Great Work of Writing upon completion of the Royal Library in the Capital."}},{{"type":"Building","name":"Royal Library","desc":"Replaces the Library. Provides an additional +1 Culture and Science and provides +15 (from 10) XP to Units trained in the City when its Great Work of Writing slot is filled."}},{{"type":"Unit","name":"Siege Tower","desc":"Replaces the Catapult. A Melee Siege Unit with 12 Strength (vs. 7) only capable of combat with enemy Cities. Arrives trained with Cover I (+33% bonus defending against Ranged attacks) and Extra sight 1.. When adjacent to an enemy City, Units within 2 tiles of the Siege Tower gain a +50% Combat Bonus when initiating combat with that City. Keeps Extra sight and Cover I promotion on upgrade."}},{{"type":"Bias","name":"Avoid Tundra","desc":""}}]}},"Australia":{{"leader":"Henry Parker","entries":[{{"type":"Ability","name":"Dreamtime","desc":"+5 Faith from Natural Wonders. Gain 10 Faith upon discovering a Natural Wonder."}},{{"type":"Unit","name":"Ngangkari","desc":"Replaces the Worker. 3 Movement (vs. 2) , receives an additional +1 Movement while embarked, and arrives with Medic I and II (adjacent allies heal an additional +10 HP while Fortified)."}},{{"type":"Building","name":"Convict Penitentiary","desc":"Replaces the Constabulary. Available at Machinery (instead of Banking). In addition to slowing the rate of Technology theft, provides +1 Gold and Production, and +5% Gold and Production in the City. Requires no Gold maintenance and is appreciably cheaper to construct (80 hammers vs. 106) ."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Austria":{{"leader":"Maria","entries":[{{"type":"Ability","name":"Diplomatic Marriage","desc":"May spend Gold to annex a City-State that has been your ally for at least 5 turns."}},{{"type":"Unit","name":"Hussar","desc":"Replaces Cavalry. +1 Sight and Movement and is 50% more effective at Flank attacks (bonus from each ally adjacent to foe increases from +10% to +15% per ally). Keeps all abilities on upgrade."}},{{"type":"Building","name":"Coffee House","desc":"Replaces the Windmill. Available at Printing Press (instead of Economics). In addition to the typical perks, provides a +5% Production bonus and a +25% Great Person generation bonus in the City. Much cheaper to construct (100 hammers from 167, vs. 167)."}},{{"type":"Bias","name":"Hill","desc":""}}]}},"Ayyubids":{{"leader":"Saladin","entries":[{{"type":"Ability","name":"Justice of Saladin","desc":"Receive a free Burial Tomb in each City you conquer. Workers construct improvements +25% faster. (The Burial Tomb is a unique building of Egypt that replaces the Temple. If the Temple in a captured City survives, the City will house both buildings.)"}},{{"type":"Building","name":"Madrasah","desc":"Replaces the University. In addition to the typical perks, provides +2 Faith in the City; +1 Science from Flood Plains, +2 Science from Oases."}},{{"type":"Unit","name":"Mamluk","desc":"Replaces the Knight. Gains a +50% bonus vs. Melee units, +25% vs. Gunpowder units. Slightly more expensive to produce (85 hammers vs. 80) . Keeps the ability on upgrade."}},{{"type":"Bias","name":"Desert","desc":""}}]}},"Aztecs":{{"leader":"Montezuma","entries":[{{"type":"Ability","name":"Sacrificial Captives","desc":"Earn 100% of foe\u2019s Strength as Culture from kills."}},{{"type":"Building","name":"Floating Gardens","desc":"Replaces the Watermill. In addition to the typical perks, provides +10% Food (from +15%) in the City and each Lake tile in the City provides an additional +1 (from +2) Food. In addition to Rivers, the Floating Gardens may also be constructed when adjacent to Lakes. Costs less Gold maintenance (1 Gold vs. 2) ."}},{{"type":"Unit","name":"Jaguar","desc":"Replaces the Warrior. Gains a +33% Combat Bonus in Forests and Jungles, heals 25 HP from kills, and moves unimpeded through Forests and Jungles. (These promotions are retained upon upgrade.) Keeps all abilities on upgrade."}},{{"type":"Bias","name":"Jungle","desc":""}}]}},"Babylon":{{"leader":"Nebuchadnezzar II","entries":[{{"type":"Ability","name":"Ingenuity","desc":"Receive a free Great Scientist in the Capital when you discover Philosophy (previously Writing). Earn Great Scientists +25% (from +50%) faster."}},{{"type":"Building","name":"Walls of Babylon","desc":"Replaces Walls. Boosts City Strength by +6 (vs. 5) and raises City health by +100 (vs. 50). Cheaper to construct (44 hammers vs. 50)."}},{{"type":"Unit","name":"Bowman","desc":"Replaces the Archer. 7 Melee Strength (vs. 5) and 9 Ranged Strength (vs. 7)."}},{{"type":"Bias","name":"Avoid Tundra","desc":""}}]}},"Belgium":{{"leader":"Leopold II","entries":[{{"type":"Ability","name":"Colonialist Riches","desc":"+1 Production from Plantations. +1 Gold from Strategic Resources."}},{{"type":"Building","name":"Stade","desc":"Replaces Zoo. In addition to the typical perks, provides an additional +1 Happiness, Culture, and Gold, and does not require Gold maintenance. Cheaper to construct (100 hammers vs. 120). Does not require a Colosseum to be built."}},{{"type":"Unit","name":"Force Publique","desc":"Replaces Great War Infantry. 51 Strength (vs. 50) , arrives with Drill I (+15% bonus in Rough terrain) and Charge (+33% bonus vs. wounded Units). Promotions are kept on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Boers":{{"leader":"Stephanus Johannes Paulus Kruger","entries":[{{"type":"Ability","name":"The Great Trek","desc":"Farms yield +1 Food when adjacent to at least 2 other Farms. This bonus is increased by +1 Gold at 3, +1 Production at 4, and +1 Culture and Science at 5 adjacent Farms."}},{{"type":"Building","name":"Staatsmuseum","desc":"Replaces the Opera House. Provides +1 additional Culture and houses an Artist Specialist slot. Cheaper to construct (117 hammers vs. 134)."}},{{"type":"Unit","name":"Voortrekker","desc":"Replaces Great War Infantry. Gains a +25% Bonus when foes initiate combat against this Unit. Heals completely from kills. Capable of building Farms in 2 turns. Slightly cheaper to construct (200 hammers vs. 210). Keeps the defense ability and still heals completely from kills on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Bolivia":{{"leader":"Tata Belzu","entries":[{{"type":"Ability","name":"Revoluci\u00f3n Nacional","desc":"Expending a Great Artist improves the yield of Mines with +1 Production and expending a Great Writer improves them with +1 Food instead. (When one yield has already been chosen, choosing the opposite will swap them instead of stacking. This ability cannot be triggered by unique Artist or Writer replacements.)"}},{{"type":"Great Person","name":"Comparsa Folklorica","desc":"Replaces the Great Musician. Moves further (4 Movement vs. 2) and may be consumed to spread Tourism, begin a Golden Age, or gain a large amount of Culture."}},{{"type":"Unit","name":"Colorado","desc":"Replaces the Anti-Tank Rifle. 40 Strength (vs. 30). Available at Dynamite. Only receives a +100% Bonus vs. Armored Units (vs. 200) , but gains +2 Strength for every 5 points of excess Happiness in the empire. (Rounds up. Updates when a unit is moved or when a new turn begins.) Loses ability on upgrade."}},{{"type":"Bias","name":"Hill","desc":""}}]}},"Brunei":{{"leader":"Bolkiah","entries":[{{"type":"Ability","name":"Sea Nomads","desc":"All Naval Units may heal outside friendly territory and pay 33% less Gold maintenance. Melee Naval Units can create improvements on Coast and Ocean tiles."}},{{"type":"Building","name":"BMPC Plant","desc":"Replaces Oil Refineries. In addition to the typical perks, provides +3 Gold and +5 Production. Doesn\u2019t require nearby Oil to build. Upon completion, provides 2 Oil resources."}},{{"type":"Improvement","name":"Kampong Ayer","desc":"Requires Optics. May be built on any Coastal tile adjacent to land tiles. Provides +1 Food, Culture and Gold. +1 Production at Navigation, and +1 additional Culture at Flight."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Bulgaria":{{"leader":"Asparukh","entries":[{{"type":"Ability","name":"Cyrillic Script","desc":"Writer Specialists provide +5 Science; Science buildings provide +1 Culture."}},{{"type":"Unit","name":"","desc":"Konnitsa - Replaces the Knight. Similar to an Impi, performs a Ranged attack when initiating combat. Ability is lost on upgrade."}},{{"type":"Building","name":"Khambar","desc":"Replaces Granary. Cheaper to construct (32 hammers vs. 40). In addition to the typical perks, yields +1 Gold from Wheat and Cattle resources."}},{{"type":"Bias","name":"None","desc":""}}]}},"Burma":{{"leader":"Anawrahta","entries":[{{"type":"Ability","name":"Pyu City States","desc":"+1 Happiness global happiness per city."}},{{"type":"Unit","name":"Aahkyayhkya Sai - Replaces the settler.","desc":"The Aahkyayhkya Sai is 40% cheaper to construct, but removes a population in the city when finished. It requires the city to have at least 3 population."}},{{"type":"Building","name":"","desc":"Kyawwat Replaces Stoneworks. In addition to the typical perks, the Kyawwat extends its yield bonuses to Salt and Iron resources and does not require any resources within the City to construct."}},{{"type":"Bias","name":"River","desc":""}}]}},"Byzantium":{{"leader":"Theodora","entries":[{{"type":"Ability","name":"Patriarchate of Constantinople","desc":"Choose an additional Pantheon, Follower, Founder or Enhancer Belief when founding a Religion."}},{{"type":"Building","name":"Hippodrome","desc":"Replaces Amphitheater. Slightly less costly to produce (63 hammers vs. 67) . In addition to the typical perks, also provides +2 Faith and +1 Happiness in the City."}},{{"type":"Unit","name":"Cataphract","desc":"Replaces Horsemen. 15 Strength (vs. 12) , but 3 Movement (vs. 4) . Able to receive defensive Terrain Bonuses and is penalized slightly less for attacking Cities (-25% vs. -33%) . Abilities are lost on upgrade."}},{{"type":"Bias","name":"None (no longer Coastal)","desc":""}}]}},"Canada":{{"leader":"John A. MacDonald","entries":[{{"type":"Ability","name":"Canadian Fur Trade","desc":"+2 Gold from Camps, +2 Gold and +1 Culture from Lake tiles."}},{{"type":"Unit","name":"Combat Engineer","desc":"Replaces Rifleman. Available at Industrialization. Identical Strength, but +1 Movement (3 vs. 2) . May construct Roads, Railroads and Forts, and clear Forests, Jungles and Marsh tiles. Abilities are lost on upgrade."}},{{"type":"Building","name":"Tim Horton\u2019s","desc":"Replaces Stock Exchange. In addition to the typical perks, provides +2 Happiness in the City and +1 Gold to river tiles worked by this City. Significantly cheaper to construct (200 hammers vs. 280) ."}},{{"type":"Bias","name":"None","desc":""}}]}},"Carthage":{{"leader":"Dido","entries":[{{"type":"Ability","name":"Phoenecian Heritage","desc":"All Coastal Cities receive a free Harbor. Units may cross mountains, receiving 50 HP damage if they end their turn on one. (The ability to cross mountains is no longer tied to earning a Great General). (Note: Harbors will only form City Connections after the discovery of the Wheel, but will still provide the appropriate Gold.)"}},{{"type":"Building","name":"Cothon","desc":"Replaces Lighthouse. +1 Food and +1 Production in the City in addition to the typical perks."}},{{"type":"Unit","name":"African Forest Elephant","desc":"Replaces Horsemen, but does not require Horses to construct. 14 Strength (vs. 12) , but 3 Movement (vs. 4) . Possesses the Feared Elephant (enemy Units adjacent to this Unit receive a -10% combat penalty) , Great Generals II (earns Great General points faster) promotion and Hannibal\u2019s Charge (+20% Strength when initiating combat from a higher elevation than foe; enemy Units will retreat if they receive more damage than this Unit, this Unit deals +50% damage to defenders incapable of retreat) . Loses the feared Elephant ability, but keeps Great Generals II and Hannibal\u2019s Charge on upgrade. (Feared Elephant will not stack.)"}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Celts":{{"leader":"Boudicca","entries":[{{"type":"Ability","name":"Druidic Lore","desc":"Cities with an adjacent unimproved Forest tile generate +1 Faith, increasing to +2 Faith with three adjacent tiles."}},{{"type":"Building","name":"Silversmith","desc":"Replaces the Mint. Provides +2 Production and +1 Gold (vs. +2 Gold) and does not require resources to construct, but retains the typical perks of a Mint. Less costly to produce (50 hammers vs. 66) ."}},{{"type":"Unit","name":"Pictish Warrior","desc":"Replaces Spearmen. Earn 100% of your foe\u2019s Combat Strength as Faith from kills (previously 50%) . Gains a +20% Combat Bonus outside of friendly territory and may pillage without movement penalties. Keeps the abilities on upgrade."}},{{"type":"Bias","name":"Forest","desc":"Chile - Bernardo O\u2019 Higgens"}}]}},"China":{{"leader":"Wu Zetian","entries":[{{"type":"Ability","name":"Art of War","desc":"Great Generals provide an additional +15% Combat Bonus to nearby Units and are generated +50% faster."}},{{"type":"Building","name":"Paper Maker","desc":"Replaces the Library. Requires no Gold maintenance and provides +2 Gold."}},{{"type":"Unit","name":"Chu-Ko-Nu","desc":"Replaces Crossbowmen. Weaker at base (14 Strength vs. 18) , but may attack twice each turn. Ability is kept on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Colombia":{{"leader":"Simon Bolivar","entries":[{{"type":"Ability","name":"Independence of the People","desc":"+1 Food from Lumber Mills. Lumber Mills receive an additional +1 Science at Chemistry."}},{{"type":"Building","name":"Cacicazgo","desc":"Replaces the Granary. In addition to the typical perks, provides +1 Production and allows the City to send Production through Trade Routes (as a Workshop would). Doesn\u2019t require Gold maintenance. Slightly more expensive to construct (44 hammers vs. 40)."}},{{"type":"Unit","name":"Llanero","desc":"Replaces Cavalry. Capable of receiving defensive terrain bonuses and Fortifying. Ability is lost on upgrade."}},{{"type":"Bias","name":"Forest, Plains","desc":""}}]}},"Cuba":{{"leader":"Fidel Castro","entries":[{{"type":"Ability","name":"\u00a1Viva la Revoluci\u00f3n!","desc":"Receive +1 Culture per turn in your Capital for every 5 Culture per turn generated in the Capital of Civilizations you\u2019ve met. Upon selecting your first Ideology Tenet, receive 2 Guerrilleros in the Capital. Receive -50% less Unhappiness from Ideology pressure."}},{{"type":"Unit","name":"Guerrillero","desc":"Replaces Great War Infantry. Available to produce only after the adoption of Cuba\u2019s first Ideology Tenet (as opposed to Replaceable Parts). Weaker (44 Strength vs. 50) , but boasts 3 Movement (vs. 2) and can be built for significantly less (176 hammers vs. 221) . Unit upgrades with normal movement."}},{{"type":"Building","name":"Dance Hall","desc":"Replaces the Opera House. In addition to the typical perks, the Dance Hall provides +1 Happiness, +4 Culture, and +15% Production towards military Units in the City when its Great Work slot is filled. (This bonus is updated at the start of every turn.) Cheaper to construct (100 hammers vs. 134) ."}},{{"type":"Bias","name":"None","desc":""}}]}},"Czechia":{{"leader":"Vladislav II","entries":[{{"type":"Ability","name":"Hussite Fervor","desc":"+100% Religious Pressure towards your own cities. Choose a Reformation belief upon founding a religio n ."}},{{"type":"Building","name":"Thaler Mint","desc":"Replaces Mint. In addition to the typical perks, yields +2 Gold from Iron and is also unlocked by it. Costs 25% less production to construct. Has a Great Merchant Specialist Slot."}},{{"type":"Unit","name":"Czechoslovak Legion","desc":"Replaces Foreign Legion . In addition to the typical perks, it is stronger (45 strength, vs 42) , does not require a specific policy to be unlocked, and has a 33% combat bonus against military units from civilizations with a different ideology than the player who owns the unit. 33% cheaper to buy with Gold and may move immediately after doing so."}},{{"type":"Bias","name":"None","desc":""}}]}},"Denmark":{{"leader":"Harald Bluetooth","entries":[{{"type":"Ability","name":"Viking Fury","desc":"Embarked Units gain +1 Movement and may disembark for only 1 Movement; Civilian units may also embark for the same cost. Melee Units pillage without movement penalties."}},{{"type":"Unit","name":"Berserker","desc":"Replaces Longswordsmen. Available at Metal Casting instead of Steel. 3 Movement (vs. 2) and ignores penalties for attacking over rivers or while embarked. Upgrades to normal movement, but keeps the Amphibious promotion."}},{{"type":"Unit","name":"Longship","desc":"Replaces Trireme. 6 Movement (vs. 4) . Embarked Units that begin their turn on the same tile as a Longship are granted 6 Movement for that turn. (This Movement bonus is retained after disembarkation. Promoted Longships with bonus Movement will also confer this bonus to the embarked Unit.)"}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Egypt":{{"leader":"Ramesses II","entries":[{{"type":"Ability","name":"Monument Builders","desc":"+15% (from 20%) Production towards the construction of World Wonders and National Wonders."}},{{"type":"Building","name":"Burial Tomb","desc":"Replaces the Temple. In addition to the typical perks, provides +2 Happiness and does not require Gold maintenance. Cities with a Burial Tomb double the amount of Gold plundered for the conqueror."}},{{"type":"Unit","name":"War Chariot","desc":"Replaces the Chariot Archer. 5 Movement (vs. 4) and does not require Horse resources to construct. Upgrades into a normal knight."}},{{"type":"Bias","name":"Avoid Forest, Jungle","desc":""}}]}},"England":{{"leader":"Elizabeth","entries":[{{"type":"Ability","name":"Sun Never Sets","desc":"English Naval Units receive +2 Movement. Receive an additional Spy upon entering the Renaissance. Constabularies and Police Stations are built twice as fast and provide +1 Happiness."}},{{"type":"Unit","name":"Longbowman","desc":"Replaces Crossbowmen. 12 Melee Strength (vs. 13) and 18 Ranged Strength (vs. 18) , but wields 3 Range (vs. 2) . Range promotion is kept on upgrade."}},{{"type":"Unit","name":"Ship of the Line","desc":"Replaces the Frigate. +1 Sight. 25 Combat Strength (from 30, vs. 25) and 30 Ranged Strength (from 35, vs. 28). (The Ship of the Line has 5 Movement at base (vs. a Frigate\u2019s 6), increasing effectively to 7 due to Sun Never Sets.) Keeps the +1 Sight promotion on upgrade."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Ethiopia":{{"leader":"Haile Selassie","entries":[{{"type":"Ability","name":"Spirit of Adwa","desc":"Ethiopian Units gain a +20% Bonus in combat with Civilizations with more Cities than Ethiopia."}},{{"type":"Building","name":"Stele","desc":"Replaces the Monument. In addition to the Culture yield, provides +2 Faith."}},{{"type":"Unit","name":"Mehal Sefari","desc":"Replaces Riflemen. 35 Strength (from 34, vs. 34). Arrives trained with Drill I (+15% Bonus in Rough terrain) and gains Strength according to distance from the Capital, peaking at +30% when the Mehal Sefari is stationed inside the Capital and decreasing by -3% per tile away. Cheaper to produce (134 hammers vs. 150) . Keeps the ability and the promotion on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Finland":{{"leader":"Mannerheim","entries":[{{"type":"Ability","name":"Finnish Mobility","desc":"All Melee and Mounted Units receive a +50% increased flanking bonus (+15% per ally adjacent to foe vs. 10%) . +1 Culture from unimproved Forest tiles."}},{{"type":"Unit","name":"Sissi","desc":"Replaces the Machine Gun. Ignores terrain costs and receives a +25% Combat Bonus vs. Armored Units. Has an additional movement and does not spend movement when pillaging tiles, but cannot attack twice. 60 Ranged Strength (vs. 50) , 50 Melee Strength. Cheaper to construct (234 hammers vs. 260) . Keeps all abilities and promotions AND is able to attack twice on upgrade."}},{{"type":"Building","name":"Sauna","desc":"Replaces the Shrine. +2 Culture from Lake tiles. Land and Naval Units trained in Cities with a Sauna heal +10 HP inside friendly territory. Costs no gold maintenance."}},{{"type":"Bias","name":"None","desc":""}}]}},"France":{{"leader":"Napoleon","entries":[{{"type":"Ability","name":"City of Light","desc":"+1 Culture in the Capital, increasing by an additional +15 upon the discovery of Acoustics. Theming bonuses are doubled in the Capital."}},{{"type":"Improvement","name":"Chateau","desc":"Provides +2 Science , +2 Culture and +1 Gold, increasing by +1 Culture and +2 Gold at Flight, and +1 Science after adopting the Free Thought policy. Provides the same defensive bonus as a Fort (+50% bonus when foe initiates combat). Chateaus must be constructed adjacent to a luxury resource, non-adjacent to another Chateau, and not atop an existing Resource tile. (Resources discovered after the construction of the Chateau will provide their yield bonus to the tile, but the Chateau will not \u2018connect\u2019 those resources.)"}},{{"type":"Unit","name":"Musketeer","desc":"Replaces Musketmen. 28 Strength (vs. 24) . That\u2019s it."}},{{"type":"Bias","name":"None","desc":""}}]}},"Franks":{{"leader":"Charlemagne","entries":[{{"type":"Ability","name":"Holy Roman Empire","desc":"+1 Faith from Farms. Frankish Units receive +25% additional Movement when traveling along friendly and neutral Roads and Railroads."}},{{"type":"Building","name":"Mead Hall","desc":"Replaces the Colosseum. Provides an additional point of Happiness (3 vs. 2) and a bonus +1 Culture. Costs 25% less production to construct. Costs no gold maintenance."}},{{"type":"Unit","name":"Seaxman","desc":"Replaces Longswordsmen. Available at Chivalry instead of Steel. 22 Strength (vs. 21) . Arrives trained with the Cover I (+33% bonus defending against ranged attacks) and Amphibious (attack from the sea or over rivers without penalty) promotions. Almost negligibly cheaper to construct (76 hammers vs. 80) . Promotions are kept on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Gaul":{{"leader":"Vercingetorix","entries":[{{"type":"Ability","name":"Oppidum of Bibracte","desc":"Receive a permanent Murus Gallicus in the Capital upon the discovery of Mining."}},{{"type":"Building","name":"Murus Gallicus","desc":"Replaces Walls. Slightly cheaper (44 Production vs. 50) and better defense (+6 vs. +5) . +1 Production and +1 Happiness; +1 additional Production after researching Metal Casting."}},{{"type":"Unit","name":"Noble Swordsman","desc":"Replaces Longswordsmen. 20 Strength (vs. 21) . Gains a +25% Combat Bonus when initiating combat and a +50% Bonus in Forests. Does not require Iron resources. Becomes obsolete at Rifling (instead of Gunpowder) and upgrades into Riflemen. Keeps ability on upgrade."}},{{"type":"Bias","name":"Forest","desc":""}}]}},"Georgia":{{"leader":"Tamar","entries":[{{"type":"Ability","name":"Ecclesiastical Architecture","desc":"Buildings unlocked by religious beliefs (except Houses of Worship) are built using Production instead (cost equal to 50% of the required Faith); these buildings provide +2 Production."}},{{"type":"Building","name":"Tsikhe","desc":"Replaces Walls. In addition to typical defensive perks, provides +3 Faith, and +1 Culture from Strategic Resources (e.g. Horses, Iron). Units trained in this City earn Golden Age points from kills (if the killed unit is pre-industrial) ."}},{{"type":"Unit","name":"Khevsur","desc":"Replaces Swordsmen. Arrives at Guilds technology instead of Iron Working, but ignores terrain movement costs as a Scout would, and receives a +33% Combat Bonus during Golden Ages. Loses \u2018ignores terrain movement cost\u2019 on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Germany":{{"leader":"Bismarck","entries":[{{"type":"Ability","name":"Precision Engineering","desc":"+1 Science and Production from Workshops, Windmills, Factories and Hydro Plants. +33% faster Great Engineer generation."}},{{"type":"Building","name":"Hanse","desc":"Replaces Banks. In addition to the typical perks, gain +5% Production in the City for every Trade Route your empire currently maintains with a City-State."}},{{"type":"Unit","name":"Panzer","desc":"Replaces the Tank. 80 Strength (vs. 70) and 6 Movement (vs. 5) . Loses bonus movement on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Golden Horde":{{"leader":"Batu Khan","entries":[{{"type":"Ability","name":"Golden Conquest","desc":"Units are +50% more effective at intimidating City-States. Puppeted Cities provide +50% Production, Gold, and Science, and generate -33% less Unhappiness."}},{{"type":"Building","name":"Yam Route","desc":"Replaces Caravansaries. Available at The Wheel instead of Horseback Riding. Retains Trade Route perks but doesn\u2019t provide Gold, instead providing +3 Science. Allows Airlifting between Cities with Yam Routes."}},{{"type":"Unit","name":"Ulan","desc":"Replaces Pikemen. Stronger than its counterpart (17 Strength vs. 16). Behaves as a Mounted Unit and requires a Horse resource. 4 Movement (vs. 2) . Arrives with Charge (+33% bonus against wounded foes) and does not receive a penalty in combat with Cities. Keeps Charge promotion on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Goths":{{"leader":"Alaric I","entries":[{{"type":"Ability","name":"Drauhtinon","desc":"Melee and Gunpowder units receive +1 Movement.l"}},{{"type":"Unit","name":"Gadrauht","desc":"Replaces Longswordsman. 22 Strength (vs. 21) . Cheaper to produce (72 hammers vs. 80) and does not require Iron to train. Heals 25HP upon killing a unit. Keeps ability on upgrade."}},{{"type":"Improvement","name":"Harjis","desc":"Available at Mining. Must be adjacent to a Luxury resource (but not on"}}]}},"Greece":{{"leader":"Pericles","entries":[{{"type":"Ability","name":"Hellenic League","desc":"City-State Influence degrades half as slowly and recovers at twice the typical rate. Undocumented: City-State\u2019s territory is considered friendly territory (No trespassing penalty and units heal at double rate)."}},{{"type":"Building","name":"Odeon","desc":"Replaces the Amphitheater. Available at Writing (instead of Drama and Poetry). Provides +4 Culture (vs. 2) , +1 Gold, and does not require Gold maintenance. Houses a slot for a Great Writer specialist."}},{{"type":"Unit","name":"Hoplite","desc":"Replaces Spearmen. 13 Strength (vs. 11)."}},{{"type":"Bias","name":"None (previously Mixed).","desc":""}}]}},"Hittites":{{"leader":"Muwatallis","entries":[{{"type":"Ability","name":"Bronze and Iron","desc":"+1 Gold from Mines. This bonus increases to +3 gold when the mine is adjacent to at least 3 other mines. Start with Mining Technology."}},{{"type":"Building","name":"Lion\u2019s Gate","desc":"Replaces Walls. Cheaper to construct (40 hammers vs. 50). In addition to the typical defensive perks, provides +2 Gold and +2 Culture in the City."}},{{"type":"Unit","name":"Heavy Chariot","desc":"Replaces the Chariot Archer. Identical Ranged Strength and 3 Movement (vs. 4) , but 10 Melee Strength (vs. 5) . Gains a +20% defensive bonus when enemies initiate combat. Keeps ability on upgrade."}},{{"type":"Bias","name":"River","desc":""}}]}},"Hungary":{{"leader":"Andr\u00e1s II","entries":[{{"type":"Ability","name":"","desc":"Verszerzodes - Walls, Castles, Arsenals and Military Bases provide +1 Production and Culture. May construct up to 2 Palaces."}},{{"type":"Unit","name":"Black Arquebusier","desc":"Replaces Musketmen. Identical strength, but is capable of setting up to perform Ranged attacks (similar to a Siege Unit). Arrives with Accuracy I (+15% bonus in Open terrain). Slightly cheaper to produce (90 vs. 100) . (Despite Accuracy typically denoting a bonus for Ranged units only, this bonus and other similar bonuses will apply to both Melee and Ranged attacks from this Unit.) Promotions are kept on upgrade. Yay!"}},{{"type":"Building","name":"Orszaggyules","desc":"Replaces the Palace. In addition to the regular bonuses of a Palace, provides +25% faster Great Person generation and +10% Production towards buildings in the City. Receive this building in the original Capital. Upon the discovery of Mathematics, a second Orszaggyules may be constructed in another City (for 67 hammers) ; this City becomes the new Capital of the empire. The original Orszaggyules may be sold and reconstructed to move this designation back; the Capital is located wherever the last Orszaggyules was constructed."}},{{"type":"Bias","name":"None","desc":""}}]}},"Huns":{{"leader":"Attila","entries":[{{"type":"Ability","name":"Scourge of God","desc":"No longer starts with Animal Husbandry Technology. +1 Production from all Pastures. Raze Cities at double speed and borrow City names from rival Civilizations."}},{{"type":"Building","name":"Qara U\u2019y","desc":"Replaces the Stable. In addition to the typical perks, yields +1 Faith from Horses. Provides 2 Horse Resources for the empire upon completion."}},{{"type":"Unit","name":"Horse Archer","desc":"Replaces the Chariot Archer. Greater Melee Strength (7 vs. 6) , receives no movement penalty in Rough terrain, and arrives with Accuracy I (+15% bonus in Open terrain). Requires Horses to construct. Keep promotion on upgrade. Yay! (In Brave New World, this Unit did not require Horses.)"}},{{"type":"Bias","name":"Avoid Forest, Jungle","desc":""}}]}},"Inca":{{"leader":"Pachacuti","entries":[{{"type":"Ability","name":"Great Andean Road","desc":"Units ignore terrain costs moving through Hills. No Gold maintenance is paid for tile improvements in Hills, and half the Gold is paid for improvements elsewhere."}},{{"type":"Improvement","name":"Terrace Farm","desc":"May only be constructed in Hills. Provides +1 Food, increasing by an additional +1 Food for every adjacent Mountain tile. Like Farms, receives an additional +1 Food if adjacent to freshwater at Civil Service, otherwise receiving this bonus at Fertilizer if not adjacent to freshwater."}},{{"type":"Unit","name":"Slinger","desc":"Replaces the Archer. 5 Melee Strength (from 4, vs. 5). The Slinger has a chance to withdraw from melee combat initiated by foes, avoiding some damage depending on the available Movement of the attacker and the amount of available tiles behind the Slinger. Keeps the ability on upgrade."}},{{"type":"Bias","name":"Hill","desc":""}}]}},"India":{{"leader":"Gandhi","entries":[{{"type":"Ability","name":"Population Growth","desc":"Settling Cities produces twice as much Unhappiness. Unhappiness generated from the number of Citizens in the empire is halved. (Cities provide an additional +3 Unhappiness.)"}},{{"type":"Building","name":"Mughal Fort","desc":"Replaces the Castle. In addition to the typical defensive perks, provides +4 Culture (from 2) , and +2 Happiness . +2 Tourism at Flight. No longer requires Walls to construct."}},{{"type":"Unit","name":"War Elephant","desc":"Replaces the Chariot Archer. 8 Melee Strength (from 9, vs. 6) and 11 Ranged Strength (vs. 10) . Slightly slower (3 Movement vs. 4) and more costly to produce (46 hammers vs. 37) . Does not receive Movement penalties in Rough terrain and does not require Horses to construct. Arrives with Swift Charge (+50% bonus against Melee foes; +25% against Gunpowder foes). Keeps Swift Charge on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Indonesia":{{"leader":"Gajah Mada","entries":[{{"type":"Ability","name":"Spice Islanders","desc":"Receive 2 copies of unique Luxury Resources in the first 3 Cities founded on separate landmasses. These Cities may never be Razed. (Each City must have its own unique landmass to receive a Luxury.) (Note that the unique Luxuries spawn as resource tiles beneath these Cities; if the City is settled atop an existing resource, the new Luxury will overwrite it. These resources yield +2 base gold again.)"}},{{"type":"Building","name":"Candi","desc":"Replaces the Garden. In addition to the typical perks, provides +2 Faith and an additional +2 Faith for each Religion with at least 1 Follower in the City. Unlike the Garden, the Candi may be built without access to freshwater. 25% less costly to construct (60 hammers vs. 80)."}},{{"type":"Unit","name":"Kris Swordsman","desc":"Replaces Swordsmen. 15 Strength (from 14, vs. 14) . After its first combat, this Unit receives one of many unique promotions at random: Invulnerability: +30% bonus when foe initiates combat. Restore +20 additional HP while fortified. Sneak Attack: Flanking bonus increases to +15% per ally (vs. +10%). Heroism: Radiates a +15% Combat Bonus to allies within 2 tiles as if it were a Great General. Ambition: +50% bonus when initiating combat, but a -20% penalty when defending. Restlessness: +1 Movement; Unit may attack twice. Recruitment: Heal 50 HP from non-Barbarian kills. (Harmful promotions Enemy Blade and Evil Spirits have been removed.) The gained promotion is kept on upgrade. If not gained a promotion and upgraded, it can still get a promotion at random."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Ireland":{{"leader":"Michael Collins","entries":[{{"type":"Ability","name":"Gaelic Revival","desc":"+2 Happiness from all National Wonders (excluding the Palace). All Cities receive a +15% Production bonus towards buildings required for National Wonders."}},{{"type":"Unit","name":"Fenian","desc":"Replaces Riflemen. Significantly stronger (40 Strength vs. 34) and significantly cheaper to construct (110 hammers vs. 150) ."}},{{"type":"Building","name":"Irish Pub","desc":"Replaces the Zoo. In addition to the typical perks, provides +3 Food in the City and requires no Gold maintenance. +1 Production from Wine, Tea, Coffee and Tobacco resources in the City."}},{{"type":"Bias","name":"None","desc":""}}]}},"Iroquois":{{"leader":"Hiawatha","entries":[{{"type":"Ability","name":"The Great Warpath","desc":"Units move through Forests and Jungles in friendly territory as if they were Roads. Forests and Jungles can be used to establish a City Connection upon the discovery of the Wheel, and extend the range of friendly Caravans like Roads."}},{{"type":"Building","name":"Longhouse","desc":"Replaces the Workshop. In addition to the typical perks, the Longhouse provides +1 Production to all worked Forest and Jungle tiles in the City. Significantly cheaper to construct (50 hammers from 66, vs. 80). The Longhouse now retains the +10% Production bonus of the Workshop. Provides +1 Production to Hardwood resources in the City."}},{{"type":"Unit","name":"Mohawk Warrior","desc":"Replaces Swordsmen. 15 Strength (from 14, vs. 14). Receives a +33% Combat Bonus fighting in Forests and Jungles. Does not require Iron. Keeps the ability on upgrade."}},{{"type":"Bias","name":"Forest","desc":""}}]}},"Israel":{{"leader":"David","entries":[{{"type":"Ability","name":"Promised Land","desc":"+1 Culture from Pastures."}},{{"type":"Unit","name":"Maccabee","desc":"Replaces Swordsmen. Earn 100% of foe\u2019s Strength as Faith from kills. Significantly cheaper to construct (40 hammers vs. 50) . Does not require Iron to construct."}},{{"type":"Building","name":"King Solomons Temple","desc":"Replaces the National College. In addition to the typical perks, grants +3 faith and awards 50 science and 50 faith upon expending a great person. May not be built with production and instead has to be bought with faith ( 260) or gold (200) (on quick speed) ."}},{{"type":"Bias","name":"None","desc":""}}]}},"Italy":{{"leader":"Vittorio Emanuele III","entries":[{{"type":"Ability","name":"Rinascimento","desc":"+1 Culture from Specialists. Receive 250 Golden Age points upon completion of a Policy Tree, and if already in a Golden Age, extends it by 3 turns."}},{{"type":"Unit","name":"Pittore","desc":"Replaces the Great Artist. Golden Ages started by a Pittore last for 9 turns (vs. 6) , scaling to 14 turns with Chichen Itza or Universal Suffrage, but will not increase further with both."}},{{"type":"Building","name":"Basilica","desc":"Replaces the Museum. Available at Industrialization (instead of Archeology). +20% faster Great Person generation in the City. Houses 2 slots for Great Artist Specialists, and generates 2 Great Artist Points each turn."}},{{"type":"Bias","name":"None","desc":""}}]}},"Japan":{{"leader":"Oda Nobunaga","entries":[{{"type":"Ability","name":"Bushido","desc":"Units fight at full Strength even when injured. +1 Culture from all Fishing Boats and +2 Culture from Atolls."}},{{"type":"Building","name":"Dojo","desc":"Replaces the Barracks. In addition to the typical perks, provides +2 Science in the City and grants Units trained in this City an unconditional +10% Combat Bonus."}},{{"type":"Unit","name":"Samurai","desc":"Replaces Longswordsmen. Arrives with Shock I (+15% bonus in Open terrain) and Great Generals II (this Unit will boost progress towards Great Generals through combat more than normal). While embarked, the Samurai may construct Fishing Boats in 4 turns. Upgrades into Rifleman instead of Musketman. Keeps the promotion and the Great Generals II ability on upgrade."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Jerusalem":{{"leader":"Fulk V","entries":[{{"type":"Ability","name":"Holy Land","desc":"+1 Faith from Luxury Resources."}},{{"type":"Unit","name":"Crusader","desc":"Replaces Longswordsmen . Available at Theology (instead of Steel) . Much weaker (17 Strength vs. 21) , but receives a +20% Combat Bonus in foreign territory and may enter rival territory without Open Borders. Earns Faith from kills equal to twice the Strength of defeated foes. Cheaper to construct (70 hammers vs. 80). Keeps the abilities on upgrade."}},{{"type":"Building","name":"Outremer","desc":"Replaces the Courthouse. In addition to the typical perks, provides +2 Culture, Faith and Happiness in the City. Costs 2 gold maintenance."}},{{"type":"Bias","name":"None","desc":""}}]}},"Khmer":{{"leader":"Suryavarman II","entries":[{{"type":"Ability","name":"Grand Baray of Angkor","desc":"Receive a free Baray in the Capital."}},{{"type":"Building","name":"Baray","desc":"Replaces the Garden. In addition to the typical perks, 10% of Food is carried over after a Citizen is born. +2 Food and +2 Faith at Drama and Poetry. The Baray may be built without access to freshwater."}},{{"type":"Unit","name":"Ballista Elephant","desc":"Replaces the Trebuchet, available at Machinery (instead of Physics). 18 Ranged Strength (vs. 14) , 20 Melee (vs. 12) , and 3 Movement (vs. 2) . Gains a +100% bonus initiating combat with Cities (vs. +200%) . Unlike the Trebuchet, the Ballista Elephant does not receive a Sight penalty. Receives the Feared Elephant (enemy Units adjacent to this Unit receive a -10% combat penalty) promotion. (Feared Elephant will not stack.) Slightly more costly to construct (90 hammers vs. 80). Loses all abilities on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Kilwa":{{"leader":"Ali ibn al-Hassan Shirazi","entries":[{{"type":"Ability","name":"Merchant Economy","desc":"Gain +5% Growth (excess Food) in the City for each foreign Trade Route departing the City. +4 Gold from internal Trade Routes. (Extra Food is updated the turn after the Trade Route is established.)"}},{{"type":"Unit","name":"Dhow","desc":"Replaces the Caravel, available at Compass (instead of Astronomy). Costs less to produce (70 hammers vs. 80) , but has 17 Strength (vs. 20) . Arrives with the Boarding Party I (+15% Combat Bonus against Naval Units) promotion. Promotion is kept on upgrade."}},{{"type":"Building","name":"Coral Port","desc":"Replaces the Workshop. In addition to the typical perks, provides varying yields to certain Coastal resources: +2 Production from Coral +1 Culture from Fish, Pearls, and Crabs +2 Gold from Whales"}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Kongo":{{"leader":"Mvemba a Nzinga","entries":[{{"type":"Ability","name":"Heart of Africa","desc":"Borders naturally expand 33% faster."}},{{"type":"Unit","name":"Ngao","desc":"Mbeba Replaces Swordsman. 15 Strength (vs. 14) . May not initiate combat, but gains double Strength defending from Ranged attacks. Does not require Iron to produce. Keeps ability on upgrade."}},{{"type":"Building","name":"Slave Market","desc":"Replaces Colosseum. Subtracts 1 Citizen in the City upon completion. Provides +4 Production and +5 Gold, with an additional +3 Production and +2 Gold at Industrialization. Does not provide Happiness and does not require Gold maintenance."}},{{"type":"Bias","name":"Jungle, avoid desert","desc":""}}]}},"Korea":{{"leader":"Sejong","entries":[{{"type":"Ability","name":"Scholars of the Jade Hall","desc":"+1 (from +2) Science from Specialists, +2 Science from Great Person tile improvements, and +1 Science from the Palace."}},{{"type":"Unit","name":"Hwach\u2019a","desc":"Replaces the Trebuchet. Massive 26 Ranged Strength (vs. 14) , but slightly less Melee Strength (11 vs. 12) . Suffers no Sight penalty, but receives no bonus initiating combat with Cities. Receives the Indirect Fire promotion."}},{{"type":"Unit","name":"Turtle Ship","desc":"Replaces the Caravel. Massive 36 Strength (vs. 20) , but receives no Sight bonus, may not Withdraw from Melee attacks, and possesses 4 Movement (vs. 5). Cannot enter Ocean tiles."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Lithuania":{{"leader":"Vytautas","entries":[{{"type":"Ability","name":"Baltic Mythology","desc":"Great Prophets generated by or given to Lithuania cannot found a Religion and cannot create a holy site. Instead they can create a Sacred Grove. The first Great Prophet spawns with 50% less faith. Your second Great Prophet would still be the regular cost. On quick speed, this would mean your first Prophet costs 100 Faith, but the second one would cost 260 faith again"}},{{"type":"Improvement","name":"Sacred Grove","desc":"Provides +1 Food, Production, Culture, Science and Faith. Each yield increases by +1 upon the discovery of Philosophy, Theology, Acoustics, Archeology, Plastics, Computers, and the Internet (for a total of 8 of each yield)."}},{{"type":"Unit","name":"Pestininkas","desc":"Replaces the Pikeman. Identical combat capabilities, but is significantly cheaper to construct (40 Production vs. 60) ."}},{{"type":"Building","name":"Grand Cathedral of Vilnius","desc":"Behaves like a Grand Temple but can be constructed without the presence of religion and may be built outside of a Holy City. Cheaper to construct (63 hammers vs. 84) ."}},{{"type":"Bias","name":"Plains","desc":""}}]}},"Macedonia":{{"leader":"Alexander","entries":[{{"type":"Ability","name":"Macedonian Discipline","desc":"Barracks and Armories provide +1 Food, Culture and Happiness in the City."}},{{"type":"Great Person","name":"Hetairoi","desc":"Replaces the Great General. Considered a Mounted Unit with scaling Combat Strength and 4 Movement with identical mechanics to Horsemen, and arrives with several promotions: Leadership: G rants the Great General bonus to Units within 2 tiles as a General would, Great Generals II: This Unit will earn progress towards Great Generals through combat significantly faster, Charge: +33% bonus vs. wounded foes, Heavy Charge: E nemy Units will retreat if they receive more damage than this Unit; this Unit deals +50% damage to defenders incapable of retreat. Can upgrade to a Knight, but will lose its ability to create Citadels. Requires a Horse resource. (If no Horses are available when this unit is generated, it receives a Strategic Resource penalty!) Keeps all abilities on upgrade. Combat strength scales with eras as follows: Ancient Era: 12 Strength Classical Era: 16 Strength Medieval Era: 23 Strength Beings with +15 XP Renaissance Era: 30 Strength Beings with +30 XP (total) Industrial Era: 40 Strength Gains the march promotion Modern Era: 65 Strength Gains a 50% strength bonus against land units Atomic Era: 80 Strength Gains an additional movement Information Era: 100 Strength Begins with +60 XP (total)"}},{{"type":"Unit","name":"Sarissophoroi","desc":"Replaces Pikemen. Available at Philosophy (instead of Civil Service). Weaker, (14 Strength vs. 16) , but gains a +100% Combat Bonus (vs. +50%) against Mounted units. Loses the ability on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Madagascar (Malagasy)":{{"leader":"Ralambo","entries":[{{"type":"Ability","name":"Sacred Hills of Imerina","desc":"Purchasing cost of units or buildings with faith is reduced by 25%. Holy sites yield +1 culture and +1 faith, which increases by +1 culture and +1 faith for every other adjacent holy site."}},{{"type":"Unit","name":"","desc":"Mpiambina - Replaces the Inquisitor. 130 base faith cost. (Modified by UA) Ranged Unit with 8 Melee Strength, 14 Ranged Strength, 1 Range, and 3 Movement. Also capable of melee attacks. Receives one promotion from a unique pool after its first round of combat: Kelimazala - Allows Unit to attack twice. Ramahavaly - +50% defensive bonus, -50% attacking penalty. Manjakatsiroa - +25% Combat Bonus in friendly territory. Rafantaka - Adjacent allies heal +15 HP while Fortified. This Unit heals every turn, even if an action was performed. Mosasa - + 4 Sight , +4 Range. Rabehaza - Gain a chance to capture defeated enemies. Ambohimanambola - Grant the Great General bonus to nearby allies. Sehatra - Heal fully from every kill. Lambamena - +100% bonus initiating combat with Cities. Famadiahona - Knock back foes that take more damage than this Unit receives during combat. This unit gains a chance to Withdraw from Melee combat. Razana - +3 Movement. Great Generals that start their turn on the same tile as this Unit receive +2 Movement. Masina - +75% bonus against wounded foes."}},{{"type":"Building","name":"Rova","desc":"Replaces Walls. In addition to the typical perks, provides +2 Food and +2 faith. Can be purchased with faith. (150 faith base cost on quick, decreased by Sacred Hills of Imerina and the piety policy Mandate of Heaven, reducing it to 70 faith together.)"}},{{"type":"Bias","name":"None","desc":""}}]}},"Manchuria":{{"leader":"Nurhaci","entries":[{{"type":"Ability","name":"Eight Banners","desc":"Mounted, Armored, and Bomber Units receive the Volley promotion (+50% bonus vs. Fortified Units and Cities) and pay no Gold maintenance."}},{{"type":"Building","name":"Canton Factory","desc":"Replaces the Bank. In addition to the typical perks, boosts City gold output by an additional +10% (+35% vs. 25%) , may house an additional Great Merchant Specialist, and is half as costly to produce (67 hammers vs. 134) ."}},{{"type":"Unit","name":"Qianlong Cavalry","desc":"Replaces Cavalry. Stronger and faster (36 Strength vs. 34 and 5 Movement vs. 4) and arrives with Great Generals I (this Unit will boost progress towards Great Generals through combat more than normal). Keeps ability on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Maori":{{"leader":"Te Rauparaha","entries":[{{"type":"Ability","name":"Mana of Tumatauenga","desc":"During the first 5 turns, all units (except Recon units) receive +2 Movement and +1 Sight. After this, units receive +2 Movement during the first turn they appear. (Captured units, such as Workers, will receive this bonus on their first available move.)"}},{{"type":"Unit","name":"Maori Warrior","desc":"9 Strength (from 8, vs. 8) . Possesses the Haka War Dance (enemy Units adjacent to this Unit receive a -10% combat penalty) promotion. (Haka War Dance will not stack. In Brave New World, this unit belonged to Polynesia.) Keeps ability on upgrade."}},{{"type":"Improvement","name":"Pa","desc":"Available at Engineering. +2 Food and +1 Faith. May only be built on Hills without resources and cannot be constructed adjacent to one another. Gains an additional +1 Food at Civil Service and Fertilizer. Units stationed on a Pa receive a +40% defensive bonus."}},{{"type":"Bias","name":"None","desc":""}}]}},"Maurya":{{"leader":"Ashoka","entries":[{{"type":"Ability","name":"Inscriptions of the Dharma","desc":"All military non-armor, non-mounted, non-air units receive a 20% combat bonus when defending."}},{{"type":"Unit","name":"Ashoka\u2019s Envoy","desc":"Replaces the Missionary. Has 5 base movement (vs. 4), may move immediately after purchase and ignores terrain cost. Awards +1 Science, +1 Culture, +1 Gold and +1 Faith per citizen converted to a religion upon spreading one. Fully converting a city to a religion awards an additional 8 of these yields."}},{{"type":"Building","name":"Pillar of Ashoka","desc":"Replaces the Monument. Half as costly to construct (13 hammers vs. 26) and requires no Gold maintenance. Available at Pottery (instead of immediate availability)."}},{{"type":"Bias","name":"None","desc":""}}]}},"Maya":{{"leader":"Pacal","entries":[{{"type":"Ability","name":"The Long Count","desc":"After researching Theology, choose a"}},{{"type":"Great Person","name":"at the end of each Mayan calendar cycle (every 394 in-game \u2018years\u2019). Each type of Great Person may only be chosen once.","desc":"(Great People chosen through Long Count cycles will delay the generation of the next naturally earned Great Person.) (Long Count intervals are based on fixed intervals regardless of when Theology was researched. Currently, these intervals are (on Quick speed): Turns 22 / 28 / 35 / 42 / 48 / 57 / 67 / 77 / 88 / 102 / 122 / 152 )"}},{{"type":"Building","name":"Pyramid","desc":"Replaces the Shrine. In addition to the typical perks, provides an additional point of Faith (2 vs. 1) and +2 Science, but cost slightly more to construct (26 hammers vs 20)."}},{{"type":"Unit","name":"Atlatist","desc":"Replaces the Archer. Available immediately with identical combat capabilities and slightly less costly to construct (24 hammers vs. 27)"}}]}},"Mongolia":{{"leader":"Genghis Khan","entries":[{{"type":"Ability","name":"Mongol Terror","desc":"Gain a +30% Combat Bonus in combat with City-States and their Units. Mounted and Armored Units receive +1 additional Movement. Upon the discovery of Chivalry, cities yield an additional +1 Food from all Horse resources. Reveal an additional Horse resource tile within the borders of every current Mongol city. (These Horses will not spawn over existing resources or features (such as forests, mountains, or Natural Wonders), but may spawn on Hills if necessary.)"}},{{"type":"Great Person","name":"Khan","desc":"Replaces the Great General. In addition to the typical traits, the Khan may move further (5 Movement vs. 4) and greatly improves the healing capabilities of adjacent Fortified Units by an additional +15 HP. (Note that this healing bonus will not apply to Units stationed on the same tile as the Khan.)"}},{{"type":"Unit","name":"Keshik","desc":"Replaces the Knight. A Ranged Mounted Unit with 17 Strength (from 15) and 17 Ranged Strength (from 16). May move after combat and receives no penalty in combat with Cities. Arrives trained with Great Generals I (this Unit will boost progress towards Great Generals through combat more than normal) and Quick Study (+50% XP earned through combat). Keeps abilities on upgrade."}},{{"type":"Bias","name":"Plains","desc":""}}]}},"Moors":{{"leader":"Abd-ar-Rahman III","entries":[{{"type":"Ability","name":"Glory of Al-Andalus","desc":"All Cities gain a +30% Production bonus towards buildings (not including Wonders) during the Medieval Era, decreasing to +15% in the Renaissance Era. (This ability updates on each turn or when technology is gained through other means. Upon entering the Industrial Era, the production bonus ends.)"}},{{"type":"Unit","name":"","desc":"Granadine Cavalry Replaces the Lancer. A Ranged Mounted unit with identical Combat Strength to the Lancer in addition to 29 Ranged Strength and 2 Range. The Granadine Cavalry sees no bonuses against Mounted Units and may not move after initiating combat, but may attack Cities without penalty."}},{{"type":"Building","name":"Alcazaba","desc":"Replaces the Castle. Retains defensive capabilities in addition to providing +3 Science and +10% Food in the City."}},{{"type":"Bias","name":"None","desc":""}}]}},"Morocco":{{"leader":"Ahmad al-Mansur","entries":[{{"type":"Ability","name":"Gateway to Africa","desc":"Receive +2 Culture (from +1) and an additional +4 Gold (from +3) for each external Trade Route sent by or to Morocco. Rivals receive an additional +2 Gold for sending Trade Routes to Morocco. Receive an additional +1 Gold and Culture for every new Era entered. (In the Ancient Era, Morocco will only receive +2 / +4; increasing to +3 / +5 in Classical, etc\u2026)"}},{{"type":"Improvement","name":"Kasbah","desc":"Available at Guilds . May only be built on Desert tiles (excluding Oases). Provides +2 (from +1) Food +1 Production, and +1 Gold. Units gain a +50% Combat Bonus defending this tile."}},{{"type":"Unit","name":"Berber Cavalry","desc":"Replaces Cavalry. In addition to the typical traits, receives a +25% Combat Bonus inside friendly territory and a +50% bonus fighting in Desert, Floodplains, and Oases. Keeps abilities on upgrade."}},{{"type":"Bias","name":"Desert, avoid wetlands","desc":""}}]}},"Mughals":{{"leader":"Akbar I","entries":[{{"type":"Ability","name":"Diverse Administrators","desc":"Cities with a dominant foreign religion yield +1 Science, +1 Culture, and +1 Gold for every 5 population in the city. Holy cities of Foreign Religions receive the same bonus if at least one of your original cities has been converted to that Religion. +2 Golden Age points from World Wonders."}},{{"type":"Building","name":"","desc":"Mansabdari Estate - Replaces Caravansary, available at Currency (vs. Horseback Riding) . Yields +5 City strength, +2 Gold and +3 Culture if the city has a garrison"}},{{"type":"Unit","name":"Zafarbaksh","desc":"Replaces Cannon. Costs 33% more production, 3 movement (vs. 2, does not stay on upgrade) . Has no sight penalty and is stronger (18 melee strength, vs. 14, 23 ranged strength, vs. 20)"}},{{"type":"Bias","name":"None","desc":""}}]}},"Mysore":{{"leader":"Hyder Ali","entries":[{{"type":"Ability","name":"Brahmin Elite","desc":"Specialists provide +1 Food and Production, but generate +50% more Unhappiness. (This Unhappiness penalty interacts additively instead of multiplicatively with other bonuses or penalties. Universal Suffrage, for example, which reduces Specialist Unhappiness by 50%, would cancel out this penalty.)"}},{{"type":"Unit","name":"Rocket Corps","desc":"Replaces Artillery. Increased Melee Strength (23 vs. 21) . Unlike Artillery, the Rocket Corps may attack without prior setup."}},{{"type":"Building","name":"Mysore Palace","desc":"Replaces the Hermitage. Available at Chivalry (instead of Architecture). Requires Walls to be built in all Cities. In addition to the typical perks, provides +3 Production, Science and Gold. Upon completion, the empire enters a Golden Age."}},{{"type":"Bias","name":"Hill","desc":""}}]}},"Nabataea":{{"leader":"Aretas III","entries":[{{"type":"Ability","name":"Spring of Moses","desc":"Freshwater Farms receive +1 Food at Mathematics rather than Civil Service."}},{{"type":"Unit","name":"Zabonah","desc":"Replaces the Scout. May not attack. 3 Movement (vs. 2) . May discover Cities from an additional +3 tiles away. When this Unit discovers a Capital or City-State, gain +10 Gold for the Empire. Will not upgrade from Ruins. (The Zabonah must make Sight contact with players and City-States to \u2018meet\u2019 them and will not \u2018meet\u2019 when discovering Cities only due to its expanded range.)"}},{{"type":"Building","name":"Rock-Cut Tomb","desc":"Replaces the Caravansary. In addition to the typical perks, provides an additional +1 Gold (3 vs. 2) and +2 Food in the City. Gains an additional +1 Food for each Trade Route departing the City."}},{{"type":"Bias","name":"Desert","desc":""}}]}},"Netherlands":{{"leader":"William","entries":[{{"type":"Ability","name":"Dutch East India Company","desc":"+1 Happiness from each unique Luxury in the empire and +1 Gold from Luxury resource tiles."}},{{"type":"Improvement","name":"Polder","desc":"Available at Guilds. May be built on Marsh and Floodplain tiles and on Lakes or Coastal tiles with at least 3 adjacent land tiles. +3 Food if constructed on Marsh, +2 Food if constructed on Lakes or Coast. Gains +1 Production and +2 Gold at Economics."}},{{"type":"Unit","name":"Sea Beggar","desc":"Replaces the Privateer. 27 Strength (from 25, vs. 27). Arrives with the Supply (may heal outside friendly territory; heals 15 HP each turn), Coastal Raider (+20% bonus when attacking Cities; Steal Gold equal to 33% of inflicted damage) and Boarding Party I (+15% bonus in Melee combat with Naval Units) promotions. Promotions are kept on upgrade, heal on unit kill ability is kept on upgrade."}},{{"type":"Bias","name":"Coast; near Wetlands (Marsh and Floodplains) (previously Grassland).","desc":""}}]}},"New Zealand":{{"leader":"Michael Joseph Savage","entries":[{{"type":"Ability","name":"Where She Goes","desc":"Earn 10 Faith, 6 Culture, 40 Gold, or 12 Science when meeting a Civilization or City-State for the first time. (This bonus is randomly chosen.)"}},{{"type":"Unit","name":"Defender","desc":"Replaces the Ironclad. 50 Strength (vs. 45) . Has Coast guard ability, which gives a +15% bonus inside friendly territory and ignores Zone of Control movement penalties when within 2 tiles of an owned City or a City from a player whom you have an active Declaration of Friendship with. In addition to the typical traits, arrives with Cover I ( gain a +33% bonus defending against Ranged attacks). Doesn\u2019t require Coal. Promotions and Coast guard ability is kept on upgrade"}},{{"type":"Unit","name":"Maori Battalion","desc":"Replaces Infantry. Possesses the promotions and traits"}}]}},"Normandy":{{"leader":"William the Conqueror","entries":[{{"type":"Ability","name":"Castle Builders","desc":"Units gain a +10% Combat Bonus outside of friendly territory. Cities receive +5 additional Strength."}},{{"type":"Improvement","name":"Motte and Bailey","desc":"Available at Engineering. +1 Food and Production. +2 Culture at Flight. Cannot be built adjacent to one another. Units gain a +25% Combat Bonus defending this tile."}},{{"type":"Unit","name":"Pedite","desc":"Replaces the Longswordsman. Available at Metal Casting (instead of Steel). 20 Strength (vs. 21) . Marginally cheaper to produce (77 hammers vs. 80) . Arrives with Shock I (+15% bonus vs. foes in Open terrain) and may construct Motte and Baileys. Promotions are kept on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Norway":{{"leader":"Harald Hardrada","entries":[{{"type":"Ability","name":"Nordic Bounty","desc":"+1 Faith from Coastal resources. +2 Food and +1 Production from Snow tiles."}},{{"type":"Unit","name":"Ski Infantry","desc":"Replaces Great War Infantry (previously Riflemen). Matches the strength of its counterpart and arrives with Drill I and II (total of +30% bonus fighting in Rough terrain) and the unique Ski Infantry promotion (+25% bonus fighting in Snow, Tundra and Hills without Forests or Jungles; move twice as far through these tiles). (In Brave New World, this unit belonged to Denmark.) Promotions and the ability is kept on upgrade."}},{{"type":"Building","name":"Stave Church","desc":"Replaces the Temple. +1 Production from Tundra and Snow tiles. Cheaper to construct (36 hammers vs. 50) and requires no Gold maintenance."}},{{"type":"Bias","name":"Coast, tundra, avoid desert","desc":""}}]}},"Nubia":{{"leader":"Amanitore","entries":[{{"type":"Ability","name":"Ta Seti","desc":"Begin with a free Apedemak\u2019s Bow."}},{{"type":"Unit","name":"Apedemak\u2019s Bow","desc":"Replaces the Scout and has identical Strength, but is classified as a Ranged unit and may attack with 3 Range. Will not upgrade from Ruins, but can upgrade with Gold like the Scout when appropriate."}},{{"type":"Building","name":"Blast Furnace","desc":"Replaces the Forge. In addition to the typical perks, provides"}}]}},"Oman":{{"leader":"Saif bin Sultan","entries":[{{"type":"Ability","name":"Chain of the Earth","desc":"All Naval Units ignore Zone of Control movement penalties. Receive a free Seaport in conquered Cities."}},{{"type":"Unit","name":"Baghlah","desc":"Replaces the Galley. Greater Melee Strength (8 Strength vs. 6) and Ranged Strength (12 vs. 8). Moves further (4 Movement vs. 3) and receives Faith and Gold from kills equal to the Strength of the defeated foe. Arrives with the Coastal Raider I (+15% bonus initiating combat with Cities; steal Gold equal to 33% of inflicted damage) promotion. Promotions are kept on upgrade, but the kill reward ability is lost. The Coastal Raider promotion does provide the combat strength against cities, but not the gold on attack, as it is not a melee attack."}},{{"type":"Building","name":"Minaa\u2019","desc":"Replaces the Harbor. Enemy Naval Units take 30 damage if they end their turn next to this City. Provides +5 City Strength, and +2 Production for each Trade Route departing the City. Allows units to Airlift between Cities with Minaa\u2019s."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Ottomans":{{"leader":"Suleiman","entries":[{{"type":"Ability","name":"Millets","desc":"Each City gains +1 Local Happiness for each Religion present in the City. Receive Faith when promoting Units equal to the required experience (not when insta-healing)."}},{{"type":"Unit","name":"Sipahi","desc":"Replaces the Lancer. In addition to the typical traits, receives +1 Sight and Movement (5 vs. 4) and may Pillage without movement penalty. Available at Printing Press (previously Metallurgy) and costs less to produce (99 hammers from 123, vs. 123). Promotions and the ability are kept on upgrade."}},{{"type":"Unit","name":"Janissary","desc":"Replaces Musketmen. Gains a +25% bonus when initiating combat and heals 50 HP from kills. Less costly to produce (80 from 100, vs. 100). Keeps abilities on upgrade."}},{{"type":"Bias","name":"None (previously Coast)","desc":""}}]}},"Palmyra":{{"leader":"Zenobia","entries":[{{"type":"Ability","name":"Pearl of the Desert","desc":"Cities provide fresh water to all adjacent tiles. Gardens, Water Mills and Hydro Plants ignore terrain requirements."}},{{"type":"Building","name":"Palmyrene Theater","desc":"Replaces Amphitheaters. Provides +2 Gold. +1 Production, Gold, and Culture from Oases, +1 Production from Lake tiles. Doesn\u2019t require Gold maintenance."}},{{"type":"Unit","name":"Clibanarius","desc":"Replaces Horsemen. 14 Strength (vs. 12), arrives with Heavy Charge (enemy Units will retreat if they receive more damage than this Unit; this Unit deals +50% damage to defenders incapable of retreat) . Keeps ability on upgrade."}},{{"type":"Bias","name":"Desert","desc":""}}]}},"Papal States (Vatican)":{{"leader":"Urban II","entries":[{{"type":"Ability","name":"The Holy See","desc":"Owned occupied Cities that follow your Religion instantly receive a Courthouse. +2 Food, +1 Gold and +1 Culture from Great Person Tile Improvements. Units receive faith from kills."}},{{"type":"Unit","name":"Swiss Guard","desc":"Replaces Landsknecht. Does not require the Mercenary Army policy, but still must be purchased. Available at Economics. More expensive to purchase (210 Gold vs. 160) , but is faster (3 Movement vs. 2) and significantly stronger (25 Strength vs. 16) . Receives Faith (double) and Gold from kills equal to the Strength of the defeated foe, receives gold from damage inflicted to cities and arrives with Medic I and II (adjacent allies heal a total of +10 additional HP while Fortified). May upgrade into Riflemen. Promotions and abilities are kept on upgrade."}},{{"type":"Building","name":"Saint Peter\u2019s Basilica","desc":"Replaces the Grand Temple. In addition to the typical perks, provides a free Cathedral in the City and spawns a Great Prophet near the City. Receive 2 delegates in the World Congress."}},{{"type":"Bias","name":"None","desc":""}}]}},"Persia":{{"leader":"Darius I","entries":[{{"type":"Ability","name":"Achaemenid Legacy","desc":"Golden Ages last +50% longer. During a Golden Age, Units receive +1 Movement. No longer receives a +10% Combat Bonus during a Golden Age."}},{{"type":"Building","name":"Satrap\u2019s Court","desc":"Replaces the Bank. In addition to the typical perks, provides an additional +1 Gold (3 vs. 2) and +2 Happiness."}},{{"type":"Unit","name":"Immortal","desc":"Replaces Spearmen. 12 Strength (vs. 11). In addition to the typical traits, the Immortal heals an additional +10 HP while fortified. Keeps ability on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Philippines":{{"leader":"Emilio Aguinaldo","entries":[{{"type":"Ability","name":"The Good Fight","desc":"Your Civilian Units receive +2 Movement in your own territory. The first two Cities you settle (after your Capital) begin with +1 extra Citizen."}},{{"type":"Unit","name":"Gerilya","desc":"Replaces Expeditionary Forces. Boasts a whopping 55 Strength (vs. 40) . In addition to the Force\u2019s innate promotions, the Gerilya arrives with Volley (gain a +50% bonus against Fortified Units and Cities), 3 Movement (vs. 2) +3 additional Movement while embarked, and +1 Sight while embarked. Keeps abilities on upgrade."}},{{"type":"Building","name":"National Church","desc":"Replaces the Zoo, but available at Compass instead of Printing Press. In addition to the typical perks of a Zoo, the National Church provides +1 Culture and +2 Faith, and provides +15 XP to Units trained in the City. Less costly to construct (100 hammers vs. 120)."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Phoenicia":{{"leader":"Hiram","entries":[{{"type":"Ability","name":"Skillful Traders","desc":"Receive a Square Sail Ship at Sailing. Upon the discovery of Optics, newly settled Cities begin with +1 extra Citizen and you earn 50 gold."}},{{"type":"Unit","name":"Square Sail Ship","desc":"Replaces the Trireme. Can see 1 tile further than its counterpart and receives the Supply (may heal outside friendly territory; heals 15 HP each turn) promotion. Slightly cheaper to train (25 hammers vs. 30) . Promotions are kept on upgrade."}},{{"type":"Building","name":"Trade Harbor","desc":"Replaces the Harbor. In addition to the typical perks, provides +15% production towards Naval units and +1 Production from all Coastal resources."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Poland":{{"leader":"Casimir III","entries":[{{"type":"Ability","name":"Solidarity","desc":"Social Policies require -10% less Culture to adopt."}},{{"type":"Building","name":"Ducal Stable","desc":"Replaces the Stable. In addition to the typical perks, provides +1 Gold from each Horse, Sheep, Cattle and Maize resource in the City. +15 XP for Mounted Units trained in the City. Requires no Gold maintenance."}},{{"type":"Unit","name":"Winged Hussar","desc":"Replaces the Lancer. 28 Strength (vs. 25) and 5 Movement (vs. 4) . In addition to the Lancer\u2019s innate promotions, arrives with the Heavy Charge ( enemy Units will retreat if they receive more damage than this Unit; this Unit deals +50% damage to defenders incapable of retreat) promotion. No longer arrives with Shock (+15% bonus vs. foes in Open terrain). Keeps ability on upgrade."}},{{"type":"Bias","name":"Plains","desc":""}}]}},"Polynesia":{{"leader":"Kamehameha","entries":[{{"type":"Ability","name":"Wayfinding","desc":"Units may embark and move through Oceans immediately. Embarked Units gain +1 Movement, +2 Sight (from +1) and double defensive Strength. Units gain a +10% Combat Bonus fighting within 2 tiles of a Moai."}},{{"type":"Improvement","name":"Moai","desc":"Available at Construction. Must be built on land adjacent to the coast. Provides +1 Culture and gains an additional +1 Culture for each adjacent Moai. Gains +1 Gold at Flight. You can now build Moais on tiles with resources and any resource it is constructed on will be improved."}},{{"type":"Unit","name":"Koa","desc":"Replaces Longswordsmen. 23 Strength (vs. 21) , requires no Iron resources, and arrives with the Amphibious (attack from the sea or over rivers without penalty) promotion. Obsolete at Rifling (instead of Gunpowder). Promotion is kept on upgrade."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Portugal":{{"leader":"Maria I","entries":[{{"type":"Ability","name":"Mare Clausum","desc":"Receive twice as much Gold from Resource Diversity from Trade Routes. Caravans and Cargo Ships are 33% less costly to construct."}},{{"type":"Building","name":"Feitoria","desc":"Replaces the Harbor. In addition to the typical perks, provides +1 Gold from Coastal resources. Naval units trained in this City receive +15 XP. Cheaper to construct (67 hammers vs. 80) and doesn\u2019t require Gold maintenance."}},{{"type":"Unit","name":"Nau","desc":"Replaces the Caravel. 6 Movement (vs. 5) . In addition to the typical perks, the Nau may perform a unique action when adjacent to foreign territory, gaining Gold for the empire and XP for the Unit scaling with the distance from Portugal\u2019s Capital. Keeps ability on upgrade, increased movement is lost."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Prussia":{{"leader":"Frederick","entries":[{{"type":"Ability","name":"Army with a State","desc":"All Land Units may receive unique Promotions upon leveling up. +50% Food and Production from internal Trade Routes."}},{{"type":"Building","name":"Fabrik","desc":"Replaces the Factory. 25% cheaper to produce (180 hammers vs. 240) and does not require Coal resources."}},{{"type":"Unit","name":"Landwehr","desc":"Replaces Riflemen. Available at Military Science instead of Rifling. Arrives trained with Blitz (may attack twice each turn) and Quick Study (+50% Experience earned through combat). 3 Movement (vs. 2). Promotions and abilities are kept on upgrade."}},{{"type":"Unique","name":"Promotions:","desc":"Tier 0 - Gehorsam : Designates Units capable of learning further Promotions. Tier 1 - Disziplin : +10% Combat Strength. Tier 2 - Fleiss : +25% bonus attacking, or Tapferkeit : +25% bonus defending. Tier 3 - Zielstrebigkeit : +1 Extra Attack (requires Fleiss), H\u00e4rte : +50% Defensive Strength vs. Ranged Units (requires Tapferkeit), or P\u00fcnktlichkeit : +1 Movement (requires either). ( Zielstrebigkeit cannot be chosen by Paradropping Units.) Tier 4 - Zuverl\u00e4ssigkeit : Gain a bonus when near Great Generals and Heal every turn (requires Zielstrebigkeit), Zur\u00fcckhaltung : +30% bonus in friendly territory (requires H\u00e4rte), or Pflichtbewusstsein : +50% bonus vs. Mounted and Armored Units in addition to +1 Movement (requires P\u00fcnktlichkeit)."}},{{"type":"Bias","name":"None","desc":""}}]}},"Romania":{{"leader":"Carol I","entries":[{{"type":"Ability","name":"Nihil Sine Deo -","desc":"Units gain a +10% Combat Bonus against wounded foes. +25% Culture output during Golden Ages (for a total of +45% Culture). Receive 100 Golden Age points upon capturing or liberating a city."}},{{"type":"Unit","name":"Vanator","desc":"Replaces the Gatling Gun. 31 Melee and Ranged Strength (vs. 30) and 2 Range (vs. 1) . Promotions are kept on upgrade."}},{{"type":"Building","name":"Painted Monastery","desc":"Replaces the Garden. In addition to the typical perks, provides +1 Production, +2 Faith, and +1 Great Artist point per turn. Does not require freshwater. Cheaper to construct (67 vs. 80 hammers) ."}},{{"type":"Bias","name":"None","desc":""}}]}},"Rome":{{"leader":"Augustus Caesar","entries":[{{"type":"Ability","name":"Glory of Rome","desc":"Gain +25% Production towards any Buildings that already exist in the Capital."}},{{"type":"Unit","name":"Ballista","desc":"Replaces the Catapult. 8 Melee Strength (vs. 7) and 10 Ranged Strength (vs. 8) . May attack without prior setup. Receives the Indirect Fire promotion. Loses ability on upgrade. Receives only a 100% bonus towards combat with cities (instead of 200%) ."}},{{"type":"Unit","name":"Legion","desc":"Replaces Swordsmen. 16 Strength (from 17, vs. 14) . May construct Roads and Forts."}},{{"type":"Bias","name":"None","desc":""}}]}},"Russia":{{"leader":"Catherine","entries":[{{"type":"Ability","name":"Siberian Riches","desc":"+1 Production from Iron, Coal, Aluminum, and Uranium resources and each of these resources, including horses, provide double quantity. All Strategic Resources may be improved instantly."}},{{"type":"Building","name":"Krepost","desc":"Replaces Barracks. In addition to the typical perks, reduces the Culture and Gold costs of acquiring tiles in the City by -25%."}},{{"type":"Unit","name":"Cossack","desc":"Replaces Cavalry. In addition to the typical traits, gains a +25% bonus (from +33%) in combat with wounded foes. Keeps ability on upgrade and Charge can still be picked as a promotion."}},{{"type":"Bias","name":"Tundra","desc":""}}]}},"Scotland":{{"leader":"Robert the Bruce","entries":[{{"type":"Ability","name":"Flower of Scotland","desc":"+2 Production, Science and Culture from Writer's, Artist's and Musician's Guilds. +33% faster Great Person generation in the Capital."}},{{"type":"Unit","name":"Gallowglass","desc":"Replaces Riflemen. Cheaper to construct (120 hammers vs. 150) and available at Metallurgy (instead of Rifling). 32 Strength (vs. 35) . Arrives with Altitude Training (+10% bonus fighting in Hills; move twice as fast through these tiles) and Volley (+50% bonus vs. Fortified Units and Cities). Keeps abilities on upgrade. Unlike Riflemen, requires 1 Iron resource to train."}},{{"type":"Building","name":"Ceilidh Hall","desc":"Replaces the Opera House. In addition to the typical perks, provides an additional +1 Culture and +3 Happiness in the City. (In Brave New World, this building belonged to the Celts.)"}},{{"type":"Bias","name":"Hill","desc":""}}]}},"Shoshone":{{"leader":"Pocatello","entries":[{{"type":"Ability","name":"Great Expanse","desc":"Founded Cities start with 4 additional tiles (previously 8) claimed. Units gain a +15% Combat Bonus in their own territory."}},{{"type":"Unit","name":"Pathfinder","desc":"Replaces the Scout. 27 hammers to produce (from 30, vs. 16). 8 Strength (vs. 5). When entering Ancient Ruins, the Pathfinder may select the benefit from the available pool. (This Unit upgrades into a Composite Bowman (instead of an Archer) from Ruins.) Keeps the ability on upgrade."}},{{"type":"Unit","name":"Comanche Riders","desc":"Replaces Cavalry. 5 Movement (vs. 4) and costs less to produce (132 hammers vs. 150) . Keeps the movement ability on upgrade."}},{{"type":"Bias","name":"None","desc":""}}]}},"Siam":{{"leader":"Ramkhamhaeng","entries":[{{"type":"Ability","name":"Father Governs Children","desc":"Gain +50% more Food, Culture Faith and Happiness from friendly City-States. Units from Military City-States arrive with an additional +10 XP and arrive +50% faster."}},{{"type":"Building","name":"Wat","desc":"Replaces the University. In addition to the typical perks, provides +3 Culture. Significantly cheaper to construct (80 vs 108 production) and requires no maintenance."}},{{"type":"Unit","name":"Naresuan\u2019s Elephant","desc":"Replaces the Knight. 25 Strength (vs. 20) , but 3 Movement (vs. 4) . Gains a +50% bonus against Mounted Units and does not require Horses to construct. Loses ability on upgrade."}},{{"type":"Bias","name":"avoid Forest","desc":""}}]}},"Sioux":{{"leader":"Sitting Bull","entries":[{{"type":"Ability","name":"Dwellers of the Plains","desc":"Units receive a +15% Combat Bonus fighting in Plains."}},{{"type":"Unit","name":"Buffalo Hunter","desc":"Replaces Composite Bowman. 12 Ranged Strength (vs. 11) 8 Melee (vs. 7) . Capable of constructing Tipis."}},{{"type":"Improvement","name":"Tipi","desc":"Available at Trapping. May only be constructed on flat Plains or flatland Deer / Bison resources and must be adjacent to a Luxury resource. Provides +1 Food, +1 Faith. Bonus +1 Culture if adjacent to a city and +1 Gold if adjacent to a River. Gains +1 Food at Civil Service and +1 Faith at Theology."}},{{"type":"Bias","name":"Plains","desc":""}}]}},"Songhai":{{"leader":"Askia","entries":[{{"type":"Ability","name":"River Warlord","desc":"Receive triple Gold from clearing Barbarian camps and capturing Cities. Land Units receive the Amphibious (attack from the sea or over rivers without penalty) promotion and ignore terrain costs moving along or across Rivers."}},{{"type":"Building","name":"Mud Pyramid Mosque","desc":"Replaces the Temple. In addition to the typical perks, provides +2 Culture and requires no Gold maintenance."}},{{"type":"Unit","name":"Mandekalu Cavalry","desc":"Replaces the Knight. Receives no penalty in combat with Cities and is slightly cheaper to produce (74 hammers vs. 80) . Loses the ability on upgrade."}},{{"type":"Bias","name":"River","desc":""}}]}},"Spain":{{"leader":"Isabella","entries":[{{"type":"Ability","name":"Seven Cities of Gold","desc":"Gain 100 Gold (previously 500 if first to discover and 100 otherwise) and +1 additional Happiness for the empire upon the discovery of a Natural Wonder. Tile yields (including Happiness yields) from Natural Wonders are doubled."}},{{"type":"Building","name":"Plaza de Toros","desc":"Replaces the Circus. In addition to the typical perks, provides +2 Culture, and may also be built with an improved source of Cattle in addition to Horses and Ivory."}},{{"type":"Unit","name":"Tercio","desc":"Replaces the Musketman. 26 Strength (vs. 24) and gains a +50% bonus against Mounted Units, but is slightly more expensive to construct. (107 hammers vs. 100). Loses the ability on upgrade. (This unit is classified as a Melee Unit and not a Gunpowder Unit.)"}},{{"type":"Bias","name":"Coast","desc":"Sumeria - Gilgamesh"}}]}},"Sweden":{{"leader":"Gustavus Adolphus","entries":[{{"type":"Ability","name":"Nobel Prize","desc":"+20% faster Great Person generation in all Cities. Receive 100 Influence and 200 Gold when gifting a Great Person to a City-State."}},{{"type":"Unit","name":"Carolean","desc":"Replaces Musketmen (instead of Riflemen). 3 Movement (vs. 2) . Identical strength; arrives with March (heal 10 HP each turn even if an action was performed). Promotions are kept on upgrade."}},{{"type":"Building","name":"Falu Gruva","desc":"Replaces Ironworks. Available at Metal Casting (instead of Machinery) . In addition to the typical perks, provides +1 Production to all Hill tiles in the City. Provides +6 Tourism at Flight."}},{{"type":"Bias","name":"Tundra, avoid desert","desc":""}}]}},"Switzerland":{{"leader":"Jonas Furrer","entries":[{{"type":"Ability","name":"Swiss Banks","desc":"Merchant Specialists provide +2 Production and Science."}},{{"type":"Building","name":"Ski Resort","desc":"Replaces Stadiums. Available at Electricity (instead of Refrigeration). Provides +3 Happiness (vs. 2), +3 Culture, and +5 Tourism. Provides an additional +5 Tourism and +2 Gold for every Mountain within 3 tiles of the City. Half as costly to construct (165 hammers vs. 330) and doesn\u2019t require Gold maintenance. Doesn\u2019t require a Zoo to construct."}},{{"type":"Building","name":"Reisl\u00e4ufer Post","desc":"Replaces Armories. Trained units receive the Mountaineer promotion (+10% Strength and +1 Movement at the start of turn when adjacent to Mountain tiles) . Doesn\u2019t require Gold maintenance. Spawns a Reisl\u00e4ufer upon completion."}},{{"type":"Unit","name":"Reisl\u00e4ufer","desc":"A unique Unit only available by completing Reisl\u00e4ufer Posts. Comparable to Longswordsmen with 20 Strength and 3 Movement. Receives Gold from defeated foes equal to twice their Strength. Upgrades into Rifleman and keeps the Mountaineer ability."}},{{"type":"Bias","name":"Hills","desc":""}}]}},"Tibet":{{"leader":"Ngawang Lobsang Gyatso","entries":[{{"type":"Ability","name":"Eightfold Path to Nirvana","desc":"Receive +2 Food, Production, Gold, Culture, Faith and Science in the Capital and the first city founded with the Dalai Lama upon the discovery of Drama and Poetry. Receive +1 to those yields for every Era you advance past the Classical Era."}},{{"type":"Unit","name":"Dalai Lama","desc":"Replaces Great Prophets generated and received by Tibet. No vision penalty and may spread Religion 1 extra time. May also start 6-turn Golden Ages (consuming the unit)."}},{{"type":"Improvement","name":"Monastery","desc":"Available at Calendar. Can only be built on Hills. Provides +2 Faith and +1 Culture; +1 additional Faith at Theology. +1 Culture for each adjacent Mountain. Receive +1 Culture at Acoustics and +1Gold, +1 Culture at Flight. May be built in Forests and Jungles without removing them."}},{{"type":"Bias","name":"None","desc":""}}]}},"Timurids":{{"leader":"Timur","entries":[{{"type":"Ability","name":"Ulugh Beg\u2019s Observatory","desc":"+5 City Strength and +15% Science, Gold and Culture in the original Capital. These bonuses are terminated if the original Capital is captured."}},{{"type":"Unit","name":"Timurid Rider -","desc":"Medieval Air Unit available at Chivalry. Replaces Horsemen. Requires 1 Horse; has 5 Range and 19 Combat Strength. Behaves as an Air Unit, and receives the appropriate promotions."}},{{"type":"Building","name":"Serai","desc":"Replaces Caravansary. Instead of providing +2 Gold at base, provides +1 Gold for every 3 Citizens in the City and otherwise retains the typical perks. Allows units to Airlift between Cities with Serais and increases Air Unit capacity in the City by 1. More costly to construct (67 hammers vs. 60) . (This bonus scales linearly. In practice, each Citizen provides +0.33 Gold.)"}},{{"type":"Bias","name":"Hill","desc":""}}]}},"Tonga":{{"leader":"Aho'eitu","entries":[{{"type":"Ability","name":"The Friendly Islands","desc":"Resting Influence with City-States is increased by 5. All Islands, Coast tiles, and Coast-adjacent land tiles within a 10-tile radius of your starting Settler are revealed immediately."}},{{"type":"Unit","name":"Matato\u2019a","desc":"Replaces the Archer (but does not require Archery to construct). 3 Movement (vs. 2) and +1 Sight; these bonuses are lost with upgrades. Cheaper to construct (20 hammers vs. 26). Obsolete at Machinery. Cannot upgrade from Ruins. Loses all ability on upgrade."}},{{"type":"Building","name":"Mala\u2019e","desc":"Replaces the Granary. Available at Sailing. Provides +1 Faith. Provides +2 Food and Culture if the city is adjacent to at least 3 Coastal tiles."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Tunisia":{{"leader":"Farhat Hached","entries":[{{"type":"Ability","name":"Khums","desc":"+100% Food, Production, and Gold from land-based Trade Routes. Melee units spend one less movement upon attacking a city (Units that cannot move after attacking still spend all movement) ."}},{{"type":"Building","name":"Ribat","desc":"Replaces Castle, yields +2 Gold, +2 Faith, and +4 Golden Age Points. 50% Cheaper to construct. Does not require Walls"}},{{"type":"Unit","name":"Xebec","desc":"Replaces Privateer . Costs 20% more production. May capture enemy ships upon their defeat (50% chance, captured units spawn with 50hp) . Is stronger (29 strength, vs. 27) and does not spend movement pillaging tiles."}},{{"type":"Bias","name":"Coastal","desc":""}}]}},"Turkey":{{"leader":"Ataturk","entries":[{{"type":"Ability","name":"Westernization","desc":"Amphitheaters, Opera Houses, Museums and Broadcast Towers are built +50% faster and each provide +1 Production, +1 Gold and +1 Science. +1 Production, +1 Gold and +1 Science from Great Works."}},{{"type":"Unit","name":"Kuva-yi Milliye","desc":"Replaces Great War Infantry. Weaker (47 Strength vs. 50) , but boasts 3 Movement (vs. 2) . Gains a +50% Combat Bonus when initiating combat, and may upgrade to Infantry for 0 Gold."}},{{"type":"Building","name":"Halkevleri","desc":"Replaces the Public School. In addition to typical perks, provides +3 Culture and is 20% cheaper to construct (160 hammers vs. 200) ."}},{{"type":"Bias","name":"None","desc":"United Arab Emirates (UAE) Sheikh Zayed"}}]}},"Ukraine":{{"leader":"Yaroslav I","entries":[{{"type":"Ability","name":"Chumatskyi Shliah","desc":"+1 Food from Maize, Wheat and Salt resources after researching The Wheel. City Connections provide +2 Gold to connected Cities. (The Capital also receives +2 Gold after the first Connection is made.)"}},{{"type":"Unit","name":"Tachanka","desc":"Replaces the Gatling Gun. Classified as a Mounted Ranged unit with 2 Range and 4 Movement and suffers the same Movement and Defensive Penalties as all Mounted Units. Upgrades to a normal Machine Gun."}},{{"type":"Building","name":"Knyaz Court","desc":"Replaces the Market. Compared to the Market, provides an additional point of Gold at base (2 vs. 1) . Each Knyaz Court in the empire generates 20 Gold whenever Ukraine consumes a Great Person."}},{{"type":"Bias","name":"Plains","desc":""}}]}},"Venice":{{"leader":"Enrico Dandolo","entries":[{{"type":"Ability","name":"Serenissima","desc":"Receive a free Trade Route and Cargo Ship in the Capital once Compass is researched. Cargo Ships may not be plundered. Capable of producing Settlers."}},{{"type":"Unit","name":"Great Galleass","desc":"Replaces the Galleass. 18 Combat Strength (vs. 16) and 20 Ranged Strength (vs. 17) , but costs 10% more to produce (73 hammers vs. 67) ."}},{{"type":"Unit","name":"The Merchant of Venice","desc":"Replaces Great Merchants. Extra embarked movement (6 vs. 4) and +100% bonus to influence and gold gained when conducting a trade-mission with a city-state. Has the \u2018buy city-state\u2019 ability, which gains you control over the city-state, including its territory and units (similar to Austria\u2019s Diplomatic Marriage)."}}]}},"Vietnam":{{"leader":"Hai Ba Trung","entries":[{{"type":"Ability","name":"Tam","desc":"Giai\u00e0o +2 Food, +1 Production from Marsh. +1 Faith from Jungle. Units receive double Movement through Marsh and Jungle tiles and receive a Combat Bonus in Marshes (+30%) and Jungles (+15%)."}},{{"type":"Unit","name":"Viet Cong","desc":"Replaces Infantry. Moves further (3 Movement vs. 2) . When initiating combat, performs a bonus Ranged Attack prior to Melee combat. Gains a +25% Combat Bonus against Gunpowder units. Cheaper to construct (233 hammers vs. 280) . Loses abilities on upgrade."}},{{"type":"Building","name":"Vo Khi","desc":"Replaces the Armory, available at Metal Casting (instead of Steel). In addition to the typical perks, provides +1 Food, +1 Culture and +1 Production from Marsh tiles and +1 Culture from Jungle tiles. Doesn\u2019t require Barracks to construct."}},{{"type":"Bias","name":"Jungle, Wetlands (Marsh and Floodplains), Avoid desert","desc":""}}]}},"Wales":{{"leader":"Owain Glyndwr","entries":[{{"type":"Ability","name":"Hafod a Hendref","desc":"+1 Food and +1 Gold from Sheep at Animal Husbandry. Units receive a +10% Combat Bonus fighting in Hills."}},{{"type":"Unit","name":"Saethwyr","desc":"Replaces Crossbowmen. 15 Combat Strength (vs. 13)."}},{{"type":"Improvement","name":"Caer","desc":"Available at Chivalry. +3 Gold, +1 Culture, and +1 Production. Units gain a +25% Combat Bonus defending this tile. +1 Culture at Acoustics. Receives bonus yields from certain policies: +1 Culture from Cultural Exchange, +1 Science from Free Thought, +1 Food from completing the Commerce tree. May only be built on Hills without resources."}},{{"type":"Bias","name":"Hill","desc":""}}]}},"Yugoslavia":{{"leader":"Aleksandar I","entries":[{{"type":"Ability","name":"Non-Aligned Doctrine -","desc":"Pick a free Ideology Tenet when adopting or switching Ideologies and receive bonuses corresponding to both rival Ideologies. If not Freedom, Specialists provide +1 Gold. If not Autocracy, receive +15% Production towards land Units. If not Order, receive +2 Production and Science in all Cities."}},{{"type":"Unit","name":"M-84","desc":"Replaces Tanks. Ignores Zone of Control movement penalties. Uh oh. Loses ability on upgrade."}},{{"type":"Building","name":"Spomenik","desc":"Replaces Museums. In addition to the typical perks, provides +20% Science in the City, but costs more to construct (230 hammers vs. 200) ."}},{{"type":"Bias","name":"None","desc":""}}]}},"Zimbabwe":{{"leader":"Nyatsimba Mutota","entries":[{{"type":"Ability","name":"Great Zimbabwe","desc":"+25% Production towards buildings and military Units in the Capital. (Guilds and National and World Wonders do not count as buildings)."}},{{"type":"Unit","name":"Shona Warrior","desc":"Replaces the Warrior. 9 Strength (vs. 8) and is capable of building Quarries and Farms."}},{{"type":"Building","name":"Stone Mason","desc":"Replaces Stoneworks. Does not provide Happiness, but provides +2 Culture at Masonry, and otherwise retains the typical perks. Doesn\u2019t require any nearby resources to construct."}},{{"type":"Bias","name":"Hill","desc":""}}]}},"Zulu":{{"leader":"Shaka","entries":[{{"type":"Ability","name":"Iklwa","desc":"Melee Units pay 50% less Gold maintenance. All Units require 25% less experience to promote."}},{{"type":"Building","name":"Ikanda","desc":"Replaces Barracks. Grants the Buffalo Horns promotion to all pre-Gunpowder Melee Units trained in the City and allows these Units to earn the Buffalo Chest and Loins promotions. Horns: +1 Movement, +25% Flanking Bonus, and +10% defensive Strength against Ranged attacks Chest: +10% Bonus in Open Terrain, an additional +25% Flanking Bonus, and an additional +10% defensive Strength against Ranged attacks (requires Horns) Loins: +10% Combat Strength, an additional +25% Flanking Bonus, and an additional +10% defensive Strength against Ranged attacks (requires Chest)"}},{{"type":"Unit","name":"Impi","desc":"Replaces Pikemen. When initiating combat, performs a bonus Ranged Attack prior to Melee combat. Gains +25% Combat Strength against Gunpowder units. Loses abilities on upgrade."}},{{"type":"Bias","name":"Avoid Jungle","desc":""}}]}},"Brazil":{{"leader":"Pedro II","entries":[{{"type":"Ability","name":"Carnival!","desc":"Output 100% more Tourism and generate Great Artists, Writers, and Musicians +100% (from 50%) faster during Golden Ages. All units earn 100% of foe\u2019s Strength as points towards a Golden Age from kills."}},{{"type":"Unit","name":"Pracinha","desc":"Replaces Infantry. 80 Strength (from 70, vs. 70) . Gains a +20% Combat Bonus outside of friendly territory. May move after attacking. Keeps abilities on upgrade."}},{{"type":"Improvement","name":"Brazilwood Camp","desc":"Available at Bronze Working (instead of Machinery). May be constructed on Jungle tiles. Provides +1 Production. Gains +2 Gold at Machinery and +2 Culture at Acoustics and improves any resources on the tile."}},{{"type":"Bias","name":"Jungle","desc":""}}]}},"Chile":{{"leader":"Bernardo O'Higgens","entries":[{{"type":"Ability","name":"By Reason or By Force","desc":"Claim all surrounding neutral tiles upon the completion of Fishing Boat or Drydock improvements. Receive a free Great Admiral at Compass. Friendly Melee & Gunpowder Units inflict a -25% Combat Penalty to adjacent enemy Naval Units; Melee Naval Units inflict this penalty to adjacent enemy Land Units. (Embarked Units do not apply these penalties.)"}},{{"type":"Building","name":"Cooperative","desc":"Replaces the Factory. In addition to the typical perks, provides +2 Happiness and houses an additional Engineer Specialist (3 vs. 2). Does not require Gold maintenance. Ideologies are unlocked for Chile after the completion of only 1 Cooperative (compared to 3 Factories)."}},{{"type":"Unit","name":"Cardoen","desc":"Replaces the Helicopter Gunship. Stronger (85 Strength vs. 70) . In addition to the typical bonuses, the Cardoen is capable of attacking and capturing Cities, has 2 additional movement, and receives a +100% Combat Bonus defending against ranged attacks."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Sumeria":{{"leader":"Gilgamesh","entries":[{{"type":"Ability","name":"Cradle of Civilization","desc":"+2 Culture from every City upon the discovery of Drama and Poetry."}},{{"type":"Building","name":"Ziggurat","desc":"Replaces Temple. In addition to the Faith yield, provides +10% Science. Only costs 1 Gold to maintain (vs. 2) ."}},{{"type":"Unit","name":"Phalanx","desc":"Replaces Spearman. Available at Mining. +50% more effective at flanking (+15% per adjacent ally vs. +10%) and cheaper to construct (30 hammers vs. 37) . Keeps ability on upgrade."}},{{"type":"Bias","name":"River","desc":""}}]}},"UAE":{{"leader":"Sheikh Zayed","entries":[{{"type":"Ability","name":"Yallah Habibi","desc":"Upon completing a World Wonder, receive 100 Gold and start a We Love The King Day in the City where it is built. All Units gain +3 Gold per turn and +1 Experience each turn on tiles with a Trade Route. (This King Day lasts for 15 turns and will stack with existing King Days.) (Gold and XP bonus is provided on each and every tile connecting the Trade Route.)"}},{{"type":"Building","name":"Burj","desc":"Replaces the Hotel. Available at Radio (instead of Refrigeration). Compared to the Hotel, the Burj provides +2 Gold and Culture to Great Person Improvements and Desert tiles in the City (with the exception of Floodplains)."}},{{"type":"Unit","name":"Qasimi Raider","desc":"Replaces the Privateer. Earns twice as much Gold from attacking Cities. Yields 2 movement points and 15 XP when plundering a trade-route. 8 Movement (vs. 6) , and 20% cheaper to construct (80 hammers vs. 100) . Promotions are kept on upgrade."}},{{"type":"Bias","name":"Coast","desc":""}}]}},"Mexico":{{"leader":"Benito Juarez","entries":[{{"type":"Bias","name":"None","desc":"Mexico - Benito Juarez"}},{{"type":"Ability","name":"Encomienda System","desc":"Receive a free Worker near the Capital at Pottery. City-States that are afraid of Mexico gain +6 Influence per turn when Mexico can demand tribute from them, but does not. Reveal all City-States within a 10-tile radius of your starting location after the end of the first turn."}},{{"type":"Unit","name":"Ranchero","desc":"Replaces Settler. Does not halt Growth to produce, but does not protect the City from starvation. Food does not contribute to its Production."}},{{"type":"Building","name":"Hacienda","desc":"Replaces Windmill. +1 Production from Luxury resources, +1 Gold from Bonus resources (e.g. Wheat, Fish), and +1 Food from Strategic Resources (e.g. Horses, Iron). Does not require Gold maintenance."}}]}}}};
const CIVPEDIA_ICONS = {{"Ability":"⚡","Building":"🏛️","Unit":"⚔️","Improvement":"🔧","Wonder":"🌟","Policy":"📜","Unique":"✨","Bias":"🗺️"}};
const TYPE_COLORS = {{"Ability":"#f97316","Building":"#3b82f6","Unit":"#ef4444","Improvement":"#22c55e","Wonder":"#eab308","Policy":"#a855f7","Unique":"#06b6d4","Bias":"#475569"}};

// Server stats for this civ
const COASTAL_CIVS = new Set(["Australia","Brunei","Carthage","Chile","Denmark","England","Indonesia","Japan","Kilwa","Korea","Netherlands","New Zealand","Norway","Oman","Philippines","Phoenicia","Polynesia","Portugal","Spain","Tonga","Tunisia","UAE","Venice"]);

const TIER_ORDER = ["normal","bronze","silver","gold","diamond"];
const TIER_LABELS = {{
  normal:  {{icon:"",   label:"Normal",  color:"#475569", border:"#1e2130"}},
  bronze:  {{icon:"🥉", label:"Bronze",  color:"#c8762e", border:"#7c4a1e"}},
  silver:  {{icon:"🥈", label:"Silver",  color:"#8fa3b8", border:"#4a5568"}},
  gold:    {{icon:"🥇", label:"Gold",    color:"#d4a500", border:"#8a6a00"}},
  diamond: {{icon:"💎", label:"Diamond", color:"#22d3ee", border:"#0a4a6e"}},
}};

function getCivTier(civName) {{
  return CARD_TIERS[civName] || "normal";
}}

function winsToTier(w) {{
  if (w >= 4) return "diamond";
  if (w >= 3) return "gold";
  if (w >= 2) return "silver";
  if (w >= 1) return "bronze";
  return "normal";
}}

// Tier-specific metallic shine override
function getTierShine(tier, dx, dy) {{
  const angle = (115 + dy * 5).toFixed(1);
  const shift1 = (42 + dx * 28).toFixed(1);
  const shift2 = (66 + dx * 22).toFixed(1);
  if (tier === "normal") {{
    return [
      `linear-gradient(${{angle}}deg, transparent ${{(+shift1-22).toFixed(1)}}%, rgba(255,255,255,0.01) ${{(+shift1-4).toFixed(1)}}%, rgba(255,255,255,0.06) ${{shift1}}%, rgba(255,255,255,0.01) ${{(+shift1+4).toFixed(1)}}%, transparent ${{(+shift1+22).toFixed(1)}}%)`,
      `linear-gradient(${{angle}}deg, transparent ${{(+shift2-15).toFixed(1)}}%, rgba(255,255,255,0.01) ${{(+shift2-3).toFixed(1)}}%, rgba(255,255,255,0.03) ${{shift2}}%, rgba(255,255,255,0.01) ${{(+shift2+3).toFixed(1)}}%, transparent ${{(+shift2+15).toFixed(1)}}%)`,
    ].join(",");
  }}
  if (tier === "bronze") {{
    return [
      `linear-gradient(${{angle}}deg, transparent ${{(+shift1-20).toFixed(1)}}%, rgba(200,118,46,0.06) ${{(+shift1-4).toFixed(1)}}%, rgba(200,118,46,0.22) ${{shift1}}%, rgba(200,118,46,0.06) ${{(+shift1+4).toFixed(1)}}%, transparent ${{(+shift1+20).toFixed(1)}}%)`,
      `linear-gradient(${{angle}}deg, transparent ${{(+shift2-12).toFixed(1)}}%, rgba(255,180,80,0.04) ${{(+shift2-3).toFixed(1)}}%, rgba(255,180,80,0.12) ${{shift2}}%, rgba(255,180,80,0.04) ${{(+shift2+3).toFixed(1)}}%, transparent ${{(+shift2+12).toFixed(1)}}%)`,
    ].join(",");
  }}
  if (tier === "silver") {{
    return [
      `linear-gradient(${{angle}}deg, transparent ${{(+shift1-20).toFixed(1)}}%, rgba(180,200,220,0.05) ${{(+shift1-4).toFixed(1)}}%, rgba(200,220,240,0.22) ${{shift1}}%, rgba(180,200,220,0.05) ${{(+shift1+4).toFixed(1)}}%, transparent ${{(+shift1+20).toFixed(1)}}%)`,
      `linear-gradient(${{angle}}deg, transparent ${{(+shift2-12).toFixed(1)}}%, rgba(220,235,250,0.04) ${{(+shift2-3).toFixed(1)}}%, rgba(220,235,250,0.14) ${{shift2}}%, rgba(220,235,250,0.04) ${{(+shift2+3).toFixed(1)}}%, transparent ${{(+shift2+12).toFixed(1)}}%)`,
    ].join(",");
  }}
  if (tier === "gold") {{
    const shift3 = (30 + dx * 30).toFixed(1);
    return [
      `linear-gradient(${{angle}}deg, transparent ${{(+shift1-18).toFixed(1)}}%, rgba(212,165,0,0.08) ${{(+shift1-4).toFixed(1)}}%, rgba(255,210,0,0.30) ${{shift1}}%, rgba(212,165,0,0.08) ${{(+shift1+4).toFixed(1)}}%, transparent ${{(+shift1+18).toFixed(1)}}%)`,
      `linear-gradient(${{angle}}deg, transparent ${{(+shift2-12).toFixed(1)}}%, rgba(255,200,0,0.05) ${{(+shift2-3).toFixed(1)}}%, rgba(255,200,0,0.18) ${{shift2}}%, rgba(255,200,0,0.05) ${{(+shift2+3).toFixed(1)}}%, transparent ${{(+shift2+12).toFixed(1)}}%)`,
      `linear-gradient(${{(+angle+60).toFixed(1)}}deg, transparent ${{(+shift3-15).toFixed(1)}}%, rgba(255,240,100,0.06) ${{shift3}}%, transparent ${{(+shift3+15).toFixed(1)}}%)`,
    ].join(",");
  }}
  if (tier === "diamond") {{
    const hue1 = ((dx + 1) * 90).toFixed(0); // 0-180
    const hue2 = ((dy + 1) * 60 + 180).toFixed(0); // 180-300
    return [
      `linear-gradient(${{angle}}deg, transparent ${{(+shift1-18).toFixed(1)}}%, hsla(${{hue1}},90%,75%,0.05) ${{(+shift1-4).toFixed(1)}}%, hsla(${{hue1}},90%,80%,0.28) ${{shift1}}%, hsla(${{hue1}},90%,75%,0.05) ${{(+shift1+4).toFixed(1)}}%, transparent ${{(+shift1+18).toFixed(1)}}%)`,
      `linear-gradient(${{(+angle+40).toFixed(1)}}deg, transparent ${{(+shift2-12).toFixed(1)}}%, hsla(${{hue2}},85%,70%,0.04) ${{(+shift2-3).toFixed(1)}}%, hsla(${{hue2}},85%,75%,0.18) ${{shift2}}%, hsla(${{hue2}},85%,70%,0.04) ${{(+shift2+3).toFixed(1)}}%, transparent ${{(+shift2+12).toFixed(1)}}%)`,
    ].join(",");
  }}
  return "";
}}

function getCivStats(civName) {{
  const wins = HISTORY.filter(g => g.players.some(p => p.civ === civName && p.finish === 1)).length;
  const played = HISTORY.filter(g => g.players.some(p => p.civ === civName)).length;
  const wr = played > 0 ? Math.round(wins/played*100) : null;
  return {{wins, played, wr}};
}}

function buildCivDetail(name) {{
  const civ = CIVPEDIA[name];
  if (!civ) return "";
  const stats = getCivStats(name);
  const isCoastal = COASTAL_CIVS.has(name);
  const mapType = isCoastal ? "⛵ Coastal" : "🏕️ Land";
  const bias = civ.entries.find(e => e.type === "Bias");
  const biasHtml = bias && bias.name ? `<span class="civ-bias">🗺️ ${{bias.name}}</span>` : "";

  const statsHtml = `<div class="civ-detail-stats">
    <div class="civ-detail-stat"><div class="civ-detail-stat-val" style="color:#06b6d4;font-size:10px">${{mapType}}</div><div class="civ-detail-stat-label">MAP</div></div>
    ${{stats.played > 0 ? `
    <div class="civ-detail-stat"><div class="civ-detail-stat-val">${{stats.played}}</div><div class="civ-detail-stat-label">PLAYED</div></div>
    <div class="civ-detail-stat"><div class="civ-detail-stat-val">${{stats.wins}}</div><div class="civ-detail-stat-label">WINS</div></div>
    <div class="civ-detail-stat"><div class="civ-detail-stat-val" style="color:${{stats.wr>=50?"#22c55e":"#ef4444"}}">${{stats.wr}}%</div><div class="civ-detail-stat-label">WIN RATE</div></div>
    ` : `<div class="civ-detail-stat" style="opacity:0.4"><div class="civ-detail-stat-val">—</div><div class="civ-detail-stat-label">UNPLAYED</div></div>`}}
  </div>`;

  const sections = civ.entries.filter(e => e.type !== "Bias").map(e => {{
    const color = TYPE_COLORS[e.type] || "#94a3b8";
    const icon = CIVPEDIA_ICONS[e.type] || "•";
    const desc = (e.desc||"")
      .replace(/\(vs\. [\d\.]+\)/g, m => `<span>${{m}}</span>`)
      .replace(/\(from [\d\.]+[^)]*\)/g, m => `<span>${{m}}</span>`)
      .replace(/\(unchanged\)/g, m => `<span>${{m}}</span>`)
      .replace(/\([^)]*previously[^)]*\)/g, m => `<span>${{m}}</span>`);
    return `<div class="civ-section" style="border-left:3px solid ${{color}}">
      <div class="civ-section-type" style="color:${{color}}">${{icon}} ${{e.type.toUpperCase()}}</div>
      ${{e.name ? `<div class="civ-section-name">${{e.name}}</div>` : ""}}
      ${{desc ? `<div class="civ-section-desc">${{desc}}</div>` : ""}}
    </div>`;
  }}).join("");

  // Card tier progress section
  let upgradeHtml = "";
  if (LOGGED_IN_ID) {{
    const tier = getCivTier(name);
    const tierInfo = TIER_LABELS[tier];
    const wins = CIV_WINS[name] || 0;
    const thresholds = [{{tier:"bronze",at:1}},{{tier:"silver",at:2}},{{tier:"gold",at:3}},{{tier:"diamond",at:4}}];
    const next = thresholds.find(t => wins < t.at);
    const nextInfo = next ? TIER_LABELS[next.tier] : null;
    const winsNeeded = next ? next.at - wins : 0;
    // Progress bar to next tier
    const prevAt = next ? (thresholds[thresholds.indexOf(next)-1]?.at || 0) : 6;
    const progress = next ? Math.round((wins - prevAt) / (next.at - prevAt) * 100) : 100;
    upgradeHtml = `<div style="margin-top:14px;padding:12px;background:#080a0f;border:1px solid #1e2130;border-radius:8px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div>
          <div style="font-size:9px;color:#475569;letter-spacing:2px;margin-bottom:3px">CARD TIER</div>
          <div style="font-size:13px;font-weight:700;color:${{tierInfo.color}}">${{tierInfo.icon||"🃏"}} ${{tierInfo.label}}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:9px;color:#475569;letter-spacing:2px;margin-bottom:3px">WINS WITH CIV</div>
          <div style="font-size:16px;font-weight:700;color:#e2e8f0">${{wins}}</div>
        </div>
      </div>
      ${{nextInfo ? `
        <div style="font-size:9px;color:#475569;margin-bottom:6px">Next: ${{nextInfo.icon}} ${{nextInfo.label}} at ${{next.at}} wins &nbsp;·&nbsp; ${{winsNeeded}} to go</div>
        <div style="height:4px;background:#1e2130;border-radius:2px;overflow:hidden">
          <div style="height:100%;width:${{progress}}%;background:${{nextInfo.color}};border-radius:2px;transition:width 0.6s ease"></div>
        </div>` : `<div style="font-size:10px;color:#f97316;text-align:center">✦ Max tier reached — Diamond ✦</div>`}}
    </div>`;
  }}

  return `
    <div class="civ-detail-header">
      <div>
        <div class="civ-detail-title">${{name}}</div>
        <div class="civ-detail-leader">Leader: ${{civ.leader}}</div>
        ${{biasHtml}}
      </div>
      <span style="font-size:10px;color:#475569;cursor:pointer;padding:3px 8px;border:1px solid #1e2130;border-radius:6px;flex-shrink:0" onclick="collapseCiv(event)">✕</span>
    </div>
    ${{statsHtml}}
    ${{sections}}
    ${{upgradeHtml}}`;
}}

let expandedCiv = null;

// ── 3D tilt + anime diagonal shine ──────────────────────────────────────────
function addTilt(tile) {{
  const shine = tile.querySelector(".civ-tile-shine");

  tile.addEventListener("mousemove", (e) => {{
    if (tile.classList.contains("expanded")) return;
    const rect = tile.getBoundingClientRect();
    const dx = (e.clientX - rect.left - rect.width  / 2) / (rect.width  / 2); // -1 to 1
    const dy = (e.clientY - rect.top  - rect.height / 2) / (rect.height / 2);
    const rotX = -dy * 12;
    const rotY =  dx * 12;
    tile.style.transform = `perspective(700px) rotateX(${{rotX}}deg) rotateY(${{rotY}}deg) scale(1.04)`;

    // Anime shine: shift the diagonal band position based on tilt
    // dx=-1 (left tilt) → band sweeps left, dx=1 → band sweeps right
    if (shine) {{
      // Shift the two shine lines sideways with tilt — creates the illusion of
      // flat lines sliding across the surface as the card angle changes
      const shift1 = (42 + dx * 25).toFixed(1); // primary band centre (%)
      const shift2 = (65 + dx * 20).toFixed(1); // secondary band centre (%)
      const angle  = (115 + dy * 5).toFixed(1); // slight angle change with vertical tilt
      shine.style.background = [
        `linear-gradient(${{angle}}deg,
          transparent ${{(+shift1-22).toFixed(1)}}%,
          rgba(255,255,255,0.01) ${{(+shift1-4).toFixed(1)}}%,
          rgba(255,255,255,0.06) ${{shift1}}%,
          rgba(255,255,255,0.01) ${{(+shift1+4).toFixed(1)}}%,
          transparent ${{(+shift1+22).toFixed(1)}}%)`,
        `linear-gradient(${{angle}}deg,
          transparent ${{(+shift2-15).toFixed(1)}}%,
          rgba(255,255,255,0.01) ${{(+shift2-3).toFixed(1)}}%,
          rgba(255,255,255,0.03) ${{shift2}}%,
          rgba(255,255,255,0.01) ${{(+shift2+3).toFixed(1)}}%,
          transparent ${{(+shift2+15).toFixed(1)}}%)`,
      ].join(",");
    }}
  }});

  tile.addEventListener("mouseleave", () => {{
    tile.style.transition = "transform 0.5s cubic-bezier(0.23,1,0.32,1), border-color 0.2s, box-shadow 0.2s";
    tile.style.transform = "perspective(700px) rotateX(0deg) rotateY(0deg) scale(1)";
    if (shine) shine.style.background = "";
    setTimeout(() => {{ tile.style.transition = "transform 0.08s ease, border-color 0.2s, box-shadow 0.2s"; }}, 500);
  }});

  tile.addEventListener("mouseenter", () => {{
    tile.style.transition = "transform 0.08s ease, border-color 0.2s, box-shadow 0.2s";
  }});
}}

function buildCivGrid(filter) {{
  const grid = document.getElementById("civGrid");
  if (!grid) return;
  grid.innerHTML = "";
  const names = Object.keys(CIVPEDIA).filter(n =>
    !filter || n.toLowerCase().includes(filter.toLowerCase())
  ).sort();

  names.forEach(name => {{
    const civ = CIVPEDIA[name];
    const isCoastal = COASTAL_CIVS.has(name);
    const stats = getCivStats(name);
    const ability = civ.entries.find(e => e.type === "Ability");
    const units = civ.entries.filter(e => e.type === "Unit" || e.type === "Great Person");
    const buildings = civ.entries.filter(e => e.type === "Building");
    const improvements = civ.entries.filter(e => e.type === "Improvement");

    const tags = [
      ...units.map(u => `<span class="civ-tile-tag" style="background:#200c0c;color:#ef4444">⚔️ ${{u.name}}</span>`),
      ...buildings.map(b => `<span class="civ-tile-tag" style="background:#0c1a20;color:#3b82f6">🏛️ ${{b.name}}</span>`),
      ...improvements.map(i => `<span class="civ-tile-tag" style="background:#0c2010;color:#22c55e">🔧 ${{i.name}}</span>`),
    ].join("");

    const playedBadge = stats.played > 0
      ? `<span class="civ-tile-played">${{stats.wins}}W / ${{stats.played}}G</span>`
      : "";

    const tier = LOGGED_IN_ID ? getCivTier(name) : "normal";
    const tierInfo = TIER_LABELS[tier];
    const tile = document.createElement("div");
    tile.className = "civ-tile" + (tier !== "normal" ? " tier-"+tier : "");
    tile.id = "civ-tile-" + name.replace(/\s+/g,'_');
    tile.dataset.tier = tier;
    // Full info for card view
    const allEntries = civ.entries.filter(e => e.type !== "Bias");
    const entryRows = allEntries.map(e => {{
      const color = TYPE_COLORS[e.type] || "#475569";
      const icon  = CIVPEDIA_ICONS[e.type] || "•";
      return `<div style="padding-left:7px;border-left:2px solid ${{color}}55">
        <div style="font-size:8px;color:${{color}};letter-spacing:1px;margin-bottom:1px">${{icon}} ${{e.type.toUpperCase()}}</div>
        <div style="font-size:9px;font-weight:700;color:#e2e8f0;margin-bottom:2px;line-height:1.2">${{e.name}}</div>
        ${{e.desc ? `<div style="font-size:8px;color:#64748b;line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${{e.desc}}</div>` : ""}}
      </div>`;
    }}).join("");

    tile.innerHTML = `
      <div class="civ-tile-shine"></div>
      ${{tier !== "normal" ? `<div class="tier-badge">${{tierInfo.icon}}</div>` : ""}}

      <!-- Card view content — hidden when expanded -->
      <div class="civ-card-content" style="display:flex;flex-direction:column;flex:1;min-height:0">
        <div class="civ-tile-top">
          <div class="civ-tile-map">${{isCoastal?"⛵":"🏕️"}}</div>
          ${{playedBadge}}
        </div>
        <div class="civ-tile-name">${{name}}</div>
        <div class="civ-tile-leader">${{civ.leader}}</div>
        <div class="civ-tile-divider"></div>
        <div style="flex:1;display:flex;flex-direction:column;gap:5px">
          ${{entryRows}}
        </div>
        ${{stats.played > 0 ? `
          <div style="margin-top:auto;padding-top:6px;border-top:1px solid #1e2130;display:flex;gap:8px;font-size:7px;color:#475569;flex-shrink:0">
            <span style="color:#22c55e;font-weight:700">${{stats.wins}}W</span>
            <span>${{stats.played}}G</span>
            <span style="color:${{stats.wr>=50?'#22c55e':'#ef4444'}}">${{stats.wr}}%WR</span>
          </div>` : ""}}
      </div>

      <!-- Expanded detail — hidden when collapsed -->
      <div class="civ-detail">${{buildCivDetail(name)}}</div>`;

    tile.onclick = (ev) => {{
      if (ev.target.closest(".civ-detail")) return;
      toggleCiv(name);
    }};

    addTilt(tile);
    grid.appendChild(tile);
    if (expandedCiv === name) tile.classList.add("expanded");
  }});
}}

function toggleCiv(name) {{
  expandedCiv = expandedCiv === name ? null : name;
  const searchVal = document.getElementById("civSearch")?.value || "";
  buildCivGrid(searchVal);
  if (expandedCiv) {{
    setTimeout(() => {{
      const el = document.getElementById("civ-tile-" + expandedCiv.replace(/\s+/g,'_'));
      if (el) el.scrollIntoView({{behavior:"smooth", block:"nearest"}});
    }}, 50);
  }}
}}

function collapseCiv(event) {{
  event.stopPropagation();
  expandedCiv = null;
  const searchVal = document.getElementById("civSearch")?.value || "";
  buildCivGrid(searchVal);
}}

function onCivSearch(val) {{
  buildCivGrid(val);
}}

function showAllSuggestions() {{}}

// Close suggestions when clicking outside
document.addEventListener("click", e => {{
  if (!e.target.closest("#civSearch") && !e.target.closest("#civSuggestions")) {{
    const box = document.getElementById("civSuggestions");
    if (box) box.style.display = "none";
  }}
}});

// ── Host Game Page ────────────────────────────────────────────────────────────
function buildHostPage() {{
  const el = document.getElementById("hostContent");
  if (!LOGGED_IN_ID) {{
    el.innerHTML = '<p class="no-games">LOG IN TO ACCESS GAME CONTROLS</p>';
    return;
  }}
  const myName = DISPLAY_NAME || LOGGED_IN_NAME;
  const myGame = LIVE_GAMES.find(g => g.players.some(p => p.id === LOGGED_IN_ID));
  const amHost = myGame && myGame.host_id === LOGGED_IN_ID;
  const isLobby = myGame && myGame.status === "lobby";
  const inDraft = myGame && !isLobby && myGame.players.some(p => p.id === LOGGED_IN_ID && !p.chosen);
  const allPicked = myGame && !isLobby && myGame.players.every(p => p.chosen);
  const myPlayerData = myGame && myGame.players.find(p => p.id === LOGGED_IN_ID);
  const iHavePicked = myPlayerData && myPlayerData.chosen;

  el.innerHTML = "";

  // ── No game yet — only show create lobby (join is in Live Games tab) ─────────
  if (!myGame) {{
    const sec = document.createElement("div"); sec.className = "host-section";
    sec.innerHTML = `
      <div class="host-section-title">HOST A GAME</div>
      <label class="form-label">DIFFICULTY</label>
      <select class="form-select" id="hDiff" style="max-width:220px">
        <option value="Prince">Prince</option>
        <option value="King">King</option>
      </select>
      <div style="margin-top:4px">
        <button class="btn btn-primary" onclick="hostCreateLobby()">🏛️ Open Lobby</button>
      </div>
      <p style="font-size:10px;color:#475569;margin-top:12px">To join someone else's lobby, go to the <strong style="color:#94a3b8">Live Games</strong> tab.</p>`;
    el.appendChild(sec);
    return;
  }}

  // ── In a lobby ─────────────────────────────────────────────────────────────
  if (isLobby) {{
    const sec = document.createElement("div"); sec.className = "host-section";
    const playerList = myGame.players.map((p,i) => `
      <div class="player-card">
        <div class="player-card-name">${{p.name}}${{p.id===LOGGED_IN_ID?" (you)":""}}</div>
        ${{i===0?'<span style="font-size:10px;color:#f97316">HOST</span>':''}}
      </div>`).join("");
    sec.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <div class="host-section-title" style="margin:0">LOBBY · ${{myGame.difficulty}}</div>
        <span style="font-size:10px;color:#475569">${{myGame.players.length}} players</span>
      </div>
      ${{playerList}}
      <div style="margin-top:16px;padding-top:14px;border-top:1px solid #1e2130;display:flex;gap:10px;flex-wrap:wrap">
        ${{amHost ? `
          <div style="flex:1;min-width:200px">
            <label class="form-label">MAP TYPE</label>
            <select class="form-select" id="hMapType">
              <option value="any">🌐 Any</option>
              <option value="land">🏕️ Land</option>
              <option value="coastal">⛵ Coastal</option>
              <option value="skip">Skip draft</option>
            </select>
            <button class="btn btn-primary" onclick="hostStartGame('${{myGame.host_id}}')">▶ Start Game</button>
            <button class="btn btn-ghost" style="margin-left:8px" onclick="cancelGame('${{myGame.host_id}}','lobby')">✕ Cancel</button>
          </div>` : `<button class="btn btn-ghost" onclick="leaveLobby('${{myGame.host_id}}')">Leave Lobby</button>`}}
      </div>`;
    el.appendChild(sec);
    return;
  }}

  // ── In draft phase ─────────────────────────────────────────────────────────
  if (!allPicked) {{
    const sec = document.createElement("div"); sec.className = "host-section";
    const mapLabel = {{"land":"🏕️ Land","coastal":"⛵ Coastal","any":"🌐 Any","skip":"No draft"}}[myGame.map_type]||"";
    sec.innerHTML = `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="host-section-title" style="margin:0">DRAFT PHASE · ${{mapLabel}}</div>
      <span style="font-size:10px;color:#475569">${{myGame.players.filter(p=>p.chosen).length}}/${{myGame.players.length}} picked</span>
    </div>`;

    myGame.players.forEach(p => {{
      const isMe = p.id === LOGGED_IN_ID;
      const card = document.createElement("div");
      card.style.marginBottom = "14px";
      card.innerHTML = `<div class="player-card" style="margin-bottom:8px">
        <div class="player-card-name">${{p.name}}${{isMe?" (you)":""}}</div>
        ${{p.chosen ? `<span class="badge" style="color:#22c55e;background:#0c2010;border:1px solid #22c55e44">${{p.chosen}}</span>` : '<span class="badge">picking...</span>'}}
      </div>`;
      if (p.pool && p.pool.length) {{
        const grid = document.createElement("div"); grid.className = "civ-grid";
        p.pool.forEach(c => {{
          const btn = document.createElement("button");
          btn.className = "civ-option" + (c === p.chosen ? " picked" : "");
          btn.textContent = c;
          btn.addEventListener("mouseenter", () => showCivTooltip(c, btn));
          btn.addEventListener("mouseleave", hideCivTooltip);
          if (isMe && !iHavePicked) {{
            btn.onclick = () => pickCiv(myGame.host_id, c);
          }} else {{
            btn.disabled = true; btn.style.opacity = "0.5"; btn.style.cursor = "default";
          }}
          grid.appendChild(btn);
        }});
        card.appendChild(grid);
      }}
      sec.appendChild(card);
    }});

    const actions = document.createElement("div");
    actions.style.cssText = "margin-top:14px;padding-top:12px;border-top:1px solid #1e2130";
    actions.innerHTML = `<button class="btn btn-ghost" onclick="cancelGame('${{myGame.host_id}}','game')">✕ Cancel Game</button>`;
    sec.appendChild(actions);
    el.appendChild(sec);
    return;
  }}

  // ── All picked — game in progress ──────────────────────────────────────────
  const sec = document.createElement("div"); sec.className = "host-section";
  const playerList2 = myGame.players.map(p => `
    <div class="player-card">
      <div class="player-card-name">${{p.name}}${{p.id===LOGGED_IN_ID?" (you)":""}}</div>
      <span class="badge" style="color:#22c55e;background:#0c2010;border:1px solid #22c55e44">${{p.chosen}}</span>
    </div>`).join("");

  let reportHtml = "";
  if (amHost) {{
    const medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣"];
    const opts = myGame.players.map(p => `<option value="${{p.id}}">${{p.name}}</option>`).join("");
    const rows = myGame.players.map((_,i) => `
      <div class="finish-row">
        <span class="finish-medal">${{medals[i]}}</span>
        <select class="form-select" id="hFinish-${{i}}" style="margin-bottom:0">${{opts}}</select>
      </div>`).join("");
    // Set default order
    reportHtml = `
      <div style="margin-top:16px;padding-top:14px;border-top:1px solid #1e2130">
        <div class="host-section-title">REPORT RESULTS</div>
        ${{rows}}
        <label class="form-label" style="margin-top:8px">VICTORY TYPE</label>
        <select class="form-select" id="hVictory" style="max-width:220px">
          <option value="">— None —</option>
          <option value="Domination">⚔️ Domination</option>
          <option value="Science">🚀 Science</option>
          <option value="Culture">🎭 Culture</option>
          <option value="Diplomatic">🕊️ Diplomatic</option>
        </select>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button class="btn btn-primary" onclick="hostSubmitResults('${{myGame.host_id}}',${{myGame.players.length}})">✅ Submit Results</button>
          <button class="btn btn-ghost" onclick="cancelGame('${{myGame.host_id}}','game')">✕ Cancel Game</button>
        </div>
      </div>`;
  }} else {{
    reportHtml = `<div style="margin-top:14px;padding-top:12px;border-top:1px solid #1e2130">
      <p style="font-size:11px;color:#475569;margin-bottom:10px">Waiting for host to report results...</p>
      <button class="btn btn-ghost" onclick="cancelGame('${{myGame.host_id}}','game')">✕ Cancel Game</button>
    </div>`;
  }}

  sec.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="host-section-title" style="margin:0">GAME IN PROGRESS</div>
      <span style="font-size:10px;color:#475569">${{myGame.difficulty}}</span>
    </div>
    ${{playerList2}}
    ${{reportHtml}}`;
  el.appendChild(sec);

  // Set default finishing order
  if (amHost) {{
    myGame.players.forEach((p, i) => {{
      const sel = document.getElementById("hFinish-"+i);
      if (sel) sel.value = p.id;
    }});
  }}
}}

// ── Core utility functions ────────────────────────────────────────────────────
function currentTab() {{
  const active = document.querySelector(".tab.active");
  if (!active) return "stats";
  const idx = Array.from(document.querySelectorAll(".tab")).indexOf(active);
  return ["stats","live","history","host"][idx] || "stats";
}}

function refreshPage() {{
  // Reload data without going back to stats
  const tab = currentTab();
  window.location.href = window.location.href.split("?")[0] + "?guild=" + guild + "&tab=" + tab;
}}

// Read tab from URL on load
(function() {{
  const urlTab = new URLSearchParams(window.location.search).get("tab");
  if (urlTab && urlTab !== "stats") {{
    setTimeout(() => switchTab(urlTab), 50);
  }}
}})();

// ── Civ Hover Tooltip ─────────────────────────────────────────────────────────
const _tooltip = document.getElementById("civTooltip");
let _tooltipTimeout = null;

function showCivTooltip(civName, anchorEl) {{
  const civ = CIVPEDIA[civName];
  if (!civ) return;
  clearTimeout(_tooltipTimeout);

  const ability = civ.entries.find(e => e.type === "Ability");
  const units = civ.entries.filter(e => e.type === "Unit" || e.type === "Great Person");
  const buildings = civ.entries.filter(e => e.type === "Building");
  const improvements = civ.entries.filter(e => e.type === "Improvement");
  const isCoastal = COASTAL_CIVS.has(civName);

  const rows = [
    ...(ability ? [{{type:"ABILITY", color:"#f97316", icon:"⚡", name: ability.name, desc: ability.desc}}] : []),
    ...units.map(u => ({{type:"UNIT", color:"#ef4444", icon:"⚔️", name: u.name, desc: u.desc}})),
    ...buildings.map(b => ({{type:"BUILDING", color:"#3b82f6", icon:"🏛️", name: b.name, desc: b.desc}})),
    ...improvements.map(i => ({{type:"IMPROVEMENT", color:"#22c55e", icon:"🔧", name: i.name, desc: i.desc}})),
  ];

  _tooltip.innerHTML = `
    <div class="civ-tooltip-name">${{isCoastal?"⛵ ":"🏕️ "}}{{}}{{}}</div>
    <div class="civ-tooltip-leader">${{civ.leader}}</div>
    ${{rows.map(r => `
      <div class="civ-tooltip-row" style="border-left:2px solid ${{r.color}};padding-left:8px">
        <div class="civ-tooltip-type" style="color:${{r.color}}">${{r.icon}} ${{r.type}}</div>
        <div class="civ-tooltip-title">${{r.name}}</div>
        ${{r.desc ? `<div class="civ-tooltip-desc">${{r.desc}}</div>` : ""}}
      </div>`).join("")}}`;

  // Hacky but needed — inject name after to avoid f-string conflict
  _tooltip.querySelector(".civ-tooltip-name").textContent = (isCoastal?"⛵ ":"🏕️ ") + civName;

  // Position tooltip near the element
  const rect = anchorEl.getBoundingClientRect();
  const tipW = 240, tipH = _tooltip.offsetHeight || 200;
  let left = rect.right + 10;
  let top = rect.top;
  if (left + tipW > window.innerWidth - 10) left = rect.left - tipW - 10;
  if (top + tipH > window.innerHeight - 10) top = window.innerHeight - tipH - 10;
  if (top < 10) top = 10;
  _tooltip.style.left = left + "px";
  _tooltip.style.top = top + "px";
  _tooltip.classList.add("visible");
}}

function hideCivTooltip() {{
  _tooltipTimeout = setTimeout(() => _tooltip.classList.remove("visible"), 100);
}}

async function pickCiv(hostId, civ) {{
  const res = await fetch("/api/game/pick", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId, civ}})
  }});
  if (res.ok) {{ refreshPage(); }}
  else {{ const t = await res.text(); alert("Could not pick: " + t); }}
}}

async function cancelGame(hostId, type) {{
  if (!confirm("Cancel this " + type + "? No Elo changes will be made.")) return;
  const res = await fetch("/api/game/cancel", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId}})
  }});
  if (res.ok) {{ refreshPage(); }}
  else {{ const t = await res.text(); alert("Could not cancel: " + t); }}
}}

async function hostCreateLobby() {{
  const difficulty = document.getElementById("hDiff").value;
  const res = await fetch("/api/lobby/create", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, difficulty}})
  }});
  if (res.ok) {{ refreshPage(); }}
  else {{ const t = await res.text(); alert("Could not create lobby: " + t); }}
}}

async function hostStartGame(hostId) {{
  const mapType = document.getElementById("hMapType").value;
  const res = await fetch("/api/game/start", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId, map_type: mapType}})
  }});
  if (res.ok) {{ refreshPage(); }}
  else {{ const t = await res.text(); alert("Could not start: " + t); }}
}}

async function hostSubmitResults(hostId, count) {{
  const order = [];
  for (let i = 0; i < count; i++) {{
    const val = document.getElementById("hFinish-"+i)?.value;
    if (!val || order.includes(val)) {{ alert("Check finishing order — no duplicates allowed."); return; }}
    order.push(val);
  }}
  const victoryType = document.getElementById("hVictory").value || null;
  const res = await fetch("/api/game/report", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId, order, victory_type: victoryType}})
  }});
  if (res.ok) {{ refreshPage(); }}
  else {{ const t = await res.text(); alert("Could not submit: " + t); }}
}}

// ── Achievement Toasts ────────────────────────────────────────────────────────
function showAchievementToast(icon, name, desc, color) {{
  const container = document.getElementById("toastContainer");
  if (!container) return;
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `
    <div class="toast-bar" style="background:${{color}}"></div>
    <div class="toast-shine"></div>
    <span class="toast-icon">${{icon}}</span>
    <div>
      <div class="toast-label">ACHIEVEMENT UNLOCKED</div>
      <div class="toast-name">${{name}}</div>
      <div class="toast-desc">${{desc}}</div>
    </div>
    <div class="toast-prog" style="background:${{color}}"></div>`;
  container.appendChild(el);
  requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.add("show")));
  setTimeout(() => {{
    el.classList.remove("show");
    setTimeout(() => el.remove(), 400);
  }}, 4500);
}}

function checkAndShowToasts(prevData, newData) {{
  // Compare old vs new achievement states and show toasts for newly unlocked ones
  if (!prevData || !newData) return;
  const toastColors = {{
    "Explorer":"#f97316","World Traveller":"#f97316","Tactician":"#22c55e","Polymath":"#22c55e",
    "Sea Dog":"#06b6d4","Landlubber":"#22c55e","Prince":"#3b82f6","King":"#06b6d4",
    "Emperor":"#a855f7","Deity":"#f97316","Grand Victor":"#eab308","Full House":"#eab308",
    "Domination I":"#ef4444","Domination V":"#ef4444","Domination X":"#ef4444",
    "Science I":"#3b82f6","Science V":"#3b82f6","Science X":"#3b82f6",
    "Culture I":"#a855f7","Culture V":"#a855f7","Culture X":"#a855f7",
    "Diplomatic I":"#eab308","Diplomatic V":"#eab308","Diplomatic X":"#eab308",
  }};
  ACHIEVEMENTS.forEach((a, idx) => {{
    const wasUnlocked = a.check(prevData);
    const isUnlocked  = a.check(newData);
    if (!wasUnlocked && isUnlocked) {{
      setTimeout(() => showAchievementToast(a.icon, a.name, a.desc, toastColors[a.name]||"#f97316"), idx*600);
    }}
  }});
}}

// ── Auth ─────────────────────────────────────────────────────────────────────
const authArea = document.getElementById("authArea");
const guild = new URLSearchParams(window.location.search).get("guild") || GUILD_ID || "";

if (LOGGED_IN_ID && LOGGED_IN_NAME) {{
  const displayLabel = DISPLAY_NAME || LOGGED_IN_NAME;
  const favLabel = FAV_CIV ? ` · ${{FAV_CIV}}` : "";
  authArea.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px">
      <button class="btn btn-ghost" id="nameBtn" style="font-size:10px;padding:4px 10px" onclick="handleNameClick()" ondblclick="return false">👤 ${{displayLabel}}${{favLabel}}</button>
      <a href="/logout" style="font-size:10px;color:#475569;text-decoration:none;padding:4px 10px;border:1px solid #1e2130;border-radius:6px">logout</a>
    </div>`;

  // Show history filter
  const hf = document.getElementById("histFilter");
  if (hf) hf.style.display = "flex";
  // Show host tab
  const ht = document.getElementById("hostTab");
  if (ht) ht.style.display = "block";

  // Auto-open this player's profile
  const myIdx = PLAYERS.findIndex(p => p.id === LOGGED_IN_ID);
  if (myIdx >= 0) {{
    setTimeout(() => {{
      document.querySelectorAll(".pcard").forEach(b => b.classList.remove("profile-active"));
      const myCard = document.getElementById("pcard-"+LOGGED_IN_ID);
      if (myCard) myCard.classList.add("profile-active");
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
  const myData = (LOGGED_IN_ID && LB_DATA[LOGGED_IN_ID]) ? LB_DATA[LOGGED_IN_ID] : null;
  const myPrefs = PLAYER_PREFS[LOGGED_IN_ID] || {{}};
  const unlockedTitles = TITLES.filter(t => !t.req || t.req(myData || {{}}));
  const colours = ["#f97316","#3b82f6","#a855f7","#22c55e","#ef4444","#eab308","#06b6d4","#ec4899","#f43f5e","#10b981","#8b5cf6","#0ea5e9","#ffffff","#94a3b8"];
  const currentColour = myPrefs.colour || "#f97316";
  const currentTitle = myPrefs.title || "";
  mc.innerHTML = `
    <div class="modal-overlay" onclick="if(event.target===this)closeModal()">
      <div class="modal">
        <div class="modal-title">⚙️ My Settings</div>
        <label class="form-label">DISPLAY NAME</label>
        <input class="form-input" id="settingName" placeholder="Your display name" value="${{DISPLAY_NAME || LOGGED_IN_NAME}}">
        <label class="form-label">PLAYER COLOUR</label>
        <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px">
          ${{colours.map(c => `<div onclick="selectColour('${{c}}')" id="col-${{c.replace('#','')}}" style="width:28px;height:28px;border-radius:50%;background:${{c}};cursor:pointer;border:3px solid ${{c===currentColour?'#fff':'transparent'}};transition:border 0.15s"></div>`).join("")}}
        </div>
        <input type="hidden" id="settingColour" value="${{currentColour}}">
        <label class="form-label">TITLE</label>
        <select class="form-select" id="settingTitle">
          <option value="">— None —</option>
          ${{unlockedTitles.map(t => `<option value="${{t.id}}"${{t.id===currentTitle?" selected":""}}>${{t.label}}</option>`).join("")}}
        </select>
        <label class="form-label">FAVOURITE CIV</label>
        <select class="form-select" id="settingCiv">
          <option value="">— None —</option>
          ${{ALL_CIVS.map(c => `<option value="${{c}}"${{c===(myPrefs.fav_civ||FAV_CIV)?" selected":""}}>${{c}}</option>`).join("")}}
        </select>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
          <button class="btn btn-ghost" onclick="closeModal()">Cancel</button>
          <button class="btn btn-primary" onclick="saveSettings()">Save</button>
        </div>
      </div>
    </div>`;
}}

function selectColour(c) {{
  document.getElementById("settingColour").value = c;
  document.querySelectorAll("[id^='col-']").forEach(el => el.style.border = "3px solid transparent");
  const el = document.getElementById("col-" + c.replace("#",""));
  if (el) el.style.border = "3px solid #fff";
}}

async function saveSettings() {{
  const name   = document.getElementById("settingName")?.value.trim() || "";
  const civ    = document.getElementById("settingCiv")?.value || "";
  const colour = document.getElementById("settingColour")?.value || "";
  const title  = document.getElementById("settingTitle")?.value || "";
  const res = await fetch("/api/prefs", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, display_name: name, fav_civ: civ, colour, title}})
  }});
  if (res.ok) {{ closeModal(); refreshPage(); }}
  else {{ const t = await res.text(); alert("Failed to save: " + t); }}
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
  if (res.ok) {{ closeModal(); switchTab("live"); refreshPage(); }}
  else {{ alert("Failed to create lobby."); }}
}}

async function joinLobby(hostId) {{
  if (!LOGGED_IN_ID) {{ alert("Please log in to join a lobby."); return; }}
  const res = await fetch("/api/lobby/join", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId}})
  }});
  if (res.ok) {{ refreshPage(); }}
  else {{ const t = await res.text(); alert("Could not join: " + t); }}
}}

async function leaveLobby(hostId) {{
  const res = await fetch("/api/lobby/leave", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{guild: guild, host_id: hostId}})
  }});
  if (res.ok) {{ refreshPage(); }}
  else {{ alert("Failed to leave lobby."); }}
}}

function closeModal() {{ document.getElementById("modalContainer").innerHTML = ""; }}

// ── Easter Egg ────────────────────────────────────────────────────────────────
let _nameClickCount = 0;
let _nameClickTimer = null;
function handleNameClick() {{
  _nameClickCount++;
  clearTimeout(_nameClickTimer);
  if (_nameClickCount === 3) {{
    _nameClickCount = 0;
    openEasterEgg();
    return;
  }}
  if (_nameClickCount === 1) {{
    // First click — also open settings after short delay if no triple click
    _nameClickTimer = setTimeout(() => {{
      if (_nameClickCount < 3) openSettingsModal();
      _nameClickCount = 0;
    }}, 300);
  }} else {{
    _nameClickTimer = setTimeout(() => {{ _nameClickCount = 0; }}, 400);
  }}
}}

let _eggExpanded = null;

function openEasterEgg() {{
  document.getElementById("easterEgg").style.display = "block";
  _eggExpanded = null;
  buildEggCards();
}}

function buildEggCards() {{
  const tiers = ["normal","bronze","silver","gold","diamond"];
  const grid = document.getElementById("eggCards");
  grid.innerHTML = "";

  tiers.forEach(tier => {{
    const info = TIER_LABELS[tier];
    const tierClass = tier !== "normal" ? "tier-"+tier : "";

    // Build a real card just like buildCivGrid but forced as Rome + this tier
    const civ = CIVPEDIA["Rome"];
    if (!civ) return;
    const allEntries = civ.entries.filter(e => e.type !== "Bias");
    const entryRows = allEntries.map(e => {{
      const color = TYPE_COLORS[e.type] || "#475569";
      const icon  = CIVPEDIA_ICONS[e.type] || "•";
      return `<div style="padding-left:7px;border-left:2px solid ${{color}}55;margin-bottom:4px">
        <div style="font-size:8px;color:${{color}};letter-spacing:1px;margin-bottom:1px">${{icon}} ${{e.type.toUpperCase()}}</div>
        <div style="font-size:9px;font-weight:700;color:#e2e8f0;margin-bottom:2px;line-height:1.2">${{e.name}}</div>
        ${{e.desc ? `<div style="font-size:8px;color:#64748b;line-height:1.5;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${{e.desc}}</div>` : ""}}
      </div>`;
    }}).join("");

    // Build expand detail
    const sections = allEntries.map(e => {{
      const color = TYPE_COLORS[e.type] || "#94a3b8";
      const icon  = CIVPEDIA_ICONS[e.type] || "•";
      const desc = (e.desc||"")
        .replace(/\(vs\. [\d\.]+\)/g, m => `<span style="color:#64748b">${{m}}</span>`)
        .replace(/\(from [\d\.]+[^)]*\)/g, m => `<span style="color:#64748b">${{m}}</span>`);
      return `<div class="civ-section" style="border-left:3px solid ${{color}}">
        <div class="civ-section-type" style="color:${{color}}">${{icon}} ${{e.type.toUpperCase()}}</div>
        ${{e.name ? `<div class="civ-section-name">${{e.name}}</div>` : ""}}
        ${{desc ? `<div class="civ-section-desc">${{desc}}</div>` : ""}}
      </div>`;
    }}).join("");

    const upgradeHtml = `<div style="margin-top:14px;padding:12px;background:#080a0f;border:1px solid #1e2130;border-radius:8px;text-align:center">
      <div style="font-size:11px;color:${{info.color}};font-weight:700;margin-bottom:4px">${{info.icon}} ${{info.label}} Tier</div>
      <div style="font-size:9px;color:#475569">Demo card — full upgrade system in civilopedia</div>
    </div>`;

    const tile = document.createElement("div");
    tile.className = "civ-tile" + (tierClass ? " "+tierClass : "");
    tile.dataset.tier = tier;
    tile.dataset.eggtier = tier;
    tile.innerHTML = `
      <div class="civ-tile-shine"></div>
      ${{tier !== "normal" ? `<div class="tier-badge">${{info.icon}}</div>` : ""}}
      <div class="civ-card-content" style="display:flex;flex-direction:column;flex:1;min-height:0">
        <div class="civ-tile-top">
          <div class="civ-tile-map">🏕️</div>
          <span style="font-size:8px;color:${{info.color}};font-weight:700;background:#080a0f;border:1px solid ${{info.color}}44;border-radius:5px;padding:1px 5px">${{info.label.toUpperCase()}}</span>
        </div>
        <div class="civ-tile-name" style="color:#e2e8f0">Rome</div>
        <div class="civ-tile-leader">Augustus Caesar</div>
        <div class="civ-tile-divider"></div>
        <div style="flex:1;display:flex;flex-direction:column;gap:4px">${{entryRows}}</div>
      </div>
      <div class="civ-detail">
        <div class="civ-detail-header">
          <div>
            <div class="civ-detail-title">Rome</div>
            <div class="civ-detail-leader">Leader: Augustus Caesar</div>
          </div>
          <span style="font-size:10px;color:#475569;cursor:pointer;padding:3px 8px;border:1px solid #1e2130;border-radius:6px;flex-shrink:0" onclick="closeEggCard(event)">✕</span>
        </div>
        ${{sections}}
        ${{upgradeHtml}}
      </div>`;

    tile.onclick = (ev) => {{
      if (ev.target.closest(".civ-detail")) return;
      const t = tile.dataset.eggtier;
      if (_eggExpanded === t) {{
        _eggExpanded = null;
        tile.classList.remove("expanded");
      }} else {{
        _eggExpanded = t;
        grid.querySelectorAll(".civ-tile").forEach(c => c.classList.remove("expanded"));
        tile.classList.add("expanded");
        setTimeout(() => tile.scrollIntoView({{behavior:"smooth",block:"nearest"}}), 50);
      }}
    }};

    addTilt(tile);
    grid.appendChild(tile);
  }});
}}

function closeEggCard(event) {{
  event.stopPropagation();
  _eggExpanded = null;
  document.querySelectorAll("#eggCards .civ-tile").forEach(c => c.classList.remove("expanded"));
}}

function closeEgg() {{
  document.getElementById("easterEgg").style.display = "none";
}}

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
    # Create player record if they haven't played yet
    _, username_str = verify_session_token(request.cookies.get("session"))
    get_player(data, user_id, username_str)
    data["players"][user_id].setdefault("prefs", {})
    colour = body.get("colour", "")
    title  = body.get("title", "")
    if display_name:
        data["players"][user_id]["prefs"]["display_name"] = display_name
        data["players"][user_id]["name"] = display_name
    data["players"][user_id]["prefs"]["fav_civ"] = fav_civ
    if colour:
        data["players"][user_id]["prefs"]["colour"] = colour
    data["players"][user_id]["prefs"]["title"] = title
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

async def handle_api_game_start(request):
    session_token = request.cookies.get("session")
    if not session_token: return web.Response(text="Not logged in", status=401)
    user_id, _ = verify_session_token(session_token)
    if not user_id: return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    host_id = body.get("host_id", "")
    map_type = body.get("map_type", "any")
    if user_id != host_id: return web.Response(text="Only the host can start", status=403)
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    if host_id not in data["lobbies"]: return web.Response(text="Lobby not found", status=404)
    lobby = data["lobbies"][host_id]
    if len(lobby["players"]) < 2: return web.Response(text="Need at least 2 players", status=400)
    game_id = host_id
    import random as _random
    draft = build_draft(lobby["players"], map_type) if map_type != "skip" else None
    for pid in lobby["players"]:
        data["active_games"][pid] = {"civ": None, "game_id": game_id}
    data["game_groups"][game_id] = {
        "players": lobby["players"], "player_names": lobby["player_names"],
        "player_civs": [None]*len(lobby["players"]),
        "draft": draft, "picks": {},
        "difficulty": lobby.get("difficulty","Prince"), "map_type": map_type,
    }
    del data["lobbies"][host_id]
    save_all_data(all_data)
    return web.Response(text="OK")

async def handle_api_game_pick(request):
    session_token = request.cookies.get("session")
    if not session_token: return web.Response(text="Not logged in", status=401)
    user_id, _ = verify_session_token(session_token)
    if not user_id: return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    host_id = body.get("host_id", "")
    civ = body.get("civ", "")
    matched = next((c for c in ALL_CIVS if c.lower() == civ.lower()), None)
    if not matched: return web.Response(text="Unknown civ", status=400)
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    group = data.get("game_groups", {}).get(host_id)
    if not group: return web.Response(text="Game not found", status=404)
    if user_id not in group["players"]: return web.Response(text="Not in this game", status=403)
    picks = group.get("picks", {})
    if user_id in picks: return web.Response(text="Already picked", status=400)
    draft = group.get("draft", {})
    if draft and matched not in draft.get(user_id, []):
        return web.Response(text="Civ not in your draft", status=400)
    if matched in picks.values(): return web.Response(text="Civ already picked", status=400)
    picks[user_id] = matched
    group["picks"] = picks
    data["active_games"][user_id]["civ"] = matched
    idx = group["players"].index(user_id)
    group["player_civs"][idx] = matched
    save_all_data(all_data)
    return web.Response(text="OK")

async def handle_api_game_cancel(request):
    session_token = request.cookies.get("session")
    if not session_token: return web.Response(text="Not logged in", status=401)
    user_id, _ = verify_session_token(session_token)
    if not user_id: return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    host_id = body.get("host_id", "")
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    # Cancel lobby
    if host_id in data.get("lobbies", {}):
        if user_id != host_id: return web.Response(text="Only host can cancel", status=403)
        del data["lobbies"][host_id]
        save_all_data(all_data)
        return web.Response(text="OK")
    # Cancel active game
    group = data.get("game_groups", {}).get(host_id)
    if group and user_id in group.get("players", []):
        for pid in group["players"]:
            data["active_games"].pop(pid, None)
        del data["game_groups"][host_id]
        save_all_data(all_data)
        return web.Response(text="OK")
    return web.Response(text="Not found", status=404)

async def handle_api_game_report(request):
    session_token = request.cookies.get("session")
    if not session_token: return web.Response(text="Not logged in", status=401)
    user_id, _ = verify_session_token(session_token)
    if not user_id: return web.Response(text="Invalid session", status=401)
    body = await request.json()
    guild_id = body.get("guild", "")
    host_id = body.get("host_id", "")
    order = body.get("order", [])  # list of player ids in finishing order
    victory_type = body.get("victory_type", None)
    if user_id != host_id: return web.Response(text="Only host can report", status=403)
    all_data = load_all_data()
    data = get_server_data(all_data, guild_id)
    group = data.get("game_groups", {}).get(host_id)
    if not group: return web.Response(text="Game not found", status=404)
    expected = set(group["players"])
    # order may contain ids or names
    resolved_order = []
    for val in order:
        if val in expected:
            resolved_order.append(val)
        else:
            match = next((pid for pid in expected if data["players"].get(pid,{}).get("name","") == val), None)
            if match: resolved_order.append(match)
    if set(resolved_order) != expected or len(resolved_order) != len(expected):
        return web.Response(text="Invalid finishing order", status=400)
    player_info = []
    for i, pid in enumerate(resolved_order):
        p = get_player(data, pid)
        entry = data["active_games"].get(pid, {})
        civ = entry.get("civ") if isinstance(entry, dict) else None
        player_info.append({"id": pid, "finish": i+1, "elo": p["elo"], "old_elo": p["elo"], "civ": civ})
    new_elos = calc_multiplayer_elo(player_info)
    difficulty = group.get("difficulty", "Prince")
    draft_pools = group.get("draft", {}) or {}
    for i, info in enumerate(player_info):
        p = get_player(data, info["id"])
        old_elo, new_elo = info["old_elo"], new_elos[i]
        p["elo"] = new_elo
        if i == 0:
            p["wins"] += 1
        else: p["losses"] += 1
        civ = info["civ"]
        if civ:
            p["civs"].setdefault(civ, {"wins":0,"losses":0})
            if i == 0: p["civs"][civ]["wins"] += 1
            else: p["civs"][civ]["losses"] += 1
        data["active_games"].pop(info["id"], None)
    data["game_groups"].pop(host_id, None)
    data["matches"].append({
        "type": f"{len(resolved_order)}-player",
        "difficulty": difficulty,
        "map_type": group.get("map_type","any"),
        "victory_type": victory_type,
        "draft_pools": draft_pools,
        "players": [{"id": info["id"], "finish": info["finish"], "civ": info["civ"],
                     "elo_before": info["old_elo"], "elo_after": new_elos[i]}
                    for i, info in enumerate(player_info)],
        "played_at": datetime.utcnow().isoformat()
    })
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
    app.router.add_post("/api/game/start", handle_api_game_start)
    app.router.add_post("/api/game/pick", handle_api_game_pick)
    app.router.add_post("/api/game/cancel", handle_api_game_cancel)
    app.router.add_post("/api/game/report", handle_api_game_report)
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
