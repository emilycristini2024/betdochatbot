# BetChat

Bot em Python para buscar jogos do dia via API-Football, resumir estatísticas úteis, pedir análise para uma LLM compatível com API OpenAI (configurada para Groq) e publicar os palpites em um chat/canal do Telegram.

## Requisitos

- Python 3.11+
- Bot criado no Telegram via `@BotFather`
- Chaves válidas da Groq e API-Football

## Configuração

1. Crie um ambiente virtual:

```bash
python -m venv .venv
```

2. Ative o ambiente e instale as dependências:

```bash
pip install -r requirements.txt
```

3. Copie `.env.example` para `.env` e preencha as variáveis:

```env
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
LLM_PROVIDER=groq
LLM_API_KEY=
LLM_BASE_URL=https://api.groq.com/openai/v1
RAPIDAPI_KEY=
```

## Como descobrir o chat do Telegram

- Para canal: crie o canal, adicione o bot como administrador e use o identificador do canal em `TELEGRAM_CHAT_ID` (ex.: `@meucanal`).
- Para grupo: adicione o bot ao grupo e use o ID numérico do chat.
- Se quiser obter o ID numérico com segurança, envie uma mensagem no grupo/canal e consulte a API do Telegram com `getUpdates`.

## Execução

```bash
python main.py
```

## Agendamento

Você pode agendar a execução no Railway ou Cron. Para a API gratuita, prefira um horário único por dia, como `08:00` ou `09:00` no horário de Brasília.

## Deploy recomendado

O melhor encaixe para este projeto é um Cron Job no Railway, porque o script executa, envia a mensagem e encerra sozinho.

Passo a passo sugerido:

1. Suba este repositório para o GitHub.
2. No Railway, crie um projeto com `Deploy from GitHub repo`.
3. Adicione as variáveis do `.env` na aba `Variables`.
4. Em `Settings`, confirme o start command `python main.py`.
5. Em `Cron Schedule`, defina o horário em UTC.

Exemplo:

- `11 11 * * *` roda todos os dias às `08:11` no horário de Brasília quando estiver em UTC-3.

Se preferir não manter um servidor ligado o tempo todo, GitHub Actions agendado também funciona, mas Railway costuma ser mais simples para logs e variáveis.

## Observação importante

O plano gratuito da API-Football costuma ser limitado. Por isso, o script usa `MAX_FIXTURES` para evitar consumir cotas demais ao buscar estatísticas e odds.

Para economizar requisições, o script busca os jogos do dia em uma chamada única e filtra as ligas localmente. Mesmo assim, estatísticas por time e odds por jogo ainda consomem cota, então `MAX_FIXTURES` e `REQUEST_DELAY_SECONDS` ajudam a controlar uso.

## Modelo padrão

O projeto está configurado para usar Groq com o modelo `llama-3.3-70b-versatile`. Se quiser trocar depois, basta alterar `LLM_MODEL` no `.env`.
