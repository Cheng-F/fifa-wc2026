"""
Real-time arbitrage trading strategy for FIFA World Cup 2026.

Architecture:
  PollingEngine   — rate-managed API loop (respects monthly quota)
  OddsTracker     — maintains odds history, detects stale/outlier prices
  ArbDetector     — finds opportunities, scores confidence
  PositionSizer   — Kelly criterion stake allocation
  PositionTracker — tracks open legs, detects broken arbs
  StrategyEngine  — orchestrates everything, prints actionable alerts

Execution note:
  This engine generates signals only — it does not place bets.
  Actual placement requires funded accounts and book-specific APIs.
"""

import importlib.util
import time
import statistics
import sys
import os
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ── load odds-api module (hyphenated filename) ────────────────────────────────
_spec = importlib.util.spec_from_file_location("odds_api", "odds-api.py")
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
get_odds = _mod.get_odds

# ── config ────────────────────────────────────────────────────────────────────
SPORT            = "soccer_fifa_world_cup"
BANKROLL         = 10_000          # USD — total capital across all books
KELLY_FRACTION   = 0.25            # fractional Kelly (reduces variance)
MIN_EDGE         = 0.003           # ignore arbs below 0.3% (transaction costs)
MAX_STAKE_PCT    = 0.05            # never risk more than 5% of bankroll per arb
STALE_SECONDS    = 120             # odds older than 2 min are considered stale
OUTLIER_Z        = 2.5             # reject odds > 2.5σ from market mean (likely errors)
POLL_INTERVAL    = 60              # seconds between API calls
MONTHLY_BUDGET   = 480             # API credits to budget per month (keep 20 spare)
CREDITS_PER_CALL = 2               # h2h + us + eu = 2 credits
# US-accessible books only: regulated state books + offshore books that accept US players
US_BOOKS = {
    "betmgm", "betonlineag", "betrivers", "betus", "bovada",
    "draftkings", "fanduel", "lowvig", "mybookieag",   # US-regulated / US-region
    "gtbets", "everygame", "betanysports",              # offshore, accept US players
}
EXCLUDE_BOOKS    = {"betfair_ex", "matchbook"}          # exchange lay prices distort h2h


# ── data structures ───────────────────────────────────────────────────────────

@dataclass
class OddsSnapshot:
    game_id:    str
    match:      str
    kickoff:    str
    outcome:    str
    odds:       float
    bookmaker:  str
    book_key:   str
    updated_at: datetime


@dataclass
class ArbOpportunity:
    game_id:    str
    match:      str
    kickoff:    str
    profit_pct: float
    sum_implied: float
    confidence: float           # 0–1: higher = more trustworthy
    legs: list[dict]            # [{outcome, odds, bookmaker, stake, return}]
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Position:
    arb:         ArbOpportunity
    legs_placed: list[str]      # outcomes confirmed placed
    legs_pending: list[str]     # outcomes not yet placed
    opened_at:   datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_complete(self) -> bool:
        return len(self.legs_pending) == 0

    @property
    def is_broken(self) -> bool:
        return len(self.legs_placed) > 0 and not self.is_complete


# ── odds tracker ─────────────────────────────────────────────────────────────

class OddsTracker:
    """Maintains a rolling history of odds per (game_id, outcome) to detect stale/outlier prices."""

    def __init__(self):
        # {(game_id, outcome): [OddsSnapshot, ...]}
        self._history: dict[tuple, list[OddsSnapshot]] = defaultdict(list)

    def update(self, snapshots: list[OddsSnapshot]) -> None:
        for s in snapshots:
            key = (s.game_id, s.outcome)
            self._history[key].append(s)
            # keep last 50 per (game, outcome, book) to bound memory
            if len(self._history[key]) > 50:
                self._history[key] = self._history[key][-50:]

    def is_stale(self, snap: OddsSnapshot) -> bool:
        age = (datetime.now(timezone.utc) - snap.updated_at).total_seconds()
        return age > STALE_SECONDS

    def is_outlier(self, snap: OddsSnapshot) -> bool:
        """True if odds deviate > OUTLIER_Z std devs from market mean for this outcome."""
        key = (snap.game_id, snap.outcome)
        recent = [s.odds for s in self._history[key] if not self.is_stale(s)]
        if len(recent) < 3:
            return False
        mean = statistics.mean(recent)
        stdev = statistics.stdev(recent)
        if stdev == 0:
            return False
        z = abs(snap.odds - mean) / stdev
        return z > OUTLIER_Z

    def market_mean_odds(self, game_id: str, outcome: str) -> Optional[float]:
        key = (game_id, outcome)
        recent = [s.odds for s in self._history[key] if not self.is_stale(s)]
        return statistics.mean(recent) if recent else None


