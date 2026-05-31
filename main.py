import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import requests
import schedule
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API_BASE_URL = "https://v3.football.api-sports.io"
SPORTSDB_BASE_URL = "https://www.thesportsdb.com/api/v1/json/123"

# IDs das ligas no TheSportsDB (usadas no fallback)
SPORTSDB_LEAGUE_IDS = [
    4328,  # Premier League
    4335,  # La Liga
    4332,  # Serie A (Itália)
    4331,  # Bundesliga
    4334,  # Ligue 1
    4351,  # Brasileirão Série A
    4406,  # Liga MX
    4480,  # Eredivisie
    4344,  # Primeira Liga (Portugal)
    4346,  # Championship (Inglaterra)
    4329,  # FA Cup
    4399,  # MLS (EUA)
    4350,  # Argentino Primera División
    4397,  # Copa Libertadores
    4480,  # Eredivisie
    4356,  # Ekstraklasa (Polônia)
    4337,  # Super Lig (Turquia)
    4480,  # Allsvenskan (Suécia) — id 4607
    4607,  # Allsvenskan (Suécia)
    4354,  # Eliteserien (Noruega)
    4333,  # Bundesliga 2 (Alemanha)
    4336,  # Segunda División (Espanha)
    4338,  # Serie B (Itália)
    4339,  # Ligue 2 (França)
    4347,  # Brasileirão Série B
    4353,  # Liga Colombiana
    4355,  # Liga Chilena
    4358,  # Liga Peruana
]

MORNING_REPORT_MEMORY: dict[str, str] = {}

DEFAULT_SYSTEM_PROMPT = """
Voce e um Trader Esportivo de elite e analista quantitativo. Sua missao e ler o JSON de partidas e estatisticas fornecido e selecionar as 10 melhores oportunidades de aposta do dia.

Foco principal nos mercados:
- Over/Under Gols (especialmente Over 2.5 e Over 1.5)
- Ambas as Equipes Marcam (BTTS - Sim/Nao)
- Escanteios (Over/Under total de escanteios quando disponivel)

Regras de Selecao:
- Valor: procure por odds entre 1.50 e 2.20 quando a probabilidade estatistica parecer dominante.
- Gols: se dois times tiverem medias ofensivas e defensivas favoraveis, priorize Over 2.5 ou Ambas Marcam.
- Escanteios: times com alto volume de ataque tendem a gerar mais escanteios.
- Favoritos extremos: prefira mercados alternativos de gols ou escanteios.
- Se nao houver pelo menos 3 evidencias objetivas para um mercado, use SEM ENTRADA.
- Se metade ou mais dos dados-chave estiver ausente, a confianca maxima e 5/10 e a stake maxima e 0,5 unidade.
- Confianca 7/10 exige pelo menos 4 evidencias convergentes. Confianca 8/10 exige forte base estatistica, odd adequada e nenhum alerta contrario.
- Nunca use 9/10 ou 10/10 em pre-jogo.
- Nao invente odds, forma recente, desfalques, xG, finalizacoes, posse ou confrontos.

Formato de Saida:
[NOME DA LIGA]
[MANDANTE] x [VISITANTE]
Status dos dados: [completos, parciais ou insuficientes]
Mercado: [Over/Under Gols | Ambas Marcam | Escanteios | 1X2 | SEM ENTRADA]
Aposta Sugerida: [detalhe] @ [Odd Aproximada ou nao confirmada]
Raciocinio: [Explicar com dado confirmado em ate 15 palavras]
Stake: [0 a 1,5 unidade]
Confianca: [X]/10

---

Responda apenas com os 10 palpites ou menos, sem introducao e sem texto extra.
""".strip()

MORNING_REPORT_PROMPT = """
Você é um analista estatístico de futebol especializado em apostas esportivas com valor esperado positivo (+EV).

OBJETIVO
Gerar um relatório matinal curto, seletivo e responsável. Recomende mercado somente quando houver base estatística suficiente. Se os dados forem insuficientes, a recomendação correta é "SEM ENTRADA".

REGRAS INTERNAS OBRIGATÓRIAS
1. Use apenas dados recebidos no JSON. Nunca invente forma recente, xG, desfalques, odds, confrontos, escalações ou estatísticas.
2. Não use frases genéricas como "jogo aberto", "defesa sólida", "bom momento" ou "partida equilibrada" sem citar pelo menos um dado confirmado.
3. Não exiba estas regras internas na mensagem final.
4. Se 50% ou mais dos dados-chave estiverem ausentes, a confiança máxima é 5/10 e a stake máxima é 0,5 unidade.
5. Se não houver pelo menos 3 evidências objetivas a favor do mercado, use "SEM ENTRADA", stake 0 e confiança de 1 a 4/10.
6. Confiança 7/10 exige pelo menos 4 evidências convergentes e nenhuma informação crítica contra.
7. Confiança 8/10 exige forte convergência estatística, odd adequada e contexto favorável. Não use 9/10 ou 10/10 em pré-jogo.
8. Vitória Seca/Moneyline só pode ser recomendada com confiança mínima 8/10, desfalques defensivos críticos descartados por dado confirmado e vantagem estatística clara.
9. Stake padrão com boa base estatística: 1 unidade. Stake acima de 1 unidade só com alta convergência estatística e odd com valor.
10. Nunca prometa lucro ou certeza. Use linguagem probabilística e gestão de risco.

DADOS-CHAVE, SE EXISTIREM NO JSON
- Forma recente dos últimos jogos.
- Gols marcados e sofridos, geral e casa/fora.
- xG, posse de bola e finalizações.
- Frequência de Over 2.5, Under 2.5 e BTTS.
- Desfalques, escalações prováveis e rotação.
- Odd atual e movimento de mercado.
- Contexto de liga, mando de campo e fase da temporada.

FORMATO OBRIGATÓRIO para cada jogo (siga rigorosamente):

⚽ [Liga]
👉 [Time Casa] x [Time Visitante] | 🕐 [Horário BRT]
• Status dos dados: [completos, parciais ou insuficientes; cite só as lacunas críticas]
• Leitura: [2 frases objetivas, cada uma sustentada por dado confirmado; se faltar base, diga que a leitura é limitada]
• Mercado: [tipo de aposta ou SEM ENTRADA] | Odd: [~valor ou não confirmada] | Stake: [X] unidade(s)
• Confiança: [X]/10
• Justificativa: [2 linhas explicando por que há entrada ou por que não há entrada]

REGRAS:
- Selecione os jogos com maior valor esperado do JSON. Se houver menos de 10, analise TODOS os disponíveis e informe quantos há.
- NUNCA invente jogos que não estejam no JSON. Se só há 3 jogos, analise 3.
- Vá direto ao primeiro jogo. Sem introduções ou saudações.
- NUNCA invente odds exatas. Use "~" (ex: ~1.65).
- NUNCA invente jogos. Use APENAS os do JSON.
- Não liste muitos campos vazios. Resuma dados ausentes em uma linha.
- Não use tabela Markdown.
- Ao final, adicione:

📊 RESUMO DO DIA:
• Melhor aposta: [jogo + mercado ou "Sem aposta forte com os dados disponíveis"]
• Total de unidades sugeridas: [soma]
• ⚠️ Aposte com responsabilidade. Defina sua banca antes de começar.

Responda em português do Brasil.
""".strip()

