import time
import logging
from typing import Any

import requests

API_BASE_URL = "https://v3.football.api-sports.io"

LEAGUE_NAMES = {
    39: "Premier League",
    61: "Ligue 1",
    71: "Brasileirao Serie A",
    78: "Bundesliga",
    135: "Serie A",
    140: "La Liga",
}


class FootballApiError(Exception):
    pass


class FootballApiRateLimitError(FootballApiError):
    pass


class FootballApiClient:
    def __init__(self, api_key: str, host: str, request_delay_seconds: float = 0.0) -> None:
        self.session = requests.Session()
        self.request_delay_seconds = request_delay_seconds
        self.session.headers.update(
            {
                "x-rapidapi-key": api_key,
                "x-rapidapi-host": host,
            }
        )

    def get(self, path: str, params: dict[str, Any]) -> Any:
        response = self.session.get(
            f"{API_BASE_URL}{path}",
            params=params,
            timeout=30,
        )
        if self.request_delay_seconds > 0:
            time.sleep(self.request_delay_seconds)

        if response.status_code == 429:
            raise FootballApiRateLimitError("Limite de requisicoes da API-Football atingido.")

        response.raise_for_status()
        payload = response.json()

        errors = payload.get("errors", {})
        if errors:
            error_msg = str(errors)
            if "suspended" in error_msg.lower() or "access" in error_msg.lower():
                raise FootballApiError(f"Erro de acesso à API-Football: {error_msg}")

        return payload.get("response", [])

    def get_daily_fixtures(
        self,
        league_ids: list[int],
        target_date: str,
        timezone: str,
    ) -> list[dict[str, Any]]:
        response = self.get_fixtures_for_date(target_date, timezone)
        allowed_leagues = set(league_ids)
        return [
            fixture
            for fixture in response
            if fixture.get("league", {}).get("id") in allowed_leagues
        ]

    def get_fixtures_for_date(self, target_date: str, timezone: str) -> list[dict[str, Any]]:
        response = self.get("/fixtures", {"date": target_date, "timezone": timezone})
        return response if isinstance(response, list) else []

    def get_team_statistics(self, team_id: int, league_id: int, season: int) -> dict[str, Any]:
        response = self.get(
            "/teams/statistics",
            {"team": team_id, "league": league_id, "season": season},
        )
        return response if isinstance(response, dict) else {}

    def get_fixture_odds(self, fixture_id: int) -> list[dict[str, Any]]:
        response = self.get("/odds", {"fixture": fixture_id})
        return response if isinstance(response, list) else []

    def get_fixture_lineups(self, fixture_id: int) -> list[dict[str, Any]]:
        response = self.get("/fixtures/lineups", {"fixture": fixture_id})
        return response if isinstance(response, list) else []