# ── arb detector ──────────────────────────────────────────────────────────────

class ArbDetector:
    def __init__(self, tracker: OddsTracker):
        self.tracker = tracker

    def _best_per_outcome(self, snapshots: list[OddsSnapshot]) -> dict[str, OddsSnapshot]:
        best: dict[str, OddsSnapshot] = {}
        for s in snapshots:
            if s.book_key not in US_BOOKS or s.book_key in EXCLUDE_BOOKS:
                continue
            if self.tracker.is_stale(s):
                continue
            if s.outcome not in best or s.odds > best[s.outcome].odds:
                best[s.outcome] = s
        return best

    def _confidence(self, best: dict[str, OddsSnapshot], game_id: str) -> float:
        """
        Score 0–1 based on:
          - No stale legs (already filtered, so base score starts high)
          - No outlier odds (outlier leg reduces score)
          - Recency: all legs updated within 30s of each other
          - Margin: larger profit = more genuine (not noise)
        """
        score = 1.0

        for outcome, snap in best.items():
            if self.tracker.is_outlier(snap):
                score -= 0.3   # outlier odds are often data errors

        timestamps = [s.updated_at.timestamp() for s in best.values()]
        spread_s = max(timestamps) - min(timestamps)
        if spread_s > 60:
            score -= 0.2       # books updated > 1 min apart — one may be stale

        return max(0.0, min(1.0, round(score, 2)))

    def scan(self, all_snapshots: list[OddsSnapshot]) -> list[ArbOpportunity]:
        by_game: dict[str, list[OddsSnapshot]] = defaultdict(list)
        for s in all_snapshots:
            by_game[s.game_id].append(s)

        opportunities = []
        for game_id, snaps in by_game.items():
            best = self._best_per_outcome(snaps)
            if len(best) != 3:
                continue

            sum_implied = sum(1 / s.odds for s in best.values())
            profit_pct  = (1 / sum_implied - 1) * 100

            if profit_pct < MIN_EDGE * 100:
                continue

            confidence = self._confidence(best, game_id)

            first = next(iter(best.values()))
            legs = []
            for outcome, snap in best.items():
                stake  = (1 / snap.odds) / sum_implied
                legs.append({
                    "outcome":   outcome,
                    "odds":      snap.odds,
                    "bookmaker": snap.bookmaker,
                    "book_key":  snap.book_key,
                    "stake_pct": round(stake, 4),
                    "updated_at": snap.updated_at.isoformat(),
                })

            opportunities.append(ArbOpportunity(
                game_id    = game_id,
                match      = first.match,
                kickoff    = first.kickoff,
                profit_pct = round(profit_pct, 3),
                sum_implied= round(sum_implied, 4),
                confidence = confidence,
                legs       = legs,
            ))

        return sorted(opportunities, key=lambda x: x.profit_pct, reverse=True)


# ── position sizer ────────────────────────────────────────────────────────────

