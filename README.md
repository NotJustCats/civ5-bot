# Civ 5 Ranked Bot

A Discord bot for running a ranked ladder for your Civilization 5 group. Play games, report results, and track who is the best.

---

## How to play

### Starting a game

1. The host opens a lobby with `/open_lobby [civ]` — pick the civ you are playing
2. Everyone else joins with `/join_lobby @host [civ]` — pick your civ
3. The host starts the game with `/start_game`
4. Go play your game
5. When finished, the host reports the finishing order with `/report_results @1st @2nd @3rd ...`

That's it. Elo ratings update automatically for everyone in the game.

### Example

```
/open_lobby America
/join_lobby @mew Korea
/join_lobby @mew Japan
/start_game
... play the game ...
/report_results @NotJustCats @mew @Yvonne
```

### Cancelling a game

- If the game has not started yet, the host can cancel the lobby with `/cancel_game`
- If the game has already started, any player in the game can use `/cancel_game`
- Cancelling never affects Elo ratings

---

## Commands

### Playing

`/open_lobby [civ]`
Open a ranked lobby. You must pick the civ you are playing. Only one lobby per person at a time.

`/join_lobby @host [civ]`
Join someone else's open lobby. Pick your civ — you cannot pick one that is already taken.

`/leave_lobby`
Leave a lobby before the game starts. If the host leaves, the lobby closes for everyone.

`/start_game`
Lock in the lobby and begin the game. Requires at least 2 players. Host only.

`/cancel_game`
Cancel an open lobby or an in-progress game. No Elo changes are made.

`/report_results @1st @2nd @3rd ...`
Report the finishing order after a game. Tag players in the order they finished. Host only. You must include everyone who was in the lobby.

### Stats

`/leaderboard`
Shows the top 10 players on this server ranked by Elo.

`/profile`
Shows your own stats — Elo, rank, win/loss record, and your top 5 civs by win rate. Use `/profile @player` to view someone else.

`/stats`
Shows server-wide totals — number of matches played, players ranked, and game sizes.

`/graph`
Posts a link to the live stats website showing Elo history, most played civs, and the leaderboard.

`/civs`
Shows a list of all valid civilization names you can use when joining a lobby.

---

## Ranks

Your rank is based on your Elo rating and updates after every game.

| Rank | Elo |
|---|---|
| Deity | 1800 and above |
| Emperor | 1600 to 1799 |
| King | 1400 to 1599 |
| Prince | 1200 to 1399 |
| Chieftain | 1000 to 1199 |
| Settler | Below 1000 |

Everyone starts at 1000 Elo. The more you play and win, the higher you climb.

---

## Civilizations

The bot supports all base game civs plus the full Lek mod civ list. When joining a lobby you must type the civ name exactly as it appears in `/civs`. Two players cannot pick the same civ in the same game.

---

## Stats website

Use `/graph` to get a link to the live stats page. It shows:

- Elo progression over time for every player
- A chart of the most played civilizations
- The full leaderboard with win rates

The page updates every time you refresh it.