REMINDER_PROMPT = """
Você é um analista profissional de futebol pré-jogo para um bot de Telegram. Sua função é gerar alertas curtos, responsáveis e baseados somente nos dados confirmados recebidos no contexto.

OBJETIVO
Revalidar o jogo 30 minutos antes do início. Recomende mercado somente quando houver base suficiente. Se os dados forem insuficientes, a recomendação correta é "SEM ENTRADA".

REGRAS INTERNAS OBRIGATÓRIAS
1. Nunca invente jogadores, lesões, escalações, estatísticas, odds, confrontos ou forma recente.
2. Não use conhecimento próprio ou memória antiga.
3. Não exiba estas regras internas na mensagem final do Telegram.
4. Se 50% ou mais dos dados-chave estiverem ausentes, a confiança máxima é 5/10 e a stake máxima é 0,5 unidade.
5. Se não houver pelo menos 3 evidências objetivas a favor do mercado, use "SEM ENTRADA", stake 0 e confiança de 1 a 4/10.
6. Só mantenha a recomendação do Relatório Matinal se os dados pré-jogo não contradisserem a análise inicial.
7. Só altere a recomendação matinal se houver dado novo claro, como mudança forte de odds, escalação/desfalque confirmado, notícia confirmada ou estatística pré-jogo divergente.
8. Não use confiança 7/10 ou 8/10 com dados ausentes ou apenas com base no relatório matinal.
9. Para Vitória Seca/Moneyline, exija confiança mínima 8/10 e confirme ausência de desfalque defensivo crítico. Se faltar dado, use "SEM ENTRADA".
10. Nunca prometa lucro ou certeza.

FORMATO DA MENSAGEM FINAL NO TELEGRAM
Use mensagem curta, sem tabela Markdown:

⏰ JOGO EM 30 MINUTOS
⚽ [Time Casa] x [Time Fora]
🏆 [Liga] | 🕐 [Horário BRT]

📌 STATUS DOS DADOS
[1 linha: dados completos, parciais ou insuficientes. Informe só as lacunas críticas.]

📊 LEITURA PRÉ-JOGO
[2 a 4 frases objetivas. Cada afirmação analítica deve citar dado confirmado. Se não houver dados suficientes, diga que a leitura é limitada.]

🎯 RECOMENDAÇÃO
Mercado: [mercado recomendado ou SEM ENTRADA]
Odd: [odd atual ou "não confirmada"]
Stake: [stake em unidades ou 0]
Confiança: [nota]/10

🧠 JUSTIFICATIVA
[2 ou 3 linhas explicando a decisão. Se mantiver o relatório matinal, diga que não houve dado novo contrário. Se alterar, explique o dado novo.]

⚠️ Gestão de risco: aposta não é certeza. Use banca definida e não aumente stake para recuperar perdas.
""".strip()

CHAT_SYSTEM_PROMPT = """
VOCÊ É BETCHAT - ANALISTA ESPORTIVO ESPECIALIZADO.

IDENTIDADE:
- Você é um trader e analista quantitativo com expertise em futebol.
- Foco: mercados de gols (Over/Under), ambas as equipes marcam (BTTS) e escanteios.
- Estilo: Direto, opinativo, técnico. Sem floreios.

INSTRUÇÕES CRÍTICAS (OBRIGATÓRIAS):
1. SEMPRE responda em português do Brasil, clara e concisa.
2. SEMPRE analise jogos com foco em VALOR - qual mercado tem a melhor probabilidade.
3. Quando receber dados de jogos (JSON), analise CADA partida assim:
   ⚽ Time A x Time B — Liga
   📊 Gols: [Over/Under baseado em médias]
   🤝 BTTS: [Sim/Não com explicação]
   🎯 Mercado: [Over 2.5/Ambas Marcam/etc]
   💡 Por quê: [máx 15 palavras]
4. Quando NÃO houver dados de jogos, responda com análise do seu conhecimento.
5. NUNCA invente odds numéricas - use aproximações (Ex: "odds próximas de 1.80").
6. NUNCA recuse analisar futebol.
7. Seja opinativo: "Este jogo tem valor em Over 2.5 porque..." (não genérico).
8. Responda saudações com entusiasmo, mas sempre pronto para análises.
9. Para Vitória Seca/Moneyline, só recomende com confiança mínima 8/10 e sem desfalque defensivo crítico confirmado no favorito. Se houver dúvida, prefira Empate Anula ou Handicap Asiático.
10. Use xG, posse de bola e finalizações quando estiverem nos dados recebidos. Se não estiverem, diga que a informação não está disponível e não invente.

EXEMPLO DE RESPOSTA IDEAL:
⚽ Bayern x Frankfurt — Bundesliga
Gols: Over 2.5 (Bayern marca 2.3 em casa, Frankfurt sofre 2.1 - soma 4.4)
BTTS: Sim (Bayern ofensivo, Frankfurt sempre marca fora)
Mercado: Over 2.5 @ ~1.75
Confiança: 8/10

PADRÃO DE FORMATAÇÃO PARA VÁRIOS JOGOS:
[Jogo 1] ... Confiança: X/10
---
[Jogo 2] ... Confiança: X/10
""".strip()

