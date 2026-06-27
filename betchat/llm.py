import json
import logging
from typing import Any

from openai import OpenAI

from .prompts import (
    DEFAULT_SYSTEM_PROMPT,
    MORNING_REPORT_PROMPT,
    REMINDER_PROMPT,
    CHAT_SYSTEM_PROMPT,
    CHAT_SYSTEM_PROMPT_SPORTSDB,
)


def ask_llm_for_predictions(
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    cleaned_payload: list[dict[str, Any]],
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    user_prompt = (
        "Execute o prompt master para as partidas abaixo no formato solicitado.\n"
        "Use apenas os dados do JSON e trate campos indisponiveis como lacunas.\n"
        "Se nao houver escalacao oficial confirmada, nao recomende aposta pre-jogo.\n\n"
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
    api_key: str,
    base_url: str,
    model: str,
    user_message: str,
    fixtures_context: list[dict[str, Any]] | None = None,
    fixtures_source: str = "football_api",
    target_date: str | None = None,
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)

    system_prompt = (
        CHAT_SYSTEM_PROMPT_SPORTSDB
        if fixtures_context and fixtures_source == "thesportsdb"
        else CHAT_SYSTEM_PROMPT
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    if fixtures_context:
        fixtures_json = json.dumps(fixtures_context, ensure_ascii=False, indent=2)
        source_labels = {
            "thesportsdb": "TheSportsDB (agenda oficial)",
            "football_data": "football-data.org",
            "football_api": "API-Football",
        }
        source_label = source_labels.get(fixtures_source, fixtures_source)
        date_label = target_date or "data solicitada"
        messages.append(
            {
                "role": "user",
                "content": (
                    f"DATA DOS JOGOS: {date_label}\n"
                    f"FONTE: {source_label}\n\n"
                    f"{fixtures_json}\n\n"
                    f"Analise cada um dos {len(fixtures_context)} jogos acima. "
                    f"Todos sao do dia {date_label}. "
                    f"Use somente os campos do JSON. Se xG, posse de bola, finalizacoes, "
                    f"odds ou escalacoes estiverem indisponiveis, informe a lacuna e nao invente. "
                    f"Sem escalacao oficial confirmada, use SEM ENTRADA."
                ),
            }
        )
    else:
        messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(model=model, messages=messages)
    content = response.choices[0].message.content or ""
    return content.strip() or "Nao consegui responder agora. Tente novamente em instantes."


def ask_llm_for_morning_report(
    api_key: str,
    base_url: str,
    model: str,
    fixtures: list[dict[str, Any]],
    today: str,
    max_fixtures: int,
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    fixtures_json = json.dumps(fixtures[:max_fixtures], ensure_ascii=False, indent=2)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": MORNING_REPORT_PROMPT},
            {
                "role": "user",
                "content": (
                    f"DATA: {today}\n"
                    f"JOGOS DO DIA ({len(fixtures[:max_fixtures])} partidas):\n"
                    f"{fixtures_json}\n\n"
                    f"Gere o relatorio matinal como triagem estatistica. "
                    f"Use apenas os dados do JSON. Quando campos vierem como indisponiveis, "
                    f"declare a lacuna e nao invente. Sem escalacao oficial confirmada, "
                    f"use SEM ENTRADA e marque para monitorar perto do inicio."
                ),
            },
        ],
    )
    report = response.choices[0].message.content or ""
    return report.strip()


def ask_llm_for_reminder(
    api_key: str,
    base_url: str,
    model: str,
    fixture: dict[str, Any],
    morning_report_context: str,
    evidence_count: int,
) -> str:
    client = OpenAI(api_key=api_key, base_url=base_url)
    home = fixture.get("home") or (fixture.get("home_team") or {}).get("name") or "?"
    away = fixture.get("away") or (fixture.get("away_team") or {}).get("name") or "?"
    league = fixture.get("league", "?")
    kickoff = fixture.get("kickoff", "")
    fixture_json = json.dumps(fixture, ensure_ascii=False, indent=2)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": REMINDER_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Jogo: {home} x {away}\n"
                    f"Liga: {league}\n"
                    f"Horario: {kickoff}\n\n"
                    f"RELATORIO MATINAL DO DIA (memoria para consistencia):\n"
                    f"{morning_report_context}\n\n"
                    f"EVIDENCIAS OBJETIVAS CONFIRMADAS: {evidence_count}\n"
                    f"DADOS DISPONIVEIS DA API:\n{fixture_json}\n\n"
                    f"Para qualquer dado ausente, resuma a lacuna no status dos dados sem listar campos vazios.\n"
                    f"Se a recomendacao mudar em relacao ao relatorio matinal, explique o dado novo confirmado.\n"
                    f"Sem escalacao oficial confirmada, use SEM ENTRADA. Nao invente nenhuma informacao."
                ),
            },
        ],
        max_tokens=1200,
    )
    content = response.choices[0].message.content or ""
    return sanitize_public_analysis_message(content.strip())


def sanitize_public_analysis_message(message: str) -> str:
    internal_markers = (
        "PROMPT MASTER",
        "REGRAS ABSOLUTAS",
        "REGRAS INTERNAS",
        "REGRAS IMPORTANTES",
        "REGRAS DE SEGURANCA",
        "FLUXO ANALITICO",
        "ADAPTACAO AO BETCHAT",
        "POLITICA DE CONFIANCA",
        "ANTES DE FINALIZAR",
        "verificacao interna obrigatoria",
    )
    public_section_starts = (
        "JOGO EM BREVE",
        "STATUS DOS DADOS",
        "LEITURA PRE-JOGO",
        "RECOMENDACAO",
        "JUSTIFICATIVA",
        "Gestao de risco",
        "[",
        "TOP 3",
        "RESUMO DO DIA",
        "MULTIPLAS",
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
