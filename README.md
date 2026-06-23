# FIFA World Cup 2026 — Odds Analysis & Arbitrage Engine

Real-time odds fetching, analysis, and arbitrage detection for FIFA World Cup 2026 using [The Odds API v4](https://the-odds-api.com/).

## Files

| File | Purpose |
|---|---|
| `odds-api.py` | Core API client — fetches sports, events, and h2h odds; converts to implied probabilities |
| `analyze_odds.py` | One-shot analysis — consensus win probabilities, match competitiveness, bookmaker vig, disagreement |
| `arb_scanner.py` | Arbitrage scanner — finds best odds per outcome across books and identifies risk-free profit |
| `trading_strategy.py` | Real-time strategy engine — continuous polling, confidence scoring, Kelly sizing, position tracking |
| `odds-api-key.yml` | API key file (gitignored) |

## Setup

```bash
pip install requests pyyaml pandas
```

Create `odds-api-key.yml` in this folder:

```yaml
api_key: your_key_here
```

Get a free key at [the-odds-api.com](https://the-odds-api.com/) — free tier includes 500 credits/month.

## Usage

### Fetch and analyse odds
```bash
python analyze_odds.py
```
Outputs: consensus win probabilities for all 48 matches, most one-sided and competitive games, bookmaker vig ranking, and cross-book disagreement.

### Scan for arbitrage
```bash
python arb_scanner.py
```
Finds matches where betting all outcomes across different bookmakers guarantees a profit. Shows exact stakes per leg for a configurable total stake (default $1,000).

### Run the real-time strategy engine
```bash
# Single poll
python trading_strategy.py --once

# Continuous (polls every 60s)
python trading_strategy.py --bankroll 10000

# N iterations then stop
python trading_strategy.py --bankroll 10000 --iterations 5
```

## How arbitrage works

For a 3-way market (home / draw / away), an arbitrage exists when:

```
1/best_home_odds + 1/best_draw_odds + 1/best_away_odds < 1
```

**Example — Panama vs England:**
| Outcome | Book | Odds | Stake | Return |
|---|---|---|---|---|
| England | GTbets | 1.32 | $770 | $1,016 |
| Panama | Coolbet | 13.00 | $78 | $1,014 |
| Draw | Everygame | 7.25 | $138 | $1,001 |
| **Total** | | | **$986** | **~$1,011** |

Profit: **+$25 (~2.5%) regardless of result.**

## Strategy engine architecture

```
PollingEngine    — rate-managed API calls (240 calls/month budget)
    │
    ▼
OddsTracker      — rolling history; detects stale (>2 min) and outlier odds
    │
    ▼
ArbDetector      — finds arbs, scores confidence 0–1
                   🟢 ≥ 0.70 — act on this
                   🟡 < 0.70 — treat as signal only, verify manually
    │
    ▼
PositionSizer    — fractional Kelly: edge × confidence × 0.25 × bankroll
                   capped at 5% of bankroll per arb
    │
    ▼
PositionTracker  — monitors open legs; alerts if arb breaks mid-execution
```

## Key bookmakers by role

| Role | Bookmakers | Notes |
|---|---|---|
| Best favourite odds | GTbets, 1xBet | Consistent source of arb edges |
| Best underdog odds | 1xBet, Coolbet, Nordic Bet | High outlier odds on minnows |
| Best draw odds | Betfair, Tipico, Unibet | Exchange prices most efficient |
| Lowest vig | Matchbook (0.8%), 1xBet (1.6%), Betfair Exchange (1.9%) | Best value overall |
| Highest vig | Winamax FR (12.8%), 888sport (9.0%), BetMGM (8.9%) | Avoid for arb legs |

## Simulation results (June 22, 2026)

Virtual $10,000 bankroll. US books only. Ran for ~117 polls (≈4 hours) before API quota exhausted.

### Resolved bets

| Match | Result | Staked | Return | Profit |
|---|---|---|---|---|
| Argentina vs Austria | Argentina win (2-0) | $35.45 | $35.95 | **+$0.50 (+1.41%)** |

### Bets placed (legs)

| Match | Outcome | Odds | Book | Stake |
|---|---|---|---|---|
| Argentina vs Austria | Argentina | 1.53 | GTbets | $23.50 |
| Argentina vs Austria | Austria | 8.52 | BetOnline.ag | $4.22 |
| Argentina vs Austria | Draw | 4.65 | LowVig.ag | $7.73 |
| Senegal vs Iraq | Senegal | 1.37 | GTbets | $6.83 |
| Senegal vs Iraq | Iraq | 11.00 | FanDuel | $0.85 |
| Senegal vs Iraq | Draw | 5.70 | BetOnline.ag | $1.64 |
| Algeria vs Austria | Algeria | 3.40 | FanDuel | $6.67 |
| Algeria vs Austria | Austria | 2.75 | DraftKings | $8.24 |
| Algeria vs Austria | Draw | 3.00 | BetRivers | $7.55 |
| Morocco vs Haiti | Morocco | 1.23 | GTbets | $6.47 |
| Morocco vs Haiti | Haiti | 17.00 | FanDuel | $0.47 |
| Morocco vs Haiti | Draw | 8.00 | BetRivers | $0.99 |

### Portfolio snapshot (at crash)

| Metric | Value |
|---|---|
| Bankroll | $10,000.50 |
| P&L | +$0.50 |
| Open bets | 3 (Senegal/Iraq, Algeria/Austria, Morocco/Haiti) |
| Resolved | 1 |
| Total staked (resolved) | $35.45 |
| ROI (resolved) | +1.41% |

**Note:** Simulation stopped at Poll #117 — free-tier API key hit the 500 credits/month limit (401 Unauthorized). Each poll costs ~2 credits (h2h × us+eu regions).

## Important caveats

- **Arb windows close fast** — odds move in seconds once books adjust; all legs must be placed simultaneously
- **Account limits** — bookmakers flag and limit accounts that consistently exploit arb
- **1xBet** has withdrawal restrictions in some regions — verify before depositing
- **This engine generates signals only** — it does not place bets; execution requires funded accounts at each book
- **API quota** — 500 credits/month free; each call costs 2 credits (h2h × us + eu regions)
