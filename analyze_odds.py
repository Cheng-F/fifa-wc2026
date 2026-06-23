"""
Analysis of FIFA World Cup 2026 odds across bookmakers.

Metrics:
  - Consensus implied win probability per team (avg across bookmakers, h2h only)
  - Match competitiveness (how close the contest is)
  - Bookmaker overround (vig) per match
  - Bookmaker disagreement (spread of win probs across books)
"""

import importlib.util
import pandas as pd
from datetime import datetime, timezone

# odds-api.py has a hyphen so standard import doesn't work
_spec = importlib.util.spec_from_file_location("odds_api", "odds-api.py")
_mod  = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
get_odds             = _mod.get_odds
extract_implied_probs = _mod.extract_implied_probs

SPORT = "soccer_fifa_world_cup"


def build_df(games: list[dict]) -> pd.DataFrame:
    records = extract_implied_probs(games, odds_format="decimal")
    return pd.DataFrame(records)


def consensus_probs(df: pd.DataFrame) -> pd.DataFrame:
    """Average implied prob per (game, outcome) across all bookmakers, then normalize."""
    avg = (
        df[df["bookmaker"] != "betfair_ex"]  # exclude exchange lay prices
        .groupby(["game_id", "commence_time", "home_team", "away_team", "outcome"])["implied_prob"]
        .mean()
        .reset_index()
    )

    # normalize so home + draw + away sum to 1 (removes overround)
    totals = avg.groupby("game_id")["implied_prob"].sum().rename("total")
    avg = avg.join(totals, on="game_id")
    avg["prob"] = (avg["implied_prob"] / avg["total"]).round(4)

    return avg[["game_id", "commence_time", "home_team", "away_team", "outcome", "prob"]]


def match_summary(df: pd.DataFrame) -> pd.DataFrame:
    cp = consensus_probs(df)

    rows = []
    for game_id, grp in cp.groupby("game_id"):
        row = {
            "kickoff": grp["commence_time"].iloc[0],
            "home":    grp["home_team"].iloc[0],
            "away":    grp["away_team"].iloc[0],
        }
        for _, r in grp.iterrows():
            out = r["outcome"]
            if out == row["home"]:
                row["home_win%"] = round(r["prob"] * 100, 1)
            elif out == row["away"]:
                row["away_win%"] = round(r["prob"] * 100, 1)
            else:
                row["draw%"] = round(r["prob"] * 100, 1)

        row["margin"] = abs(row.get("home_win%", 0) - row.get("away_win%", 0))
        rows.append(row)

    summary = pd.DataFrame(rows).sort_values("kickoff").reset_index(drop=True)
    summary["kickoff"] = pd.to_datetime(summary["kickoff"]).dt.strftime("%m-%d %H:%M UTC")
    return summary[["kickoff", "home", "away", "home_win%", "draw%", "away_win%", "margin"]]


def overround(df: pd.DataFrame) -> pd.DataFrame:
    """Bookmaker vig = sum of raw implied probs - 1. Higher = more juice."""
    vig = (
        df[df["bookmaker"] != "betfair_ex"]
        .groupby(["game_id", "home_team", "away_team", "bookmaker"])["implied_prob"]
        .sum()
        .reset_index()
        .rename(columns={"implied_prob": "overround"})
    )
    vig["vig_%"] = ((vig["overround"] - 1) * 100).round(2)

    by_book = (
        vig.groupby("bookmaker")["vig_%"]
        .mean()
        .sort_values()
        .reset_index()
        .rename(columns={"vig_%": "avg_vig_%"})
    )
    by_book["avg_vig_%"] = by_book["avg_vig_%"].round(2)
    return by_book


def bookmaker_disagreement(df: pd.DataFrame) -> pd.DataFrame:
    """Std dev of win prob for the favourite across bookmakers — measures market uncertainty."""
    fav_probs = (
        df[df["bookmaker"] != "betfair_ex"]
        .sort_values("implied_prob", ascending=False)
        .groupby(["game_id", "home_team", "away_team", "outcome"])["implied_prob"]
        .std()
        .reset_index()
        .rename(columns={"implied_prob": "std"})
    )
    # keep only the favourite outcome per game
    idx = fav_probs.groupby("game_id")["std"].idxmax()
    top = fav_probs.loc[idx].copy()
    top["std"] = top["std"].round(4)
    top["match"] = top["home_team"] + " vs " + top["away_team"]
    return top[["match", "outcome", "std"]].sort_values("std", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    games = get_odds(sport=SPORT, regions="us,eu", markets="h2h", odds_format="decimal")
    df    = build_df(games)

    print("\n" + "="*70)
    print("MATCH SUMMARY — Consensus Win Probabilities (normalized, no vig)")
    print("="*70)
    summary = match_summary(df)
    print(summary.to_string(index=False))

    print("\n" + "="*70)
    print("MOST ONE-SIDED MATCHES  (largest gap between win probabilities)")
    print("="*70)
    print(summary.nlargest(5, "margin")[["home", "away", "home_win%", "draw%", "away_win%", "margin"]].to_string(index=False))

    print("\n" + "="*70)
    print("MOST COMPETITIVE MATCHES  (smallest gap)")
    print("="*70)
    print(summary.nsmallest(5, "margin")[["home", "away", "home_win%", "draw%", "away_win%", "margin"]].to_string(index=False))

    print("\n" + "="*70)
    print("BOOKMAKER VIG  (avg overround across all matches — lower = better value)")
    print("="*70)
    print(overround(df).to_string(index=False))

    print("\n" + "="*70)
    print("BOOKMAKER DISAGREEMENT  (std dev of favourite's implied prob across books)")
    print("="*70)
    print(bookmaker_disagreement(df).head(10).to_string(index=False))
