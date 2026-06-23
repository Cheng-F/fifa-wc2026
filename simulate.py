"""
Virtual simulation of the US arb strategy against a $10,000 bankroll.

Flow each poll:
  1. Fetch live odds → detect arbs (US books only)
  2. For new arbs: place all 3 legs at detected odds (instantly, no slippage)
  3. Fetch scores → resolve any completed games
  4. Print running P&L dashboard

Assumptions (optimistic baseline):
  - All 3 legs fill at the displayed odds (no slippage)
  - No account limits or bet rejections
  These will be relaxed with --slippage and --fill-rate flags.

Usage:
  python simulate.py                        # run indefinitely
  python simulate.py --iterations 20        # 20 polls then report
  python simulate.py --slippage 0.02        # simulate 2% odds slippage per leg
  python simulate.py --fill-rate 0.8        # 80% chance each leg fills
"""

import importlib.util
import os, sys, time, random, argparse
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import yaml
import requests

# ── load shared modules ───────────────────────────────────────────────────────
_spec = importlib.util.spec_from_file_location("odds_api", "odds-api.py")
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

with open("odds-api-key.yml") as f:
    API_KEY = yaml.safe_load(f)["api_key"]
BASE_URL = "https://api.the-odds-api.com/v4"

# ── US book allowlist (mirrors trading_strategy.py) ───────────────────────────
US_BOOKS = {
    "betmgm", "betonlineag", "betrivers", "betus", "bovada",
    "draftkings", "fanduel", "lowvig", "mybookieag",
    "gtbets", "everygame", "betanysports",
}
EXCLUDE_BOOKS = {"betfair_ex", "matchbook"}

# ── config ────────────────────────────────────────────────────────────────────
SPORT            = "soccer_fifa_world_cup"
BANKROLL         = 10_000.0
KELLY_FRACTION   = 0.25
MIN_EDGE         = 0.003
MAX_STAKE_PCT    = 0.05
POLL_INTERVAL    = 60


# ── data structures ───────────────────────────────────────────────────────────

@dataclass
class VirtualBet:
    game_id:    str
    match:      str
    kickoff:    str
    placed_at:  datetime
    legs: list[dict]        # [{outcome, odds, book, stake, filled}]
    total_stake: float
    resolved:   bool = False
    won_return: float = 0.0
    result:     Optional[str] = None   # winning outcome name

    @property
    def all_filled(self) -> bool:
        return all(leg["filled"] for leg in self.legs)

    @property
    def profit(self) -> float:
        if not self.resolved:
            return 0.0
        return self.won_return - self.total_stake


# ── score fetcher ─────────────────────────────────────────────────────────────

def fetch_scores() -> dict[str, dict]:
    """Return {game_id: {home_team, away_team, completed, winner}} for recent games."""
    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{SPORT}/scores",
            params={"apiKey": API_KEY, "daysFrom": 2},
            timeout=10,
        )
        resp.raise_for_status()
        scores = {}
        for game in resp.json():
            if not game.get("completed"):
                continue
            home = game["home_team"]
            away = game["away_team"]
            result = game.get("scores") or []
            if not result:
                continue
            # determine winner from scores list
            score_map = {s["name"]: int(s["score"]) for s in result if s.get("score") is not None}
            home_score = score_map.get(home, 0)
            away_score = score_map.get(away, 0)
            if home_score > away_score:
                winner = home
            elif away_score > home_score:
                winner = away
            else:
                winner = "Draw"
            scores[game["id"]] = {
                "home_team": home,
                "away_team": away,
                "winner":    winner,
                "score":     f"{home_score}-{away_score}",
            }
        return scores
    except Exception as e:
        print(f"  [WARN] Score fetch failed: {e}")
        return {}


# ── arb detection (US books only) ────────────────────────────────────────────

def fetch_arbs() -> list[dict]:
    """Fetch live odds and return list of arb opportunities (US books only)."""
    _devnull = open(os.devnull, "w")
    sys.stdout, _real = _devnull, sys.stdout
    try:
        games = _mod.get_odds(
            sport="soccer_fifa_world_cup",
            regions="us,eu",
            markets="h2h",
            odds_format="decimal",
        )
    finally:
        sys.stdout = _real
        _devnull.close()

    arbs = []
    now = datetime.now(timezone.utc)

    for game in games:
        ko = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
        if ko <= now:
            continue   # already kicked off

        best: dict[str, tuple[float, str, str]] = {}
        for bm in game["bookmakers"]:
            if bm["key"] not in US_BOOKS or bm["key"] in EXCLUDE_BOOKS:
                continue
            for market in bm["markets"]:
                if market["key"] != "h2h":
                    continue
                for o in market["outcomes"]:
                    name, price = o["name"], o["price"]
                    if name not in best or price > best[name][0]:
                        best[name] = (price, bm["title"], bm["key"])

        if len(best) != 3:
            continue

        sum_imp = sum(1 / v[0] for v in best.values())
        pct     = (1 / sum_imp - 1) * 100
        if pct < MIN_EDGE * 100:
            continue

        legs = []
        for outcome, (odds, book, book_key) in best.items():
            stake_pct = (1 / odds) / sum_imp
            legs.append({
                "outcome":  outcome,
                "odds":     odds,
                "book":     book,
                "book_key": book_key,
                "stake_pct": stake_pct,
            })

        arbs.append({
            "game_id":    game["id"],
            "match":      f"{game['home_team']} vs {game['away_team']}",
            "kickoff":    game["commence_time"],
            "profit_pct": round(pct, 3),
            "sum_implied": round(sum_imp, 4),
            "legs":       legs,
        })

    return sorted(arbs, key=lambda x: x["profit_pct"], reverse=True)