CHAT_SYSTEM_PROMPT_SPORTSDB = """
VOCÊ É BETCHAT - ANALISTA ESPORTIVO ESPECIALIZADO.

REGRA ABSOLUTA — LEIA ANTES DE TUDO:
- Você receberá um JSON com a lista EXATA de jogos de uma data específica.
- ANALISE SOMENTE os jogos presentes nesse JSON. NENHUM outro.
- É PROIBIDO mencionar, inventar ou sugerir qualquer jogo que não esteja no JSON.
- A data dos jogos está indicada no JSON e na instrução. NÃO tente calcular datas.
- NÃO diga que não há jogos — se recebeu o JSON, há jogos. Analise-os.
- Ignorar essa regra é um erro crítico.

IDENTIDADE:
- Trader e analista quantitativo com expertise em futebol.
- Foco: Over/Under gols, BTTS (ambas marcam) e escanteios.
- Estilo: direto, opinativo, técnico.

FORMATO DE ANÁLISE (para cada jogo do JSON):
⚽ [home] x [away] — [league] | [kickoff]
📊 Gols: [Over/Under com justificativa]
🤝 BTTS: [Sim/Não + motivo]
🎯 Mercado: [recomendação]
💡 Por quê: [máx 15 palavras]
Confiança: [X]/10
---

AO FINAL:
🏆 TOP 3 DO DIA:
1. [melhor aposta]
2. [segunda melhor]
3. [terceira melhor]

RESTRIÇÕES:
- NUNCA invente odds numéricas. Use "~1.75", "próximo de 1.80".
- NUNCA adicione jogos além dos do JSON.
- NUNCA calcule ou assuma datas por conta própria.
- Para Vitória Seca/Moneyline, exija confiança mínima 8/10 e ausência de desfalque defensivo crítico confirmado. Se faltar dado, prefira Empate Anula ou Handicap Asiático.
- Use xG, posse de bola e finalizações quando o JSON trouxer esses campos. Se não trouxer, informe indisponibilidade.
- SEMPRE responda em português do Brasil.
""".strip()

LEAGUE_NAMES = {
    39: "Premier League",
    61: "Ligue 1",
    71: "Brasileirao Serie A",
    78: "Bundesliga",
    135: "Serie A",
    140: "La Liga",
}

# Palavras-chave que indicam que o usuário quer jogos do dia
FIXTURES_KEYWORDS = [
    "jogos", "partidas", "hoje", "amanhã", "amanha", "manhã", "manha",
    "tarde", "noite", "dia", "grade", "agenda", "programação", "programacao",
    "fixtures", "jogos de hoje", "o que tem hoje", "tem jogo",
    "próximo", "proximo", "próximos", "proximos", "próxima", "proxima",
    "semana", "fim de semana", "fds", "quando joga", "quando é o jogo",
    "apostas", "palpites", "tips", "dicas",
]


@dataclass
class Settings:
    telegram_token: str
    telegram_chat_id: str
    llm_provider: str
    llm_api_key: str
    llm_base_url: str
    rapidapi_key: str
    rapidapi_host: str
    llm_model: str
    timezone: str
    target_date: str
    bookmaker_name: str
    league_ids: list[int]
    max_fixtures: int
    request_delay_seconds: float
    bot_mode: str


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
            raise FootballApiRateLimitError(
                "Limite de requisicoes da API-Football atingido."
            )

        response.raise_for_status()
        payload = response.json()

        # Verifica se a conta está suspensa ou com erro de acesso
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
        response = self.get(
            "/fixtures",
            {
                "date": target_date,
                "timezone": timezone,
            },
        )
        allowed_leagues = set(league_ids)
        return [
            fixture
            for fixture in response
            if fixture.get("league", {}).get("id") in allowed_leagues
        ]

    def get_team_statistics(
        self,
        team_id: int,
        league_id: int,
        season: int,
    ) -> dict[str, Any]:
        response = self.get(
            "/teams/statistics",
            {
                "team": team_id,
                "league": league_id,
                "season": season,
            },
        )
        if isinstance(response, dict):
            return response
        return {}

    def get_fixture_odds(self, fixture_id: int) -> list[dict[str, Any]]:
        response = self.get("/odds", {"fixture": fixture_id})
        if isinstance(response, list):
            return response
        return []


