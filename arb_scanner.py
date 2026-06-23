"""
Arbitrage scanner for FIFA World Cup 2026 h2h markets.

For each match, finds the best (highest) odds per outcome across all bookmakers.
If sum(1 / best_odds) < 1, a risk-free profit exists regardless of result.

Profit % = (1 / sum_implied - 1) * 100
Stake allocation: stake_i = (total_stake / best_odds_i) / sum_implied
"""

import importlib.util
from datetime import datetime

_spec = importlib.util.spec_from_file_location("odds_api", "odds-api.py")
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
get_odds = _mod.get_odds

SPORT        = "soccer_fifa_world_cup"
TOTAL_STAKE  = 1000   # USD — change to see dollar amounts
EXCLUDE_BOOKS = {"betfair_ex", "matchbook"}   # exchange lay prices distort h2h
US_BOOKS = {
    "betmgm", "betonlineag", "betrivers", "betus", "bovada",
    "draftkings", "fanduel", "lowvig", "mybookieag",   # US-regulated / US-region
    "gtbets", "everygame", "betanysports",              # offshore, accept US players
}


def best_odds_per_outcome(game: dict) -> dict[str, tuple[float, str]]:
    """Return {outcome_name: (best_odds, bookmaker)} for h2h market, US books only."""
    best: dict[str, tuple[float, str]] = {}
    for bm in game["bookmakers"]:
        if bm["key"] not in US_BOOKS or bm["key"] in EXCLUDE_BOOKS:
            continue
        for market in bm["markets"]:
            if market["key"] != "h2h":
                continue
            for outcome in market["outcomes"]:
                name, price = outcome["name"], outcome["price"]
                if name not in best or price > best[name][0]:
                    best[name] = (price, bm["title"])
    return best


def scan(games: list[dict]) -> list[dict]:
    results = []
    for game in games:
        best = best_odds_per_outcome(game)
        if len(best) != 3:
            continue

        sum_implied = sum(1 / odds for odds, _ in best.values())
        profit_pct  = (1 / sum_implied - 1) * 100

        outcomes = []
        for name, (odds, book) in best.items():
            stake = (TOTAL_STAKE / odds) / sum_implied
            outcomes.append({
                "outcome": name,
                "odds":    odds,
                "book":    book,
                "stake":   round(stake, 2),
                "return":  round(stake * odds, 2),
            })

        results.append({
            "match":      f"{game['home_team']} vs {game['away_team']}",
            "kickoff":    game["commence_time"],
            "profit_pct": round(profit_pct, 3),
            "sum_implied": round(sum_implied, 4),
            "outcomes":   outcomes,
        })

    return sorted(results, key=lambda x: x["profit_pct"], reverse=True)


def print_report(results: list[dict], show_all: bool = False) -> None:
    arb   = [r for r in results if r["profit_pct"] > 0]
    close = [r for r in results if -1 < r["profit_pct"] <= 0]

    print(f"\n{'='*70}")
    print(f"ARBITRAGE SCAN — {len(results)} matches checked")
    print(f"  True arb (profit > 0%):   {len(arb)}")
    print(f"  Near-arb (within -1%):    {len(close)}")
    print(f"  Stake assumption:         ${TOTAL_STAKE:,.0f} total")
    print(f"{'='*70}")

    targets = arb if not show_all else arb + close
    if not targets:
        print("\nNo arbitrage opportunities found.")
        return

    for r in targets:
        kickoff = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
        tag = "ARB" if r["profit_pct"] > 0 else "NEAR"
        print(f"\n[{tag}]  {r['match']}")
        print(f"  Kickoff:    {kickoff:%Y-%m-%d %H:%M UTC}")
        print(f"  Profit:     {r['profit_pct']:+.3f}%  (sum implied = {r['sum_implied']:.4f})")
        if r["profit_pct"] > 0:
            print(f"  Guaranteed return on ${TOTAL_STAKE}: ${TOTAL_STAKE * (1 + r['profit_pct']/100):,.2f}")
        print(f"  {'Outcome':<25} {'Odds':>6}  {'Book':<25} {'Stake':>8}  {'Return':>8}")
        print(f"  {'-'*75}")
        for o in r["outcomes"]:
            print(f"  {o['outcome']:<25} {o['odds']:>6.2f}  {o['book']:<25} ${o['stake']:>7.2f}  ${o['return']:>7.2f}")


if __name__ == "__main__":
    games = get_odds(sport=SPORT, regions="us,eu", markets="h2h", odds_format="decimal")
    results = scan(games)
    print_report(results, show_all=True)