class PositionSizer:
    """
    Fractional Kelly criterion for arb sizing.

    For a guaranteed arb (all legs cover), edge = profit_pct.
    Kelly fraction = edge / (1 - 1/sum_implied) * KELLY_FRACTION
    Capped at MAX_STAKE_PCT of bankroll.
    """
    def __init__(self, bankroll: float):
        self.bankroll = bankroll

    def total_stake(self, arb: ArbOpportunity) -> float:
        edge = arb.profit_pct / 100
        # for a risk-free arb the Kelly fraction simplifies; we use confidence as a discount
        raw_fraction = KELLY_FRACTION * edge * arb.confidence
        capped       = min(raw_fraction, MAX_STAKE_PCT)
        return round(self.bankroll * capped, 2)

    def leg_stakes(self, arb: ArbOpportunity) -> list[dict]:
        total = self.total_stake(arb)
        result = []
        for leg in arb.legs:
            stake  = round(total * leg["stake_pct"], 2)
            ret    = round(stake * leg["odds"], 2)
            result.append({**leg, "stake": stake, "expected_return": ret})
        return result


# ── position tracker ──────────────────────────────────────────────────────────

class PositionTracker:
    def __init__(self):
        self.open: dict[str, Position] = {}   # game_id → Position
        self.closed: list[Position]    = []

    def open_position(self, arb: ArbOpportunity) -> Position:
        outcomes = [leg["outcome"] for leg in arb.legs]
        pos = Position(arb=arb, legs_placed=[], legs_pending=outcomes)
        self.open[arb.game_id] = pos
        return pos

    def confirm_leg(self, game_id: str, outcome: str) -> None:
        pos = self.open.get(game_id)
        if pos and outcome in pos.legs_pending:
            pos.legs_pending.remove(outcome)
            pos.legs_placed.append(outcome)
            if pos.is_complete:
                self.closed.append(self.open.pop(game_id))

    def check_broken(self, current_opps: list[ArbOpportunity]) -> list[Position]:
        """Return positions where the arb no longer exists in live prices."""
        live_ids = {o.game_id for o in current_opps}
        broken   = [p for p in self.open.values() if p.game_id not in live_ids and p.is_broken]
        return broken


# ── polling engine ────────────────────────────────────────────────────────────

class PollingEngine:
    """
    Manages API quota and converts raw game data to OddsSnapshots.
    Budget: MONTHLY_BUDGET credits / CREDITS_PER_CALL = max calls per month.
    At POLL_INTERVAL seconds each, we self-throttle automatically.
    """
    def __init__(self):
        self.calls_made   = 0
        self.max_calls    = MONTHLY_BUDGET // CREDITS_PER_CALL
        self.quota_remaining: Optional[int] = None

    @property
    def budget_ok(self) -> bool:
        if self.quota_remaining is not None:
            return self.quota_remaining > CREDITS_PER_CALL
        return self.calls_made < self.max_calls

    def fetch(self) -> Optional[list]:
        if not self.budget_ok:
            print("[WARN] API budget exhausted — stopping.")
            return None

        try:
            # suppress verbose print output from get_odds
            _devnull = open(os.devnull, "w")
            sys.stdout, _real_stdout = _devnull, sys.stdout
            try:
                games = get_odds(sport=SPORT, regions="us,eu", markets="h2h", odds_format="decimal")
            finally:
                sys.stdout = _real_stdout
                _devnull.close()

            self.calls_made += 1
            snapshots = []
            for game in games:
                kickoff = game["commence_time"]
                # skip games that have already kicked off
                ko_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                if ko_dt < datetime.now(timezone.utc):
                    continue

                for bm in game["bookmakers"]:
                    updated = datetime.fromisoformat(
                        bm["last_update"].replace("Z", "+00:00")
                    )
                    for market in bm["markets"]:
                        if market["key"] != "h2h":
                            continue
                        for outcome in market["outcomes"]:
                            snapshots.append(OddsSnapshot(
                                game_id    = game["id"],
                                match      = f"{game['home_team']} vs {game['away_team']}",
                                kickoff    = kickoff,
                                outcome    = outcome["name"],
                                odds       = outcome["price"],
                                bookmaker  = bm["title"],
                                book_key   = bm["key"],
                                updated_at = updated,
                            ))
            return snapshots

        except Exception as e:
            print(f"[ERROR] API call failed: {e}")
            return []

    def update_quota(self, remaining: int) -> None:
        self.quota_remaining = remaining


# ── strategy engine ───────────────────────────────────────────────────────────