class SportsDbClient:
    """Cliente para a TheSportsDB API (gratuita, sem chave)."""

    BASE_URL = SPORTSDB_BASE_URL

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "BetChat/1.0"})

    def get_fixtures_by_date(self, date: str) -> list[dict[str, Any]]:
        """Busca todos os jogos de futebol de uma data específica."""
        try:
            response = self.session.get(
                f"{self.BASE_URL}/eventsday.php",
                params={"d": date, "s": "Soccer"},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("events") or []
        except Exception as exc:
            logging.warning("TheSportsDB eventsday falhou: %s", exc)
            return []

    def get_next_fixtures_by_league(self, league_id: int) -> list[dict[str, Any]]:
        """Busca os próximos jogos de uma liga específica."""
        try:
            response = self.session.get(
                f"{self.BASE_URL}/eventsnextleague.php",
                params={"id": league_id},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("events") or []
        except Exception as exc:
            logging.warning("TheSportsDB eventsnextleague(%s) falhou: %s", league_id, exc)
            return []

    def get_fixtures_for_date(self, target_date: str) -> list[dict[str, Any]]:
        """
        Estratégia combinada:
        1. Tenta buscar jogos do dia diretamente via eventsday
        2. Complementa com próximos jogos por liga, filtrando ESTRITAMENTE pela data
        Jogos com data diferente de target_date são sempre descartados.
        """
        # Passo 1: jogos do dia
        day_fixtures = self.get_fixtures_by_date(target_date)
        logging.info("TheSportsDB eventsday retornou %d jogos para %s", len(day_fixtures), target_date)

        # Normaliza e filtra pela data (eventsday pode retornar datas erradas em edge cases)
        result = []
        seen_ids: set[str] = set()
        for e in day_fixtures:
            if e.get("dateEvent", "") == target_date:
                normalized = self._normalize_event(e)
                result.append(normalized)
                if normalized.get("event_id"):
                    seen_ids.add(str(normalized["event_id"]))

        # Passo 2: complementa com próximos jogos por liga, filtrando pela data
        logging.info("Complementando com eventsnextleague para %s...", target_date)
        for league_id in SPORTSDB_LEAGUE_IDS:
            league_events = self.get_next_fixtures_by_league(league_id)
            for event in league_events:
                event_date = event.get("dateEvent", "")
                event_id = str(event.get("idEvent", ""))
                # FILTRO ESTRITO: só aceita jogos da data exata solicitada
                if event_date == target_date and event_id not in seen_ids:
                    result.append(self._normalize_event(event))
                    seen_ids.add(event_id)

        logging.info("TheSportsDB total combinado: %d jogos para %s", len(result), target_date)
        return result

    def _normalize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Normaliza um evento da TheSportsDB para o formato interno."""
        # Horário: strTime está em UTC, converte para BRT (UTC-3)
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
        """Converte horário UTC para BRT (UTC-3) e retorna string formatada."""
        if not date_str or not time_utc:
            return date_str or ""
        try:
            # time_utc pode vir como "HH:MM:SS" ou "HH:MM:SS+00:00"
            time_clean = time_utc.split("+")[0].strip()
            dt_utc = datetime.strptime(f"{date_str} {time_clean}", "%Y-%m-%d %H:%M:%S")
            dt_utc = dt_utc.replace(tzinfo=UTC)
            dt_brt = dt_utc.astimezone(timezone(timedelta(hours=-3)))
            return dt_brt.strftime("%Y-%m-%d %H:%M BRT")
        except Exception:
            return f"{date_str} {time_utc}"


def load_settings() -> Settings:
    load_dotenv()

    tz = os.getenv("TIMEZONE", "America/Sao_Paulo")
    target_date = os.getenv("TARGET_DATE", "").strip()

    if not target_date:
        target_date = get_current_datetime(tz).strftime("%Y-%m-%d")

    league_ids = [
        int(lid.strip())
        for lid in os.getenv("LEAGUE_IDS", "39,140,135,78,61,71").split(",")
        if lid.strip()
    ]

    settings = Settings(
        telegram_token=os.getenv("TELEGRAM_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        llm_provider=os.getenv("LLM_PROVIDER", "groq").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").strip(),
        rapidapi_key=os.getenv("RAPIDAPI_KEY", "").strip(),
        rapidapi_host=os.getenv("RAPIDAPI_HOST", "v3.football.api-sports.io").strip(),
        llm_model=os.getenv("LLM_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct").strip(),
        timezone=tz,
        target_date=target_date,
        bookmaker_name=os.getenv("BOOKMAKER_NAME", "Bet365").strip(),
        league_ids=league_ids,
        max_fixtures=int(os.getenv("MAX_FIXTURES", "10")),
        request_delay_seconds=float(os.getenv("REQUEST_DELAY_SECONDS", "1.0")),
        bot_mode=os.getenv("BOT_MODE", "cron").strip().lower(),
    )

    validate_settings(settings)
    return settings


def validate_settings(settings: Settings) -> None:
    missing_fields = []

    if not settings.telegram_token:
        missing_fields.append("TELEGRAM_TOKEN")
    if not settings.llm_api_key:
        missing_fields.append("LLM_API_KEY")

    if settings.bot_mode == "cron":
        if not settings.telegram_chat_id:
            missing_fields.append("TELEGRAM_CHAT_ID")
        # No modo cron, API-Football é necessária para análise completa
        # mas não bloqueia se TheSportsDB for o fallback

    if missing_fields:
        fields = ", ".join(missing_fields)
        raise ValueError(f"Variaveis obrigatorias ausentes: {fields}")


def get_current_datetime(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        fallback_offsets = {"America/Sao_Paulo": -3, "UTC": 0}
        offset_hours = fallback_offsets.get(timezone_name, 0)
        fallback_timezone = timezone(timedelta(hours=offset_hours))
        logging.warning(
            "Timezone %s indisponivel. Usando fallback UTC%+d.",
            timezone_name,
            offset_hours,
        )
        return datetime.now(UTC).astimezone(fallback_timezone)


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
            over_under = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }
            extracted["over_under"] = over_under
        elif "both teams score" in name:
            extracted["both_teams_score"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }
        elif "corner" in name:
            extracted["corners"] = {
                item.get("value"): item.get("odd") for item in values if item.get("value")
            }

    return extracted


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
    settings: Settings,
) -> list[dict[str, Any]]:
    fixtures = api_client.get_daily_fixtures(
        league_ids=settings.league_ids,
        target_date=settings.target_date,
        timezone=settings.timezone,
    )

    if not fixtures:
        return []

    simplified_fixtures: list[dict[str, Any]] = []

    for fixture in fixtures[: settings.max_fixtures]:
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
                bookmaker_name=settings.bookmaker_name,
            )
        )

    return simplified_fixtures


def message_wants_fixtures(text: str) -> bool:
    """Detecta se a mensagem pede jogos do dia."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in FIXTURES_KEYWORDS)


def extract_date_from_message(text: str, current_date: str, timezone_name: str) -> str:
    """Extrai data da mensagem ou retorna hoje."""
    text_lower = text.lower()

    # Detecta "amanhã"
    if "amanhã" in text_lower or "amanha" in text_lower:
        dt = get_current_datetime(timezone_name) + timedelta(days=1)
        extracted = dt.strftime("%Y-%m-%d")
        logging.info("Detectado 'amanhã': %s (hoje: %s)", extracted, current_date)
        return extracted

    # Detecta "hoje"
    if "hoje" in text_lower:
        logging.info("Detectado 'hoje': %s", current_date)
        return current_date

    # Detecta padrão DD/MM ou DD/MM/YYYY
    match = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if match:
        day = match.group(1).zfill(2)
        month = match.group(2).zfill(2)
        year = match.group(3) or get_current_datetime(timezone_name).strftime("%Y")
        extracted = f"{year}-{month}-{day}"
        logging.info("Detectado data específica: %s", extracted)
        return extracted

    logging.info("Nenhuma data específica detectada, usando hoje: %s", current_date)
    return current_date


def get_fixtures_for_chat(settings: Settings, target_date: str) -> tuple[list[dict[str, Any]], str]:
    """
    Busca jogos do dia para o modo chat.
    Tenta API-Football primeiro; se falhar, usa TheSportsDB como fallback.
    Retorna (lista_de_jogos, fonte) onde fonte é 'football_api' ou 'thesportsdb'.
    """
    # Tenta API-Football se a chave estiver configurada
    if settings.rapidapi_key:
        api_client = FootballApiClient(
            api_key=settings.rapidapi_key,
            host=settings.rapidapi_host,
            request_delay_seconds=0.5,
        )
        try:
            fixtures = api_client.get_daily_fixtures(
                league_ids=settings.league_ids,
                target_date=target_date,
                timezone=settings.timezone,
            )
            if fixtures:
                result = []
                for fixture in fixtures[:15]:
                    league = fixture.get("league", {})
                    teams = fixture.get("teams", {})
                    fixture_info = fixture.get("fixture", {})
                    result.append({
                        "kickoff": fixture_info.get("date"),
                        "league": league.get("name") or LEAGUE_NAMES.get(league.get("id"), "Liga"),
                        "home": teams.get("home", {}).get("name"),
                        "away": teams.get("away", {}).get("name"),
                        "technical_metrics": unavailable_technical_metrics(),
                        "source": "football_api",
                    })
                logging.info("API-Football retornou %d jogos para %s", len(result), target_date)
                return result, "football_api"
            else:
                logging.info("API-Football retornou 0 jogos para %s, tentando TheSportsDB", target_date)
        except Exception as exc:
            logging.warning("API-Football falhou (%s), usando TheSportsDB como fallback", exc)

    # Fallback: TheSportsDB
    logging.info("Buscando jogos via TheSportsDB para %s", target_date)
    sportsdb = SportsDbClient()
    fixtures = sportsdb.get_fixtures_for_date(target_date)
    return fixtures, "thesportsdb"


def ask_llm_for_predictions(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    cleaned_payload: list[dict[str, Any]],
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    user_prompt = (
        "Analise as partidas abaixo e retorne os melhores palpites no formato pedido.\n\n"
        "Use xG, posse de bola e finalizacoes quando existirem no JSON. "
        "Se estiverem indisponiveis, informe isso sem inventar dados.\n\n"
        f"{json.dumps(cleaned_payload, ensure_ascii=False, indent=2)}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content or ""
    logging.info("Analise gerada com provider %s e modelo %s", provider, model)
    return sanitize_public_analysis_message(content.strip())


def ask_llm_for_chat_reply(
    settings: Settings,
    user_message: str,
    fixtures_context: list[dict[str, Any]] | None = None,
    fixtures_source: str = "football_api",
    target_date: str | None = None,
) -> str:
    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    # Escolhe o system prompt baseado na fonte dos dados
    if fixtures_context and fixtures_source == "thesportsdb":
        system_prompt = CHAT_SYSTEM_PROMPT_SPORTSDB
    else:
        system_prompt = CHAT_SYSTEM_PROMPT

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    if fixtures_context:
        fixtures_json = json.dumps(fixtures_context, ensure_ascii=False, indent=2)
        source_label = "TheSportsDB (agenda oficial)" if fixtures_source == "thesportsdb" else "API-Football"
        date_label = target_date or "data solicitada"
        messages.append({
            "role": "user",
            "content": (
                f"DATA DOS JOGOS: {date_label}\n"
                f"FONTE: {source_label}\n\n"
                f"{fixtures_json}\n\n"
                f"Analise cada um dos {len(fixtures_context)} jogos acima. "
                f"Todos são do dia {date_label}. "
                f"Use xG, posse de bola e finalizacoes quando existirem no JSON; se estiverem indisponiveis, informe isso sem inventar."
            ),
        })
    else:
        messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(
        model=settings.llm_model,
        messages=messages,
    )
    content = response.choices[0].message.content or ""
    return content.strip() or "Não consegui responder agora. Tente novamente em instantes."


def split_message(text: str, max_length: int = 4000) -> list[str]:
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    chunk = ""

    for block in text.split("\n---\n"):
        candidate = f"{chunk}\n---\n{block}".strip() if chunk else block
        if len(candidate) <= max_length:
            chunk = candidate
            continue
        if chunk:
            parts.append(chunk)
        chunk = block

    if chunk:
        parts.append(chunk)

    normalized_parts: list[str] = []
    for part in parts:
        if len(part) <= max_length:
            normalized_parts.append(part)
            continue
        for index in range(0, len(part), max_length):
            normalized_parts.append(part[index: index + max_length])

    return normalized_parts


async def send_to_telegram(token: str, chat_id: str, message: str) -> None:
    bot = Bot(token=token)
    async with bot:
        for chunk in split_message(message):
            await bot.send_message(chat_id=chat_id, text=chunk)


def send_to_telegram_sync(token: str, chat_id: str, message: str) -> None:
    """Versão síncrona segura — cria novo event loop para evitar conflito com o loop do bot."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_to_telegram(token, chat_id, message))
    finally:
        loop.close()


def build_no_games_message(target_date: str) -> str:
    return f"Nenhuma partida encontrada para {target_date} nas ligas configuradas."


def build_rate_limit_message(target_date: str) -> str:
    return (
        f"BetChat nao conseguiu concluir a analise de {target_date} porque a cota da API-Football foi atingida. "
        "Tente novamente mais tarde ou reduza MAX_FIXTURES."
    )


def remember_morning_report(target_date: str, report: str) -> None:
    """Guarda o relatorio matinal para comparar com lembretes do mesmo dia."""
    if report.strip():
        MORNING_REPORT_MEMORY[target_date] = report.strip()


def get_fixture_date(fixture: dict[str, Any], timezone_name: str) -> str:
    kickoff = str(fixture.get("kickoff") or "")
    match = re.search(r"\d{4}-\d{2}-\d{2}", kickoff)
    if match:
        return match.group(0)
    return get_current_datetime(timezone_name).strftime("%Y-%m-%d")


def get_morning_report_context(fixture: dict[str, Any], settings: Settings) -> str:
    target_date = get_fixture_date(fixture, settings.timezone)
    report = MORNING_REPORT_MEMORY.get(target_date, "").strip()
    if not report:
        return "Relatorio Matinal nao encontrado em memoria para este jogo."
    return report[:6000]


def is_unavailable_value(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    text = str(value).strip().lower()
    return text in {"n/d", "nd", "none", "null"} or "nao disponivel" in text or "não disponível" in text


def count_objective_evidence(fixture: dict[str, Any]) -> int:
    evidence = 0

    for team_key in ("home_team", "away_team"):
        team = fixture.get(team_key) or {}
        for stat_key in ("avg_goals_scored", "avg_goals_conceded", "last_5_form"):
            if not is_unavailable_value(team.get(stat_key)):
                evidence += 1

    odds = fixture.get("odds") or {}
    for market_key in ("match_winner", "over_under", "both_teams_score", "corners"):
        market = odds.get(market_key)
        if isinstance(market, dict) and any(not is_unavailable_value(value) for value in market.values()):
            evidence += 1

    technical_metrics = fixture.get("technical_metrics") or {}
    if isinstance(technical_metrics, dict):
        for value in technical_metrics.values():
            if not is_unavailable_value(value):
                evidence += 1

    return evidence


def sanitize_public_analysis_message(message: str) -> str:
    internal_markers = (
        "REGRAS INTERNAS",
        "REGRAS IMPORTANTES",
        "REGRAS DE SEGURANÇA",
        "ANTES DE FINALIZAR",
        "verificação interna obrigatória",
    )
    public_section_starts = (
        "⏰", "⚽", "🏆", "📌", "📊", "🎯", "🧠", "⚠️ Gestão de risco",
    )

    cleaned_lines: list[str] = []
    skipping_internal_section = False

    for line in message.splitlines():
        stripped = line.strip()
        if any(marker.lower() in stripped.lower() for marker in internal_markers):
            skipping_internal_section = True
            continue
        if skipping_internal_section and stripped.startswith(public_section_starts):
            skipping_internal_section = False
        if not skipping_internal_section:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def build_low_data_reminder_message(
    fixture: dict[str, Any],
    morning_report_context: str,
) -> str:
    home = fixture.get("home") or (fixture.get("home_team") or {}).get("name") or "Time Casa"
    away = fixture.get("away") or (fixture.get("away_team") or {}).get("name") or "Time Fora"
    league = fixture.get("league") or "Liga"
    kickoff = fixture.get("kickoff") or "horário não confirmado"

    if morning_report_context.startswith("Relatorio Matinal nao encontrado"):
        morning_line = "Não há relatório matinal salvo para comparar este jogo."
    else:
        morning_line = (
            "O relatório matinal ficou como contexto, mas a checagem pré-jogo "
            "não trouxe dados novos para validar uma entrada."
        )

    return (
        "⏰ JOGO EM 30 MINUTOS\n"
        f"⚽ {home} x {away}\n"
        f"🏆 {league} | 🕐 {kickoff}\n\n"
        "📌 STATUS DOS DADOS\n"
        "Dados pré-jogo insuficientes: só há confirmação de times, liga e horário.\n\n"
        "📊 LEITURA PRÉ-JOGO\n"
        f"{morning_line} Sem estatísticas atualizadas, odds confirmadas, escalações ou desfalques, "
        "a leitura precisa ser tratada como limitada.\n\n"
        "🎯 RECOMENDAÇÃO\n"
        "Mercado: SEM ENTRADA\n"
        "Odd: não confirmada\n"
        "Stake: 0\n"
        "Confiança: 4/10\n\n"
        "🧠 JUSTIFICATIVA\n"
        "Não há pelo menos 3 evidências objetivas a favor de um mercado. "
        "A decisão mais prudente é preservar banca em vez de manter uma confiança alta sem validação.\n\n"
        "⚠️ Gestão de risco: aposta não é certeza. Use banca definida e não aumente stake para recuperar perdas."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "BetChat online. Analiso jogos com foco em gols, ambas marcam e escanteios. "
        "Pergunte sobre jogos de hoje, amanhã ou qualquer partida específica."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Comandos:\n"
        "/start - inicia o bot\n"
        "/help - mostra esta ajuda\n\n"
        "Exemplos de perguntas:\n"
        "- Jogos de hoje\n"
        "- Jogos de amanhã\n"
        "- Apostas de hoje\n"
        "- Vasco x Flamengo, analisa\n"
        "- Real Madrid x Barcelona escanteios\n\n"
        "Em grupo, me mencione com @Betchatdo_bot ou responda uma mensagem minha."
    )


def should_answer_message(update: Update, bot_username: str | None) -> bool:
    message = update.message
    if not message or not message.text:
        return False
    if message.chat.type == "private":
        return True
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.is_bot
    ):
        return True
    if bot_username and f"@{bot_username.lower()}" in message.text.lower():
        return True
    return False


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    bot_username: str | None = context.application.bot_data.get("bot_username")

    if not update.message or not update.message.text:
        return

    if not should_answer_message(update, bot_username):
        return

    await update.message.chat.send_action("typing")

    user_text = update.message.text
    fixtures_context: list[dict[str, Any]] | None = None
    fixtures_source = "football_api"
    target_date: str | None = None

    # Se a mensagem pede jogos, busca dados reais (API-Football ou TheSportsDB)
    if message_wants_fixtures(user_text):
        target_date = extract_date_from_message(
            user_text,
            get_current_datetime(settings.timezone).strftime("%Y-%m-%d"),
            settings.timezone,
        )
        logging.info("Buscando jogos para o chat: %s", target_date)
        fixtures_context, fixtures_source = await asyncio.to_thread(
            get_fixtures_for_chat, settings, target_date
        )
        if fixtures_context:
            logging.info(
                "Encontrados %d jogos via %s para %s",
                len(fixtures_context), fixtures_source, target_date,
            )
        else:
            logging.info("Nenhum jogo encontrado para %s em nenhuma fonte", target_date)

    try:
        reply = await asyncio.to_thread(
            ask_llm_for_chat_reply,
            settings,
            user_text,
            fixtures_context,
            fixtures_source,
            target_date,
        )
    except Exception as exc:
        logging.error("Erro ao chamar a LLM: %s", exc)
        await update.message.reply_text(
            "Ocorreu um erro ao processar sua mensagem. Tente novamente em instantes."
        )
        return

    for chunk in split_message(reply):
        await update.message.reply_text(chunk)


async def post_init(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username
    logging.info("Bot conversacional conectado como @%s", me.username)


def run_chat_bot(settings: Settings) -> None:
    application = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(post_init)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    application.bot_data["settings"] = settings
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))
    logging.info("Iniciando modo chat por polling")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


def run_cron_bot(settings: Settings) -> None:
    # Tenta API-Football primeiro
    if settings.rapidapi_key:
        api_client = FootballApiClient(
            api_key=settings.rapidapi_key,
            host=settings.rapidapi_host,
            request_delay_seconds=settings.request_delay_seconds,
        )

        logging.info("Buscando partidas de %s via API-Football", settings.target_date)
        try:
            logging.info("Chamando API-Football /fixtures...")
            cleaned_payload = build_analysis_payload(api_client, settings)
            logging.info("API-Football respondeu. Partidas encontradas: %s", len(cleaned_payload))

            if cleaned_payload:
                _send_cron_analysis(settings, cleaned_payload)
                return

            logging.info("API-Football sem jogos, tentando TheSportsDB...")
        except FootballApiRateLimitError as exc:
            logging.warning("%s", exc)
            message = build_rate_limit_message(settings.target_date)
            send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
            logging.info("Fluxo finalizado com aviso de limite da API")
            return
        except Exception as exc:
            logging.warning("API-Football falhou: %s. Tentando TheSportsDB...", exc)

    # Fallback: TheSportsDB para agenda + LLM para análise
    logging.info("Buscando partidas de %s via TheSportsDB", settings.target_date)
    sportsdb = SportsDbClient()
    fixtures = sportsdb.get_fixtures_for_date(settings.target_date)

    if not fixtures:
        message = build_no_games_message(settings.target_date)
        logging.info("Nenhuma partida encontrada em nenhuma fonte")
    else:
        logging.info("TheSportsDB retornou %d jogos. Enviando para LLM...", len(fixtures))
        try:
            message = ask_llm_for_predictions(
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                cleaned_payload=fixtures[:settings.max_fixtures],
            )
            logging.info("LLM respondeu com sucesso.")
        except Exception as exc:
            logging.error("Erro ao chamar LLM: %s", exc, exc_info=True)
            send_to_telegram_sync(
                    settings.telegram_token,
                    settings.telegram_chat_id,
                    f"❌ Erro ao gerar análise: {exc}",
                )
            return

    logging.info("Enviando mensagem para o Telegram...")
    send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
    logging.info("Fluxo finalizado com sucesso")


def _send_cron_analysis(settings: Settings, cleaned_payload: list[dict[str, Any]]) -> None:
    """Envia análise LLM para o Telegram no modo cron."""
    logging.info("Enviando %s partidas para analise LLM...", len(cleaned_payload))
    try:
        message = ask_llm_for_predictions(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            cleaned_payload=cleaned_payload,
        )
        logging.info("LLM respondeu com sucesso.")
    except Exception as exc:
        logging.error("Erro ao chamar LLM: %s", exc, exc_info=True)
        send_to_telegram_sync(
                settings.telegram_token,
                settings.telegram_chat_id,
                f"❌ Erro ao gerar análise: {exc}",
            )
        return

    logging.info("Enviando mensagem para o Telegram...")
    send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
    logging.info("Fluxo finalizado com sucesso")


def send_game_reminder(settings: Settings, fixture: dict[str, Any]) -> None:
    """Envia lembrete 30 minutos antes de um jogo específico."""
    home = fixture.get("home") or (fixture.get("home_team") or {}).get("name") or "?"
    away = fixture.get("away") or (fixture.get("away_team") or {}).get("name") or "?"
    league = fixture.get("league", "?")
    kickoff = fixture.get("kickoff", "")
    morning_report_context = get_morning_report_context(fixture, settings)
    evidence_count = count_objective_evidence(fixture)

    logging.info("Enviando lembrete: %s x %s", home, away)

    if evidence_count < 3:
        message = build_low_data_reminder_message(fixture, morning_report_context)
        send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
        return

    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    fixture_json = json.dumps(fixture, ensure_ascii=False, indent=2)
    try:
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": REMINDER_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Jogo: {home} x {away}\n"
                        f"Liga: {league}\n"
                        f"Horário: {kickoff}\n\n"
                        f"RELATORIO MATINAL DO DIA (memoria para consistencia):\n"
                        f"{morning_report_context}\n\n"
                        f"EVIDENCIAS OBJETIVAS CONFIRMADAS: {evidence_count}\n"
                        f"DADOS DISPONÍVEIS DA API:\n{fixture_json}\n\n"
                        f"Para qualquer dado ausente, resuma a lacuna no status dos dados sem listar campos vazios.\n"
                        f"Se a recomendação mudar em relação ao Relatório Matinal, explique o dado novo confirmado que motivou a mudança.\n"
                        f"NÃO invente nenhuma informação. Gere a análise pré-jogo."
                    ),
                },
            ],
            max_tokens=600,
        )
        message = response.choices[0].message.content or ""
        message = sanitize_public_analysis_message(message.strip())
    except Exception as exc:
        logging.error("Erro ao gerar lembrete para %s x %s: %s", home, away, exc)
        message = (
            f"⏰ Em 30 minutos!\n"
            f"⚽ {home} x {away} — {league}\n"
            f"🕐 {kickoff}"
        )

    # Usa novo loop para evitar conflito com loop do bot de chat
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            send_to_telegram(settings.telegram_token, settings.telegram_chat_id, message)
        )
    finally:
        loop.close()


def schedule_game_reminders(
    settings: Settings,
    fixtures: list[dict[str, Any]],
    apscheduler: BackgroundScheduler,
) -> int:
    """
    Agenda lembretes 30 minutos antes de cada jogo.
    Retorna o número de lembretes agendados.
    """
    now_utc = datetime.now(UTC)
    scheduled = 0

    for fixture in fixtures:
        kickoff_str = fixture.get("kickoff", "")
        if not kickoff_str:
            continue

        # Parse do horário — suporta múltiplos formatos
        kickoff_utc: datetime | None = None
        formats_to_try = [
            ("%Y-%m-%d %H:%M BRT", timezone(timedelta(hours=-3))),
            ("%Y-%m-%d %H:%M:%S BRT", timezone(timedelta(hours=-3))),
            ("%Y-%m-%d %H:%M", timezone(timedelta(hours=-3))),
            ("%Y-%m-%dT%H:%M:%S%z", None),  # ISO com timezone
            ("%Y-%m-%dT%H:%M:%S", timezone(timedelta(hours=-3))),
        ]

        for fmt, tz in formats_to_try:
            try:
                clean = kickoff_str.replace(" BRT", "").strip()
                dt = datetime.strptime(clean, fmt.replace(" BRT", "").strip())
                if tz:
                    kickoff_utc = dt.replace(tzinfo=tz).astimezone(UTC)
                else:
                    kickoff_utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
                break
            except ValueError:
                continue

        if kickoff_utc is None:
            logging.warning("Não foi possível parsear horário '%s' — lembrete ignorado", kickoff_str)
            continue

        # Agenda 30 minutos antes
        reminder_time = kickoff_utc - timedelta(minutes=30)

        home = fixture.get("home", "?")
        away = fixture.get("away", "?")

        # Só agenda se ainda está no futuro (com margem de 2 minutos)
        if reminder_time <= now_utc + timedelta(minutes=2):
            logging.info(
                "Lembrete de %s x %s ignorado (horário já passou: %s UTC)",
                home, away, reminder_time.strftime("%H:%M"),
            )
            continue

        job_id = f"reminder_{home}_{away}_{kickoff_str}".replace(" ", "_").replace("/", "-")

        apscheduler.add_job(
            send_game_reminder,
            trigger=DateTrigger(run_date=reminder_time),
            args=[settings, fixture],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=600,  # 10 min de tolerância
        )
        logging.info(
            "Lembrete agendado: %s x %s às %s UTC (kickoff %s)",
            home, away, reminder_time.strftime("%H:%M"), kickoff_str,
        )
        scheduled += 1

    return scheduled


def send_morning_report(settings: Settings, apscheduler: "BackgroundScheduler | None" = None) -> None:
    """Busca jogos do dia, envia o relatório matinal e agenda lembretes 30min antes de cada jogo."""
    today = get_current_datetime(settings.timezone).strftime("%Y-%m-%d")
    logging.info("Gerando relatório matinal para %s", today)

    # Tenta API-Football, senão usa TheSportsDB
    fixtures: list[dict[str, Any]] = []
    if settings.rapidapi_key:
        api_client = FootballApiClient(
            api_key=settings.rapidapi_key,
            host=settings.rapidapi_host,
            request_delay_seconds=settings.request_delay_seconds,
        )
        try:
            raw = api_client.get_daily_fixtures(
                league_ids=settings.league_ids,
                target_date=today,
                timezone=settings.timezone,
            )
            fixtures = [
                {
                    "kickoff": f.get("fixture", {}).get("date"),
                    "league": f.get("league", {}).get("name") or LEAGUE_NAMES.get(f.get("league", {}).get("id"), "Liga"),
                    "home": f.get("teams", {}).get("home", {}).get("name"),
                    "away": f.get("teams", {}).get("away", {}).get("name"),
                    "technical_metrics": unavailable_technical_metrics(),
                }
                for f in raw[:settings.max_fixtures]
            ]
            logging.info("API-Football: %d jogos para %s", len(fixtures), today)
        except Exception as exc:
            logging.warning("API-Football falhou: %s. Usando TheSportsDB...", exc)

    if not fixtures:
        sportsdb = SportsDbClient()
        fixtures = sportsdb.get_fixtures_for_date(today)
        logging.info("TheSportsDB: %d jogos para %s", len(fixtures), today)

    if not fixtures:
        message = f"📋 Relatório matinal {today}\n\nNenhuma partida encontrada nas ligas configuradas."
        send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, message)
        return

    # Gera o relatório via LLM
    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    fixtures_json = json.dumps(fixtures[:settings.max_fixtures], ensure_ascii=False, indent=2)

    try:
        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": MORNING_REPORT_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"DATA: {today}\n"
                        f"JOGOS DO DIA ({len(fixtures[:settings.max_fixtures])} partidas):\n"
                        f"{fixtures_json}\n\n"
                        f"Use xG, posse de bola e finalizacoes quando os campos estiverem disponiveis. "
                        f"Quando vierem como indisponiveis, declare a indisponibilidade e nao invente.\n"
                        f"Gere o relatório matinal com os palpites do dia."
                    ),
                },
            ],
        )
        report = response.choices[0].message.content or ""
        report = report.strip()
        logging.info("Relatório matinal gerado com sucesso.")
    except Exception as exc:
        logging.error("Erro ao gerar relatório matinal: %s", exc)
        report = f"❌ Erro ao gerar relatório matinal: {exc}"

    if "Erro ao gerar relatório matinal" not in report:
        remember_morning_report(today, report)

    header = f"🌅 BetChat — Relatório Matinal {today}\n\n"

    # Aviso quando há menos de 10 jogos disponíveis
    total = len(fixtures[:settings.max_fixtures])
    if total < 10:
        header += (
            f"⚠️ Hoje encontramos apenas {total} jogo(s) nas ligas monitoradas. "
            f"Em dias com poucos jogos a cobertura pode ser limitada.\n\n"
        )

    send_to_telegram_sync(settings.telegram_token, settings.telegram_chat_id, header + report)
    logging.info("Relatório matinal enviado para o Telegram.")

    # Agenda lembretes 30 min antes de cada jogo
    if apscheduler is not None:
        count = schedule_game_reminders(settings, fixtures[:settings.max_fixtures], apscheduler)
        logging.info("%d lembretes agendados para hoje.", count)


def run_scheduled_bot(settings: Settings) -> None:
    """
    Modo scheduled: roda o bot de chat E agenda o relatório matinal às 7h BRT
    com lembretes automáticos 30 minutos antes de cada jogo.
    BOT_MODE=scheduled no Railway.
    """
    # APScheduler para lembretes dinâmicos por jogo
    apscheduler = BackgroundScheduler(timezone=UTC)
    apscheduler.start()
    logging.info("APScheduler iniciado.")

    # Horário 7h BRT = 10h UTC
    schedule_time_utc = "10:00"
    logging.info("Agendando relatório matinal para 07:00 BRT (10:00 UTC) todos os dias.")

    def job() -> None:
        try:
            send_morning_report(settings, apscheduler)
        except Exception as exc:
            logging.error("Erro no job do relatório matinal: %s", exc, exc_info=True)

    schedule.every().day.at(schedule_time_utc).do(job)

    # Se o bot reiniciou depois das 7h BRT, re-agenda os lembretes do dia
    now_brt = get_current_datetime(settings.timezone)
    report_time_brt = now_brt.replace(hour=7, minute=0, second=0, microsecond=0)
    if now_brt > report_time_brt:
        logging.info("Bot iniciado após 07h BRT — re-agendando lembretes do dia...")
        try:
            today = now_brt.strftime("%Y-%m-%d")
            sportsdb = SportsDbClient()
            fixtures = sportsdb.get_fixtures_for_date(today)
            if fixtures:
                count = schedule_game_reminders(settings, fixtures[:settings.max_fixtures], apscheduler)
                logging.info("Re-agendados %d lembretes para hoje (%s).", count, today)
            else:
                logging.info("Nenhum jogo encontrado para re-agendar hoje.")
        except Exception as exc:
            logging.warning("Erro ao re-agendar lembretes: %s", exc)

    # Roda o scheduler de horário fixo em thread separada
    import threading

    def run_scheduler() -> None:
        logging.info("Scheduler de horário fixo iniciado.")
        while True:
            schedule.run_pending()
            time.sleep(30)

    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Inicia o bot de chat normalmente
    logging.info("Iniciando bot de chat com relatório matinal e lembretes agendados.")
    run_chat_bot(settings)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    settings = load_settings()

    if settings.bot_mode == "chat":
        run_chat_bot(settings)
        return

    if settings.bot_mode == "scheduled":
        run_scheduled_bot(settings)
        return

    run_cron_bot(settings)


if __name__ == "__main__":
    main()