# ── virtual portfolio ─────────────────────────────────────────────────────────

class VirtualPortfolio:
    def __init__(self, bankroll: float, slippage: float, fill_rate: float):
        self.cash          = bankroll
        self.initial       = bankroll
        self.slippage      = slippage    # fraction to reduce each leg's odds
        self.fill_rate     = fill_rate   # probability each leg actually fills
        self.open_bets:    list[VirtualBet] = []
        self.closed_bets:  list[VirtualBet] = []
        self.placed_ids:   set[str] = set()

    @property
    def total_staked(self) -> float:
        return sum(b.total_stake for b in self.open_bets if b.all_filled)

    @property
    def equity(self) -> float:
        return self.cash + self.total_staked

    def place(self, arb: dict) -> Optional[VirtualBet]:
        if arb["game_id"] in self.placed_ids:
            return None

        # Kelly sizing
        edge         = arb["profit_pct"] / 100
        kelly_stake  = min(KELLY_FRACTION * edge, MAX_STAKE_PCT) * self.cash
        total_stake  = round(kelly_stake, 2)

        if total_stake < 1.0 or total_stake > self.cash:
            return None

        legs = []
        any_unfilled = False
        for leg in arb["legs"]:
            filled = random.random() < self.fill_rate
            if not filled:
                any_unfilled = True
            effective_odds = round(leg["odds"] * (1 - self.slippage), 4)
            stake          = round(total_stake * leg["stake_pct"], 2)
            legs.append({
                "outcome": leg["outcome"],
                "odds":    effective_odds,
                "book":    leg["book"],
                "stake":   stake,
                "filled":  filled,
            })

        bet = VirtualBet(
            game_id    = arb["game_id"],
            match      = arb["match"],
            kickoff    = arb["kickoff"],
            placed_at  = datetime.now(timezone.utc),
            legs       = legs,
            total_stake= total_stake,
        )

        if any_unfilled:
            # partial fill — partially exposed; mark as resolved with a loss on unfilled legs
            # (worst case: filled legs lose, unfilled legs have no coverage)
            filled_stake = sum(l["stake"] for l in legs if l["filled"])
            bet.resolved   = True
            bet.won_return = 0.0   # assume worst case: filled legs lost
            bet.result     = "PARTIAL FILL — position closed at loss"
            self.cash     -= filled_stake
            self.closed_bets.append(bet)
        else:
            self.cash -= total_stake
            self.open_bets.append(bet)

        self.placed_ids.add(arb["game_id"])
        return bet

    def resolve(self, scores: dict[str, dict]) -> list[VirtualBet]:
        resolved = []
        remaining = []
        for bet in self.open_bets:
            if bet.game_id not in scores:
                remaining.append(bet)
                continue
            result   = scores[bet.game_id]
            winner   = result["winner"]
            # find the leg that won
            win_leg  = next((l for l in bet.legs if l["outcome"] == winner), None)
            if win_leg:
                bet.won_return = round(win_leg["stake"] * win_leg["odds"], 2)
            else:
                bet.won_return = 0.0
            bet.resolved = True
            bet.result   = f"{winner}  ({result['score']})"
            self.cash   += bet.won_return
            self.closed_bets.append(bet)
            resolved.append(bet)

        self.open_bets = remaining
        return resolved

    def summary(self) -> dict:
        closed  = self.closed_bets
        wins    = [b for b in closed if not b.resolved or b.won_return > b.total_stake]
        partials = [b for b in closed if "PARTIAL" in (b.result or "")]
        total_staked  = sum(b.total_stake for b in closed)
        total_return  = sum(b.won_return  for b in closed)
        total_profit  = total_return - total_staked
        roi           = (total_profit / total_staked * 100) if total_staked else 0
        return {
            "equity":        round(self.equity, 2),
            "cash":          round(self.cash, 2),
            "open_bets":     len(self.open_bets),
            "closed_bets":   len(closed),
            "partial_fills": len(partials),
            "total_staked":  round(total_staked, 2),
            "total_return":  round(total_return, 2),
            "total_profit":  round(total_profit, 2),
            "roi_pct":       round(roi, 2),
            "pnl_vs_start":  round(self.equity - self.initial, 2),
        }


# ── dashboard printer ─────────────────────────────────────────────────────────

