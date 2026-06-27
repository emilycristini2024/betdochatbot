import logging
import re
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import requests

from .football_api import unavailable_technical_metrics

SPORTSDB_BASE_URL = "https://www.thesportsdb.com/api/v1/json/123"

SPORTSDB_LEAGUE_IDS = [
    4328, 4335, 4332, 4331, 4334, 4351, 4406, 4480,
    4344, 4346, 4329, 4399, 4350, 4397, 4356, 4337,
    4607, 4354, 4333, 4336, 4338, 4339, 4347, 4353,
    4355, 4358,
]


class SportsDbClient:
    """Cliente para a TheSportsDB API (gratuita, sem chave)."""

    def __init__(
        self,
        request_delay_seconds: float = 0.4,
        max_retries: int = 1,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "BetChat/1.0"})
        self.request_delay_seconds = request_delay_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._rate_limited = False

    def get_fixtures_by_date(self, date: str) -> list[dict[str, Any]]:
        return self._get_events(
            "eventsday.php",
            {"d": date, "s": "Soccer"},
            f"eventsday({date})",
        )

    def get_next_fixtures_by_league(self, league_id: int) -> list[dict[str, Any]]:
        return self._get_events(
            "eventsnextleague.php",
            {"id": league_id},
            f"eventsnextleague({league_id})",
        )

    def get_next_fixtures(self, limit: int | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for league_id in SPORTSDB_LEAGUE_IDS:
            if self._rate_limited or (limit is not None and len(result) >= limit):
                break
            league_events = self.get_next_fixtures_by_league(league_id)
            for event in league_events:
                event_id = str(event.get("idEvent", ""))
                if event_id in seen_ids:
                    continue
                normalized = self._normalize_event(event)
                result.append(normalized)
                if event_id:
                    seen_ids.add(event_id)
                if limit is not None and len(result) >= limit:
                    break

        logging.info("TheSportsDB proximos jogos combinado: %d jogos", len(result))
        return result

    def get_fixtures_for_date(self, target_date: str) -> list[dict[str, Any]]:
        """
        Estratégia combinada:
        1. Tenta eventsday diretamente
        2. Complementa com próximos jogos por liga, filtrando pela data
        """
        day_fixtures = self.get_fixtures_by_date(target_date)
        logging.info(
            "TheSportsDB eventsday retornou %d jogos para %s",
            len(day_fixtures), target_date,
        )

        result = []
        seen_ids: set[str] = set()

        for e in day_fixtures:
            if e.get("dateEvent", "") == target_date:
                normalized = self._normalize_event(e)
                if self._fixture_matches_local_date(normalized, target_date):
                    result.append(normalized)
                    if normalized.get("event_id"):
                        seen_ids.add(str(normalized["event_id"]))

        logging.info("Complementando com eventsnextleague para %s...", target_date)
        for league_id in SPORTSDB_LEAGUE_IDS:
            if self._rate_limited:
                break
            league_events = self.get_next_fixtures_by_league(league_id)
            for event in league_events:
                event_date = event.get("dateEvent", "")
                event_id = str(event.get("idEvent", ""))
                if event_date == target_date and event_id not in seen_ids:
                    normalized = self._normalize_event(event)
                    if self._fixture_matches_local_date(normalized, target_date):
                        result.append(normalized)
                        seen_ids.add(event_id)

        logging.info(
            "TheSportsDB total combinado: %d jogos para %s",
            len(result), target_date,
        )
        return result

    def _get_events(
        self,
        endpoint: str,
        params: dict[str, Any],
        log_label: str,
    ) -> list[dict[str, Any]]:
        if self._rate_limited:
            return []

        for attempt in range(self.max_retries + 1):
            try:
                self._wait_between_requests()
                response = self.session.get(
                    f"{SPORTSDB_BASE_URL}/{endpoint}",
                    params=params,
                    timeout=15,
                )
                if response.status_code == 429:
                    if attempt < self.max_retries:
                        delay = self._get_retry_delay(response)
                        logging.warning(
                            "TheSportsDB %s limitou chamadas; tentando de novo em %.1fs",
                            log_label,
                            delay,
                        )
                        time.sleep(delay)
                        continue
                    self._rate_limited = True
                    logging.warning(
                        "TheSportsDB %s retornou 429; pausando consultas nesta execucao",
                        log_label,
                    )
                    return []

                response.raise_for_status()
                data = response.json()
                return data.get("events") or []
            except Exception as exc:
                logging.warning("TheSportsDB %s falhou: %s", log_label, exc)
                return []

        return []

    def _wait_between_requests(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.request_delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _get_retry_delay(self, response: requests.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 5.0)
            except ValueError:
                pass
        return max(self.request_delay_seconds * 3, 1.0)

    def _fixture_matches_local_date(self, fixture: dict[str, Any], target_date: str) -> bool:
        kickoff = str(fixture.get("kickoff") or "")
        match = re.search(r"\d{4}-\d{2}-\d{2}", kickoff)
        if not match:
            return True
        return match.group(0) == target_date

    def _normalize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        time_utc = event.get("strTime") or event.get("strTimeLocal") or ""
        kickoff_brt = self._convert_time_to_brt(event.get("dateEvent", ""), time_utc)

        return {
            "event_id": event.get("idEvent"),
            "kickoff": kickoff_brt,
            "league": event.get("strLeague", ""),
            "country": event.get("strCountry", ""),
            "home": event.get("strHomeTeam", ""),
            "away": event.get("strAwayTeam", ""),
            "technical_metrics": unavailable_technical_metrics(),
            "source": "thesportsdb",
        }

    def _convert_time_to_brt(self, date_str: str, time_utc: str) -> str:
        if not date_str or not time_utc:
            return date_str or ""
        try:
            time_clean = time_utc.split("+")[0].strip()
            dt_utc = datetime.strptime(f"{date_str} {time_clean}", "%Y-%m-%d %H:%M:%S")
            dt_utc = dt_utc.replace(tzinfo=UTC)
            dt_brt = dt_utc.astimezone(timezone(timedelta(hours=-3)))
            return dt_brt.strftime("%Y-%m-%d %H:%M BRT")
        except Exception:
            return f"{date_str} {time_utc}"