def parse_average(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def serialize_form(value: str | None) -> str:
    if not value:
        return "N/D"
    return value[-5:]


def unavailable_technical_metrics() -> dict[str, str]:
    return {
        "xg": "Informacao nao disponivel no momento.",
        "possession": "Informacao nao disponivel no momento.",
        "shots": "Informacao nao disponivel no momento.",
    }


def pick_bookmaker(
    odds_payload: list[dict[str, Any]],
    preferred_name: str,
) -> dict[str, Any] | None:
    bookmakers: list[dict[str, Any]] = []
    for fixture_odds in odds_payload:
        bookmakers.extend(fixture_odds.get("bookmakers", []))
    if not bookmakers:
        return None
    for bookmaker in bookmakers:
        if bookmaker.get("name", "").lower() == preferred_name.lower():
            return bookmaker
    return bookmakers[0]


def extract_market_odds(bookmaker: dict[str, Any] | None) -> dict[str, Any]:
    if not bookmaker:
        return {}

    extracted: dict[str, Any] = {}
    for bet in bookmaker.get("bets", []):
        name = bet.get("name", "").lower()
        values = bet.get("values", [])

        if "match winner" in name or "1x2" in name:
            extracted["match_winner"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }
        elif "goals over/under" in name or "over/under" in name:
            extracted["over_under"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }
        elif "both teams score" in name:
            extracted["both_teams_score"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }
        elif "corner" in name:
            extracted["corners"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }

    return extracted


def normalize_lineup_player(item: dict[str, Any]) -> dict[str, Any]:
    player = item.get("player", {})
    return {
        "name": player.get("name"),
        "number": player.get("number"),
        "position": player.get("pos"),
    }


def normalize_fixture_lineups(lineups_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lineups: list[dict[str, Any]] = []

    for lineup in lineups_payload:
        team = lineup.get("team", {})
        coach = lineup.get("coach", {})
        lineups.append(
            {
                "team": team.get("name"),
                "formation": lineup.get("formation"),
                "coach": coach.get("name"),
                "start_xi": [
                    normalize_lineup_player(item)
                    for item in lineup.get("startXI", [])
                ],
                "substitutes": [
                    normalize_lineup_player(item)
                    for item in lineup.get("substitutes", [])
                ],
            }
        )

    return lineups


def normalize_team_stats(team_name: str, stats: dict[str, Any], side: str) -> dict[str, Any]:
    goals_for = stats.get("goals", {}).get("for", {}).get("average", {})
    goals_against = stats.get("goals", {}).get("against", {}).get("average", {})
    clean_sheets = stats.get("clean_sheet", {})
    failed_to_score = stats.get("failed_to_score", {})

    return {
        "name": team_name,
        "avg_goals_scored": parse_average(goals_for.get(side)),
        "avg_goals_conceded": parse_average(goals_against.get(side)),
        "clean_sheets": clean_sheets.get(side),
        "failed_to_score": failed_to_score.get(side),
        "last_5_form": serialize_form(stats.get("form")),
    }


def simplify_fixture(
    fixture: dict[str, Any],
    home_stats: dict[str, Any],
    away_stats: dict[str, Any],
    odds_payload: list[dict[str, Any]],
    bookmaker_name: str,
) -> dict[str, Any]:
    league = fixture.get("league", {})
    teams = fixture.get("teams", {})
    fixture_info = fixture.get("fixture", {})
    bookmaker = pick_bookmaker(odds_payload, bookmaker_name)

    return {
        "fixture_id": fixture_info.get("id"),
        "kickoff": fixture_info.get("date"),
        "league": league.get("name") or LEAGUE_NAMES.get(league.get("id"), "Liga"),
        "country": league.get("country"),
        "season": league.get("season"),
        "home_team": normalize_team_stats(
            teams.get("home", {}).get("name", "Mandante"),
            home_stats,
            "home",
        ),
        "away_team": normalize_team_stats(
            teams.get("away", {}).get("name", "Visitante"),
            away_stats,
            "away",
        ),
        "technical_metrics": unavailable_technical_metrics(),
        "odds": {
            "bookmaker": bookmaker.get("name") if bookmaker else None,
            **extract_market_odds(bookmaker),
        },
    }


def build_analysis_payload(
    api_client: FootballApiClient,
    league_ids: list[int],
    target_date: str,
    timezone: str,
    bookmaker_name: str,
    max_fixtures: int,
    request_delay_seconds: float,
) -> list[dict[str, Any]]:
    fixtures = api_client.get_daily_fixtures(
        league_ids=league_ids,
        target_date=target_date,
        timezone=timezone,
    )

    if not fixtures:
        return []

    simplified_fixtures: list[dict[str, Any]] = []

    for fixture in fixtures[:max_fixtures]:
        league = fixture.get("league", {})
        teams = fixture.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})

        try:
            home_stats = api_client.get_team_statistics(
                team_id=home.get("id"),
                league_id=league.get("id"),
                season=league.get("season"),
            )
            away_stats = api_client.get_team_statistics(
                team_id=away.get("id"),
                league_id=league.get("id"),
                season=league.get("season"),
            )
            odds_payload = api_client.get_fixture_odds(fixture.get("fixture", {}).get("id"))
        except FootballApiRateLimitError:
            if simplified_fixtures:
                logging.warning(
                    "Cota da API esgotada apos %s partidas. Analise parcial.",
                    len(simplified_fixtures),
                )
                break
            raise

        simplified_fixtures.append(
            simplify_fixture(
                fixture=fixture,
                home_stats=home_stats,
                away_stats=away_stats,
                odds_payload=odds_payload,
                bookmaker_name=bookmaker_name,
            )
        )

    return simplified_fixtures
