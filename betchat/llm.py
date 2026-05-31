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
        source_label = (
            "TheSportsDB (agenda oficial)" if fixtures_source == "thesportsdb" else "API-Football"
        )
        date_label = target_date or "data solicitada"
        messages.append({
            "role": "user",
            "content": (
                f"DATA DOS JOGOS: {date_label}\n"
                f"FONTE: {source_label}\n\n"
                f"{fixtures_json}\n\n"
                f"Analise cada um dos {len(fixtures_context)} jogos acima. "
                f"Todos são do dia {date_label}. "
                f"Use xG, posse de bola e finalizacoes quando existirem no JSON; "
                f"se estiverem indisponiveis, informe isso sem inventar."
            ),
        })
    else:
        messages.append({"role": "user", "content": user_message})

    response = client.chat.completions.create(model=model, messages=messages)
    content = response.choices[0].message.content or ""
    return content.strip() or "Não consegui responder agora. Tente novamente em instantes."


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
                    f"Use xG, posse de bola e finalizacoes quando os campos estiverem disponiveis. "
                    f"Quando vierem como indisponiveis, declare a indisponibilidade e nao invente.\n"
                    f"Gere o relatório matinal com os palpites do dia."
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
                    f"Horário: {kickoff}\n\n"
                    f"RELATORIO MATINAL DO DIA (memoria para consistencia):\n"
                    f"{morning_report_context}\n\n"
                    f"EVIDENCIAS OBJETIVAS CONFIRMADAS: {evidence_count}\n"
                    f"DADOS DISPONÍVEIS DA API:\n{fixture_json}\n\n"
                    f"Para qualquer dado ausente, resuma a lacuna no status dos dados sem listar campos vazios.\n"
                    f"Se a recomendação mudar em relação ao Relatório Matinal, explique o dado novo confirmado.\n"
                    f"NÃO invente nenhuma informação. Gere a análise pré-jogo."
                ),
            },
        ],
        max_tokens=600,
    )
    content = response.choices[0].message.content or ""
    return sanitize_public_analysis_message(content.strip())


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
