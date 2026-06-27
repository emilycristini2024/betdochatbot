import logging
import re
from datetime import datetime
from typing import Any

import requests

from .football_api import unavailable_technical_metrics

STATS_BOMB_BASE_URL = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"


class StatsBombOpenDataClient:
    """Cliente para os JSONs publicos do repositorio StatsBomb Open Data."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "BetChat/1.0"})

    def get_competitions(self) -> list[dict[str, Any]]:
        payload = self._get_json("competitions.json")
        return payload if isinstance(payload, list) else []

    def get_matches_for_date(
        self,
        target_date: str,
        limit: int = 15,
        max_competitions: int = 40,
    ) -> list[dict[str, Any]]:
        competitions = [
            competition
            for competition in self.get_competitions()
            if self._competition_could_include_date(competition, target_date)
        ]
        competitions = sorted(
            competitions,
            key=lambda item: str(item.get("match_available") or ""),
            reverse=True,
        )

        result: list[dict[str, Any]] = []
        seen_match_ids: set[str] = set()

        for competition in competitions[:max_competitions]:
            if len(result) >= limit:
                break
            for match in self.get_matches_for_competition(competition):
                if match.get("match_date") != target_date:
                    continue
                match_id = str(match.get("match_id") or "")
                if match_id in seen_match_ids:
                    continue
                result.append(self._normalize_match(match, competition))
                if match_id:
                    seen_match_ids.add(match_id)
                if len(result) >= limit:
                    break

        logging.info(
            "StatsBomb Open Data retornou %d jogos historicos para %s",
            len(result),
            target_date,
        )
        return result

    def get_matches_for_competition(self, competition: dict[str, Any]) -> list[dict[str, Any]]:
        competition_id = competition.get("competition_id")
        season_id = competition.get("season_id")
        if competition_id is None or season_id is None:
            return []

        payload = self._get_json(f"matches/{competition_id}/{season_id}.json")
        return payload if isinstance(payload, list) else []

    def get_dataset_summary(self) -> dict[str, Any]:
        competitions = self.get_competitions()
        latest_available = ""
        if competitions:
            latest_available = max(
                str(competition.get("match_available") or "")
                for competition in competitions
            )
        return {
            "competition_seasons": len(competitions),
            "latest_match_available": latest_available,
        }

    def _get_json(self, path: str) -> Any:
        try:
            response = self.session.get(f"{STATS_BOMB_BASE_URL}/{path}", timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logging.warning("StatsBomb Open Data falhou em %s: %s", path, exc)
            return []

    def _competition_could_include_date(
        self,
        competition: dict[str, Any],
        target_date: str,
    ) -> bool:
        target_year = self._extract_year(target_date)
        if target_year is None:
            return True

        season_years = [
            int(year)
            for year in re.findall(r"\d{4}", str(competition.get("season_name") or ""))
        ]
        if not season_years:
            return True
        return target_year in season_years

    def _extract_year(self, target_date: str) -> int | None:
        try:
            return datetime.strptime(target_date, "%Y-%m-%d").year
        except ValueError:
            return None

    def _normalize_match(
        self,
        match: dict[str, Any],
        competition: dict[str, Any],
    ) -> dict[str, Any]:
        kickoff = self._build_kickoff(match)
        return {
            "statsbomb_match_id": match.get("match_id"),
            "kickoff": kickoff,
            "league": competition.get("competition_name")
            or match.get("competition", {}).get("competition_name"),
            "country": competition.get("country_name"),
            "season": competition.get("season_name"),
            "home": (match.get("home_team") or {}).get("home_team_name"),
            "away": (match.get("away_team") or {}).get("away_team_name"),
            "score": {
                "home": match.get("home_score"),
                "away": match.get("away_score"),
            },
            "status": "historical_open_data",
            "technical_metrics": unavailable_technical_metrics(),
            "source": "statsbomb_open_data",
            "source_note": "StatsBomb Open Data e uma base historica; nao fornece agenda futura ao vivo.",
        }

    def _build_kickoff(self, match: dict[str, Any]) -> str:
        match_date = str(match.get("match_date") or "").strip()
        kick_off = str(match.get("kick_off") or "").strip()
        return f"{match_date} {kick_off}".strip()
