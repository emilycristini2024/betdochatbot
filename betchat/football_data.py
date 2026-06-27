import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .football_api import unavailable_technical_metrics

FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"


class FootballDataError(Exception):
    pass


class FootballDataClient:
    def __init__(self, api_key: str, timezone_name: str) -> None:
        self.timezone_name = timezone_name
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Auth-Token": api_key,
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(
            f"{FOOTBALL_DATA_BASE_URL}{path}",
            params=params or {},
            timeout=30,
        )

        if response.status_code == 429:
            raise FootballDataError("Limite de requisicoes da football-data.org atingido.")
        if response.status_code in {401, 403}:
            raise FootballDataError("Token da football-data.org invalido ou sem permissao.")

        response.raise_for_status()
        return response.json()

    def get_matches_for_date(self, target_date: str) -> list[dict[str, Any]]:
        payload = self.get(
            "/matches",
            {
                "dateFrom": target_date,
                "dateTo": target_date,
            },
        )
        matches = payload.get("matches") or []
        if not isinstance(matches, list):
            return []
        normalized_matches = [self.normalize_match(match) for match in matches]
        return [
            match
            for match in normalized_matches
            if self.match_matches_local_date(match, target_date)
        ]

    def get_match(self, match_id: int) -> dict[str, Any] | None:
        payload = self.get(f"/matches/{match_id}")
        if not isinstance(payload, dict):
            return None
        match = payload.get("match") or payload
        if not isinstance(match, dict):
            return None
        return self.normalize_match(match)

    def normalize_match(self, match: dict[str, Any]) -> dict[str, Any]:
        competition = match.get("competition") or {}
        area = match.get("area") or {}
        home_team = match.get("homeTeam") or {}
        away_team = match.get("awayTeam") or {}
        score = match.get("score") or {}
        normalized = {
            "football_data_match_id": match.get("id"),
            "kickoff": self.convert_utc_to_local(match.get("utcDate")),
            "league": competition.get("name") or "Liga",
            "competition_code": competition.get("code"),
            "country": area.get("name"),
            "stage": match.get("stage"),
            "matchday": match.get("matchday"),
            "status": match.get("status"),
            "home": home_team.get("name") or home_team.get("shortName"),
            "away": away_team.get("name") or away_team.get("shortName"),
            "score": {
                "winner": score.get("winner"),
                "full_time": score.get("fullTime"),
                "half_time": score.get("halfTime"),
            },
            "technical_metrics": unavailable_technical_metrics(),
            "source": "football_data",
        }

        official_lineups = self.extract_lineups(home_team, away_team)
        if official_lineups:
            normalized["official_lineups"] = official_lineups

        return normalized

    def convert_utc_to_local(self, utc_date: str | None) -> str:
        if not utc_date:
            return ""

        try:
            dt_utc = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        except ValueError:
            return utc_date

        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=UTC)

        try:
            target_timezone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            fallback_offsets = {"America/Sao_Paulo": -3, "UTC": 0}
            offset_hours = fallback_offsets.get(self.timezone_name, 0)
            logging.warning(
                "Timezone %s indisponivel para football-data.org. Usando UTC%+d.",
                self.timezone_name,
                offset_hours,
            )
            target_timezone = timezone(timedelta(hours=offset_hours))

        dt_local = dt_utc.astimezone(target_timezone)
        suffix = "BRT" if self.timezone_name == "America/Sao_Paulo" else self.timezone_name
        return dt_local.strftime(f"%Y-%m-%d %H:%M {suffix}")

    def extract_lineups(
        self,
        home_team: dict[str, Any],
        away_team: dict[str, Any],
    ) -> list[dict[str, Any]]:
        home_lineup = self.normalize_team_lineup(home_team)
        away_lineup = self.normalize_team_lineup(away_team)
        if not home_lineup or not away_lineup:
            return []
        return [home_lineup, away_lineup]

    def normalize_team_lineup(self, team: dict[str, Any]) -> dict[str, Any] | None:
        starters = team.get("lineup") or []
        if not starters:
            return None

        coach = team.get("coach") or {}
        return {
            "team": team.get("name") or team.get("shortName"),
            "formation": team.get("formation"),
            "coach": coach.get("name"),
            "start_xi": [self.normalize_player(player) for player in starters],
            "substitutes": [
                self.normalize_player(player)
                for player in team.get("bench", [])
            ],
        }

    def normalize_player(self, player: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": player.get("name"),
            "number": player.get("shirtNumber"),
            "position": player.get("position"),
        }

    def match_matches_local_date(self, match: dict[str, Any], target_date: str) -> bool:
        kickoff = str(match.get("kickoff") or "")
        if not kickoff:
            return True
        return kickoff.startswith(target_date)