class StrategyEngine:
    def __init__(self, bankroll: float = BANKROLL):
        self.poller   = PollingEngine()
        self.tracker  = OddsTracker()
        self.detector = ArbDetector(self.tracker)
        self.sizer    = PositionSizer(bankroll)
        self.positions= PositionTracker()
        self.seen_arbs: set[str] = set()   # game_ids alerted this session

    def _print_alert(self, arb: ArbOpportunity, stakes: list[dict]) -> None:
        tag    = "🟢 ARB" if arb.confidence >= 0.7 else "🟡 WEAK ARB"
        total  = sum(s["stake"] for s in stakes)
        profit = sum(s["expected_return"] for s in stakes) / 3 - total

        print(f"\n{'='*70}")
        print(f"{tag}  {arb.match}")
        print(f"  Kickoff:    {arb.kickoff}")
        print(f"  Profit:     +{arb.profit_pct:.3f}%   confidence={arb.confidence:.2f}")
        print(f"  Total stake: ${total:,.2f}   Expected profit: ${profit:,.2f}")
        print(f"  {'Outcome':<25} {'Odds':>6}  {'Book':<22} {'Stake':>8}  {'Return':>8}")
        print(f"  {'-'*72}")
        for s in stakes:
            stale_warn = " ⚠ STALE" if (
                datetime.now(timezone.utc) -
                datetime.fromisoformat(s["updated_at"])
            ).total_seconds() > STALE_SECONDS else ""
            print(f"  {s['outcome']:<25} {s['odds']:>6.2f}  {s['bookmaker']:<22} "
                  f"${s['stake']:>7.2f}  ${s['expected_return']:>7.2f}{stale_warn}")
        print(f"  Detected at: {arb.detected_at.strftime('%H:%M:%S UTC')}")

    def _print_broken_warning(self, pos: Position) -> None:
        print(f"\n⚠  BROKEN ARB — {pos.arb.match}")
        print(f"   Placed:  {pos.legs_placed}")
        print(f"   Missing: {pos.legs_pending}")
        print(f"   Action:  Hedge remaining legs at market price to limit loss.")

    def run(self, max_iterations: Optional[int] = None) -> None:
        print(f"Strategy engine started — bankroll=${BANKROLL:,}  "
              f"min_edge={MIN_EDGE*100:.1f}%  poll={POLL_INTERVAL}s")
        print(f"API budget: {self.poller.max_calls} calls remaining this month\n")

        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            now = datetime.now(timezone.utc)
            print(f"[{now:%H:%M:%S}] Polling... ", end="", flush=True)

            snapshots = self.poller.fetch()
            if snapshots is None:
                break
            if not snapshots:
                print("no data.")
                time.sleep(POLL_INTERVAL)
                iteration += 1
                continue

            print(f"{len(snapshots)} odds snapshots received.")

            self.tracker.update(snapshots)
            opportunities = self.detector.scan(snapshots)

            # alert on new opportunities
            for arb in opportunities:
                if arb.game_id not in self.seen_arbs:
                    stakes = self.sizer.leg_stakes(arb)
                    self._print_alert(arb, stakes)
                    self.seen_arbs.add(arb.game_id)

            # check for broken positions
            broken = self.positions.check_broken(opportunities)
            for pos in broken:
                self._print_broken_warning(pos)

            # summary line
            if not opportunities:
                print("  No arb opportunities this tick.")
            else:
                ids  = {o.game_id for o in opportunities}
                new  = ids - (self.seen_arbs - ids)
                print(f"  {len(opportunities)} active arbs  |  "
                      f"{len(self.positions.open)} open positions")

            iteration += 1
            if max_iterations is None or iteration < max_iterations:
                time.sleep(POLL_INTERVAL)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--bankroll",   type=float, default=BANKROLL,
                        help="Total capital across all books (default: $10,000)")
    parser.add_argument("--iterations", type=int,   default=None,
                        help="Stop after N polls (default: run forever)")
    parser.add_argument("--once",       action="store_true",
                        help="Single poll then exit (same as --iterations 1)")
    args = parser.parse_args()

    engine = StrategyEngine(bankroll=args.bankroll)
    engine.run(max_iterations=1 if args.once else args.iterations)