def print_dashboard(portfolio: VirtualPortfolio, poll: int) -> None:
    s = portfolio.summary()
    pnl_sign = "+" if s["pnl_vs_start"] >= 0 else ""
    print(f"\n{'─'*60}")
    print(f"  Poll #{poll}  |  {datetime.now(timezone.utc):%H:%M:%S UTC}")
    print(f"  Equity:        ${s['equity']:>10,.2f}   "
          f"({pnl_sign}${s['pnl_vs_start']:,.2f} vs start)")
    print(f"  Cash:          ${s['cash']:>10,.2f}")
    print(f"  Open bets:     {s['open_bets']}")
    print(f"  Resolved:      {s['closed_bets']}   "
          f"(partial fills: {s['partial_fills']})")
    if s["total_staked"] > 0:
        print(f"  Total staked:  ${s['total_staked']:>10,.2f}")
        print(f"  Total return:  ${s['total_return']:>10,.2f}")
        print(f"  Profit:        ${s['total_profit']:>+10,.2f}   ROI: {s['roi_pct']:+.2f}%")
    print(f"{'─'*60}")


def print_bet(bet: VirtualBet, tag: str = "PLACED") -> None:
    print(f"\n  [{tag}]  {bet.match}  (kickoff {bet.kickoff[:16].replace('T',' ')} UTC)")
    print(f"  {'Outcome':<25} {'Odds':>6}  {'Book':<22} {'Stake':>8}  {'Fill':>5}")
    for l in bet.legs:
        fill = "✓" if l["filled"] else "✗"
        print(f"  {l['outcome']:<25} {l['odds']:>6.2f}  {l['book']:<22} ${l['stake']:>6.2f}  {fill:>5}")
    print(f"  Total stake: ${bet.total_stake:.2f}")


def print_resolved(bet: VirtualBet) -> None:
    sign = "+" if bet.profit >= 0 else ""
    tag  = "WIN" if bet.profit > 0 else "LOSS"
    print(f"\n  [RESOLVED — {tag}]  {bet.match}")
    print(f"  Result:  {bet.result}")
    print(f"  Staked: ${bet.total_stake:.2f}   Return: ${bet.won_return:.2f}   "
          f"Profit: {sign}${bet.profit:.2f}")


# ── main simulation loop ──────────────────────────────────────────────────────

def run(iterations: Optional[int], slippage: float, fill_rate: float) -> None:
    portfolio = VirtualPortfolio(BANKROLL, slippage, fill_rate)

    print(f"\n{'='*60}")
    print(f"  VIRTUAL SIMULATION — ${BANKROLL:,.0f} bankroll")
    print(f"  Slippage: {slippage*100:.1f}%  |  Fill rate: {fill_rate*100:.0f}%")
    print(f"  Strategy: US books only (GTbets, BetOnline, FanDuel, etc.)")
    print(f"{'='*60}")

    poll = 0
    while iterations is None or poll < iterations:
        poll += 1
        print(f"\n[Poll #{poll}  {datetime.now(timezone.utc):%H:%M:%S}]  Fetching odds & scores...")

        arbs   = fetch_arbs()
        scores = fetch_scores()

        # resolve completed games
        resolved = portfolio.resolve(scores)
        for bet in resolved:
            print_resolved(bet)

        # place new arbs
        if not arbs:
            print("  No arbs detected this poll.")
        for arb in arbs:
            bet = portfolio.place(arb)
            if bet:
                tag = "PLACED" if bet.all_filled else "PARTIAL FILL"
                print_bet(bet, tag)
            else:
                if arb["game_id"] in portfolio.placed_ids:
                    print(f"  [SKIP]  {arb['match']} — already have a position")

        print_dashboard(portfolio, poll)

        if iterations is None or poll < iterations:
            time.sleep(POLL_INTERVAL)

    # final report
    print(f"\n{'='*60}")
    print("  FINAL REPORT")
    print(f"{'='*60}")
    s = portfolio.summary()
    print(f"  Starting bankroll: ${BANKROLL:,.2f}")
    print(f"  Final equity:      ${s['equity']:,.2f}  ({'+' if s['pnl_vs_start']>=0 else ''}${s['pnl_vs_start']:.2f})")
    print(f"  Total bets placed: {s['closed_bets'] + s['open_bets']}")
    print(f"  Resolved:          {s['closed_bets']}")
    print(f"  Still open:        {s['open_bets']}")
    print(f"  Total staked:      ${s['total_staked']:,.2f}")
    print(f"  Total returned:    ${s['total_return']:,.2f}")
    print(f"  Net profit:        ${s['total_profit']:+,.2f}")
    print(f"  ROI:               {s['roi_pct']:+.2f}%")

    if portfolio.closed_bets:
        print(f"\n  Bet log:")
        for b in portfolio.closed_bets:
            sign = "+" if b.profit >= 0 else ""
            print(f"    {b.match:<35} {sign}${b.profit:.2f}   {b.result or 'unresolved'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int,   default=None)
    parser.add_argument("--slippage",   type=float, default=0.0,
                        help="Odds reduction per leg to simulate execution slippage (e.g. 0.02 = 2%%)")
    parser.add_argument("--fill-rate",  type=float, default=1.0,
                        help="Probability each leg fills (e.g. 0.8 = 80%% fill rate)")
    args = parser.parse_args()
    run(args.iterations, args.slippage, args.fill_rate)
