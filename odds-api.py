"""
Fetch upcoming FIFA World Cup 2026 events and odds from The Odds API v4.
Docs: https://the-odds-api.com/liveapi/guides/v4/
"""

import yaml
import requests
from datetime import datetime

with open("odds-api-key.yml") as f:
    API_KEY = yaml.safe_load(f)["api_key"]
BASE_URL = "https://api.the-odds-api.com/v4"

# FIFA World Cup 2026 sport key - check /sports for the exact key
SPORT = "soccer_fifa_world_cup"


def get_sports(all_sports: bool = False) -> list[dict]:
    """
    Step 1: List all available (in-season) sports.
    Does NOT count against usage quota.
    """
    params = {"apiKey": API_KEY}
    if all_sports:
        params["all"] = "true"

    response = requests.get(f"{BASE_URL}/sports", params=params)
    response.raise_for_status()

    sports = response.json()
    print(f"Available sports ({len(sports)} total):")
    for s in sports:
        if "soccer" in s["key"].lower() or "fifa" in s["key"].lower():
            print(f"  {s['key']:50s} | {s['title']}")

    return sports


def get_events(sport: str) -> list[dict]:
    """
    Step 2: Get upcoming events (no odds, no quota cost).
    Use this to browse what matches are available.
    """
    params = {"apiKey": API_KEY}

    response = requests.get(f"{BASE_URL}/sports/{sport}/events", params=params)
    response.raise_for_status()

    # Log quota usage from response headers
    print(f"\nQuota remaining: {response.headers.get('x-requests-remaining')}")
    print(f"Quota used:      {response.headers.get('x-requests-used')}")

    events = response.json()
    print(f"\nUpcoming events for {sport} ({len(events)} total):")
    for e in events:
        commence = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        print(f"  {e['id']} | {e['home_team']} vs {e['away_team']} | {commence:%Y-%m-%d %H:%M UTC}")

    return events


def get_odds(
    sport: str,
    regions: str = "us,eu",
    markets: str = "h2h",
    odds_format: str = "decimal",
) -> list[dict]:
    """
    Step 3: Fetch upcoming events with bookmaker odds.
    Quota cost = number of markets x number of regions.
    e.g. h2h only, us+eu = 1 market x 2 regions = 2 credits

    Args:
        sport:       Sport key from /sports endpoint. Use 'upcoming' for next 8 games across all sports.
        regions:     Comma-separated: us, us2, uk, eu, au
        markets:     Comma-separated: h2h (moneyline), spreads, totals, outrights
        odds_format: 'decimal' or 'american'
    """
    params = {
        "apiKey": API_KEY,
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }

    response = requests.get(f"{BASE_URL}/sports/{sport}/odds", params=params)
    response.raise_for_status()

    # Log quota usage
    print(f"\nQuota remaining: {response.headers.get('x-requests-remaining')}")
    print(f"Quota used:      {response.headers.get('x-requests-used')}")
    print(f"Cost of this call: {response.headers.get('x-requests-last')}")

    games = response.json()
    print(f"\nFetched {len(games)} games with odds:\n")

    for game in games:
        commence = datetime.fromisoformat(game["commence_time"].replace("Z", "+00:00"))
        print(f"{'='*60}")
        print(f"Match:   {game['home_team']} vs {game['away_team']}")
        print(f"Kickoff: {commence:%Y-%m-%d %H:%M UTC}")
        print(f"ID:      {game['id']}")
        print(f"Bookmakers ({len(game['bookmakers'])}):")

        for bm in game["bookmakers"]:
            print(f"\n  [{bm['title']}]  (updated: {bm['last_update']})")
            for market in bm["markets"]:
                print(f"    Market: {market['key']}")
                for outcome in market["outcomes"]:
                    point = f"  point={outcome.get('point', '')}" if "point" in outcome else ""
                    print(f"      {outcome['name']:25s}  {outcome['price']}{point}")

    return games


def american_to_implied_prob(american_odds: int) -> float:
    """
    Convert American odds to implied probability.
    Used to measure 'expected' team strength as a baseline
    for controlling confounders in weather analysis.

    Examples:
        -150 → 0.60 (60% implied win probability)
        +200 → 0.33 (33% implied win probability)
    """
    if american_odds < 0:
        return (-american_odds) / (-american_odds + 100)
    else:
        return 100 / (american_odds + 100)


def decimal_to_implied_prob(decimal_odds: float) -> float:
    """
    Convert decimal odds to implied probability.
    Examples:
        1.67 → 0.60 (60%)
        3.00 → 0.33 (33%)
    """
    return 1 / decimal_odds


def extract_implied_probs(games: list[dict], odds_format: str = "decimal") -> list[dict]:
    """
    Flatten games + bookmaker odds into a list of records with implied probabilities.
    Useful for loading into BigQuery or Pandas for weather correlation analysis.

    Returns list of dicts with:
        game_id, sport_key, commence_time, home_team, away_team,
        bookmaker, outcome_name, odds, implied_prob
    """
    records = []
    for game in games:
        for bm in game["bookmakers"]:
            for market in bm["markets"]:
                if market["key"] != "h2h":
                    continue
                for outcome in market["outcomes"]:
                    price = outcome["price"]
                    if odds_format == "american":
                        prob = american_to_implied_prob(price)
                    else:
                        prob = decimal_to_implied_prob(price)

                    records.append({
                        "game_id":       game["id"],
                        "sport_key":     game["sport_key"],
                        "commence_time": game["commence_time"],
                        "home_team":     game["home_team"],
                        "away_team":     game["away_team"],
                        "bookmaker":     bm["key"],
                        "bookmaker_name": bm["title"],
                        "outcome":       outcome["name"],
                        "odds":          price,
                        "implied_prob":  round(prob, 4),
                    })

    return records


if __name__ == "__main__":
    # Step 1: Discover sport key for FIFA World Cup 2026
    # Uncomment to find exact key once you have an API key
    # get_sports(all_sports=True)

    # Step 2: List upcoming events (free, no quota cost)
    # events = get_events(SPORT)

    # Step 3: Fetch events + h2h odds from US and EU bookmakers
    # Quota cost: 1 market x 2 regions = 2 credits
    games = get_odds(
        sport=SPORT,
        regions="us,eu",
        markets="h2h",
        odds_format="decimal",
    )

    # Step 4: Flatten to records with implied probabilities
    # for loading into BigQuery / Pandas
    records = extract_implied_probs(games, odds_format="decimal")
    print(f"\n\nExtracted {len(records)} odds records with implied probabilities:")
    for r in records[:5]:  # preview first 5
        print(r)