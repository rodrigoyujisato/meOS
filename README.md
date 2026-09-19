# meOS (rysOS)

Assistente executivo pessoal que lê Google Calendar, Gmail, transcrições de reunião e
diário falado, e mantém sozinho um cofre [Obsidian](https://obsidian.md) organizado pelo
método PARA. O Google Gemini entra só para linguagem e roteamento: a estrutura das notas
é sempre gerada por código. Feito para uso pessoal e aberto para quem
quiser rodar e adaptar. Licença MIT.

> **Nome:** o repositório se chama meOS; o pacote Python e o comando de terminal se chamam `rysos` (`uv run rysos ...`).
>
> **Naming:** the repository is meOS; the Python package and the CLI are called `rysos`.

## Quick start (English)

An executive assistant that turns your calendar, e-mail, meeting transcripts and spoken
diary into an organized Obsidian vault. It runs on your own machine, single user. Prompts,
notes and bot messages are in Brazilian Portuguese.

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), a
[Gemini API key](https://aistudio.google.com/apikey). Optional: a Google OAuth client
(Gmail and Calendar), a Telegram bot.

```bash
git clone https://github.com/rodrigoyujisato/meOS.git && cd meOS
uv sync
cp .env.example .env     # set USER_NAME, USER_EMAILS, OBSIDIAN_VAULT_PATH, GEMINI_API_KEY
uv run rysos init-vault  # builds the PARA folder structure inside your vault
uv run rysos status      # checks vault, Google accounts and Gemini
uv run rysos serve       # web UI + scheduler + Telegram bot at http://127.0.0.1:8000
```

Gmail/Calendar need a Google OAuth client and `uv run rysos auth`; Telegram needs a bot
token and your numeric user ID. Step by step in "Instalação e credenciais" below. Two
safety defaults: the web UI has no authentication and binds to `127.0.0.1`, and the
Telegram bot answers nobody until you list your user ID in `TELEGRAM_ALLOWED_USER_IDS`.

---

## Instalação e credenciais

Este documento é a referência principal do sistema: instalação, arquitetura, fluxos,
modelo de dados, contratos de idempotência, configuração e deploy.

**Premissa de projeto:** frontmatter, seções, wikilinks e checkboxes são sempre
montados por templates Jinja2. O modelo só escreve texto (resumos, títulos) e decide
roteamento (área, categoria, intenção). Nenhum fato (nome, número, data, decisão)
pode ser inventado se não estiver na fonte.

### 1. Instalar

Pré-requisitos: Python 3.11 ou superior e o [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/rodrigoyujisato/meOS.git
cd meOS
uv sync                    # cria .venv e instala as dependências travadas em uv.lock
cp .env.example .env
```

Edite o `.env`: `USER_NAME`, `USER_ROLE`, `USER_EMAILS` (todas as suas contas Google,
separadas por vírgula), `OBSIDIAN_VAULT_PATH` (uma pasta nova ou um cofre existente) e
`GEMINI_API_KEY`.

```bash
uv run rysos init-vault    # cria a estrutura PARA no cofre
uv run rysos status        # confere cofre, contas Google e Gemini
```

Sem `GEMINI_API_KEY`, o sistema sobe, mas as funções de IA caem num fallback heurístico.

### 2. Chave do Gemini

Gere em <https://aistudio.google.com/apikey> e cole em `GEMINI_API_KEY`. O uso é cobrado
pelo Google conforme o seu plano; `uv run rysos tokens` mostra o consumo e o custo estimado.

### 3. Google (Gmail e Agenda)

1. Em <https://console.cloud.google.com>, crie um projeto.
2. Em **APIs e serviços > Biblioteca**, ative a **Gmail API** e a **Google Calendar API**.
3. Em **Tela de consentimento OAuth**, escolha o tipo **Externo**, preencha o mínimo e
   adicione as suas contas como **usuários de teste**. Os escopos usados são
   `gmail.readonly`, `gmail.modify` e `calendar.events`. Com o app em modo "Teste", o Google
   expira o token a cada 7 dias; para uso pessoal, publique o app ("Em produção") e aceite
   o aviso de app não verificado.
4. Em **Credenciais > Criar credenciais > ID do cliente OAuth**, escolha **App para
   computador (Desktop)** e baixe o JSON. Salve como `credentials.json` na raiz do projeto.
5. Conecte cada conta (repita para as demais):

```bash
uv run rysos auth          # abre o navegador; os tokens ficam em tokens/token_<email>.json
uv run rysos accounts      # lista as contas conectadas
```

O login abre um navegador na própria máquina. Num servidor sem tela, rode `rysos auth`
no seu computador e copie a pasta `tokens/` (e o `credentials.json`) para o servidor.
Nunca commite esses arquivos; o `.gitignore` já os exclui.

### 4. Telegram (opcional)

1. No Telegram, fale com o [@BotFather](https://t.me/BotFather), envie `/newbot` e copie o
   token para `TELEGRAM_BOT_TOKEN`.
2. Suba o sistema (`uv run rysos serve`) e mande qualquer mensagem ao seu bot. Ele responde
   com o seu ID numérico e recusa o acesso.
3. Coloque esse ID em `TELEGRAM_ALLOWED_USER_IDS` (vários, separados por vírgula) e em
   `TELEGRAM_CHAT_ID`, e reinicie. Sem essa lista, o bot não atende ninguém.

### 5. Rodar

```bash
uv run rysos serve         # API + interface web + agendador + bot, em http://127.0.0.1:8000
```

Comandos disponíveis: `rysos serve | init-vault | sync | status | tokens | accounts | auth`.
A interface web não tem autenticação, por isso escuta só em `127.0.0.1`. Serviço 24/7
(systemd), Docker e acesso remoto: veja [`docs/deploy.md`](docs/deploy.md).

---

## 1. Arquitetura

| Camada | Tecnologia | Papel |
| --- | --- | --- |
| API + PWA | FastAPI + Uvicorn | dashboard multi-dispositivo, endpoints REST, sync manual |
| Agendador | APScheduler (`AsyncIOScheduler`) | briefing 07:00, recap 19:00, varredura a cada 15 min |
| IA | Google Gemini (`google-genai`, SDK síncrono) | classificação de e-mail, briefs, atas estruturadas, chat, roteamento |
| Banco local | SQLAlchemy async + SQLite (`data/rysos.db`) | dedup, candidatos de entidade, decisões, uso de tokens, rascunhos |
| Cofre (fonte da verdade) | Obsidian sobre mount rclone do Google Drive | notas Markdown + YAML, organizadas em PARA |
| Chat | Telegram Bot API (long-polling via `httpx`) | captura de notas, cards de triagem, briefings |
| Auth | Google OAuth 2.0 multi-conta | uma sessão por conta, `tokens/token_<email>.json` |

O núcleo é `RysOSCore` (`src/rysos/core.py`). O agendador chama seus métodos de
sincronização; cada método usa `GeminiAIClient` para interpretar texto,
`EntityResolver` para decidir o que vira wikilink automático versus candidato pendente,
e `VaultManager` para toda escrita no cofre. Tudo se registra no SQLite para dedup e
auditoria.

---

## 2. Estrutura do cofre (PARA)

Criada e reparada em todo boot por `VaultManager.initialize_vault_structure`
(`manager.py:43`) e `Settings.ensure_directories` (`config.py:108`). O cofre não é um
repositório git — sincroniza com o Google Drive via mount rclone; editar um arquivo lá
não é uma mudança versionada.

| Pasta | Dono | Escrito por | Conteúdo |
| --- | --- | --- | --- |
| `00_Cockpit/Daily/<data>.md` | máquina (preserva itens marcados) | `create_daily_cockpit`, `add_cockpit_focus` | painel do dia: focos, agenda, e-mails críticos, decisões abertas |
| `00_Cockpit/Weekly/` | humano | template | revisão semanal (Dataview) |
| `01_Projects/` | misto | `create_project_note` / `ensure_project_note` | projetos. Match é pelo nome do arquivo, não pelo `title:` |
| `02_Areas/` | máquina (uma vez) | `_ensure_area_notes` | as 4 áreas fixas |
| `03_Decisions/` | misto | `create_decision_note` | registro de decisões `DEC-AAAA-NNNNN` |
| `04_Meetings/<data> - <slug>.md` | topo=máquina, corpo=humano | `create_meeting_note`, `append_transcript_minutes` | notas de reunião; abaixo de "Notas & Discussão" nunca é sobrescrito |
| `05_People/` | misto | `create_person_note` / `ensure_person_note`, `enrich_person_note` | pessoas e stakeholders |
| `06_Resources/` | humano | `create_idea_note`, `create_framework_note` | ideias, leituras, frameworks |
| `07_Inbox_Agent/Transcricoes/` | entrada | você larga o `.txt` | fila de transcrições; `Processado/` = já ingeridas |
| `07_Inbox_Agent/Diario/` | entrada | pipeline externo de transcrição de voz | fila de diário falado |
| `07_Inbox_Agent/Triagem/` | humano resolve, máquina arquiva | `write_triagem_note`, `apply_pending_triagem` | entidades que não foram criadas/vinculadas automaticamente |
| `08_Archive/Triagem/` | máquina | `_archive_triagem_note` | triagens concluídas |
| `09_Journal/<data>.md` | máquina (append-only) | `ensure_journal_note`, `append_diary_entry` | uma nota por dia, narração verbatim + extração |
| `_templates/` | referência | `_ensure_template_files` | templates Jinja2 crus |

**Áreas (conjunto fechado de 4)** — `_resolve_area` (`manager.py:1613`) dobra qualquer
rótulo do modelo num destes pares; default é Negócios.

| slug | título |
| --- | --- |
| `Negocios_e_Governanca` | Negócios & Governança |
| `Saude_e_Vitalidade` | Saúde & Vitalidade |
| `Patrimonio_e_Financas` | Patrimônio & Finanças |
| `Familia_e_Relacionamentos` | Família & Relacionamentos |

---

## 3. Fluxos principais

### Sincronização diária (`run_daily_sync`, `core.py:63`)

Gatilho: cron 07:00, `POST /api/sync`, ou `rysos sync`.

1. Inicializa banco e estrutura do cofre.
2. Se autenticado no Google: busca eventos do Calendar e gera briefs de reunião; busca
   e-mails do Gmail e analisa prioridade/boletos.
3. E-mail de boleto vencendo hoje vira foco no Cockpit; vencendo depois agenda um
   lembrete para a data certa.
4. Monta o Cockpit do dia com focos, agenda, e-mails e decisões pendentes.
5. Processa transcrições e diário pendentes (ver abaixo).
6. Grava uma linha em `sync_logs`.

Convites com descrição curta (menos de 15 caracteres) nunca vão ao modelo — recebem um
brief factual determinístico, nunca uma pauta inventada. Boletos duplicados (o mesmo
boleto e seu lembrete, ou cópias vindas de relays diferentes) colapsam numa única
entrada pela chave vencimento + credor, nunca pelo id da mensagem.

### Ingestão de transcrição de reunião (`process_pending_transcripts`, `core.py:374`)

Gatilho: job a cada 15 minutos (`TRANSCRIPT_SCAN_INTERVAL_MIN`), e no fim do sync
diário. Você larga um `.txt`/`.md`/`.vtt`/`.srt` em `07_Inbox_Agent/Transcricoes/`.

1. Aplica resoluções de triagem feitas à mão desde o último scan.
2. Calcula o SHA-256 do arquivo; se já processado, apenas arquiva e segue.
3. Gemini estrutura a ata em 17 campos fixos (ver §4).
4. Resolve a data da reunião (nome do arquivo, depois indício explícito, depois data de
   modificação do arquivo).
5. `EntityResolver` decide, por entidade citada: vira wikilink direto, vira candidato
   pendente, ou é descartada.
6. Cria a nota de reunião e anexa a ata abaixo do marcador `<!-- transcript:<sha12> -->`.
7. Grava candidatos pendentes no banco e arquiva o `.txt` cru.
8. Gera focos no Cockpit para itens de ação e prazos próximos.
9. Escreve a nota de triagem e avisa no Telegram.

Ordem crítica, recuperável em caso de crash: primeiro o bloco com o marcador na nota,
depois arquivar o arquivo cru, depois a linha no banco. O pipeline nunca cria nota nova
de pessoa/projeto/decisão/recurso sozinho — só vincula quando o match é confiável. Se o
Gemini falhar em estruturar a ata, ela sai como esqueleto sinalizado, nunca some em
silêncio.

### Ingestão de diário falado (`process_pending_diary`, `core.py:573`)

Mesmo job do item anterior, rodando logo em seguida (um job só evita duas passagens
concorrentes na mesma tabela de candidatos). A transcrição de voz é produzida por um
pipeline externo, fora do escopo do rysOS, que deposita o `.txt` na pasta que o rclone
mapeia para `07_Inbox_Agent/Diario/`.

O fluxo espelha o de reunião (mesmo SHA, mesmo marcador, mesmo `EntityResolver`), com
três diferenças: a fala entra na nota como citação verbatim (nunca reescrita); a data
da entrada nunca aciona renomeação de nota (`09_Journal` é append-only por data); e
perguntas em aberto captadas pelo modelo aparecem na nota mas ainda não têm um fluxo de
resposta — isso é trabalho futuro.

### Triagem: nota → resolução → aplicação

A nota de triagem lista, linha por linha, cada entidade sem match automático. Você edita
a linha `resolução:` diretamente no Obsidian:

| Você escreve | Ação |
| --- | --- |
| `NOVO` / `REGISTRAR` / `CRIAR` | cria a nota e vincula na ata |
| `= Nome Exato` | vincula a uma nota existente (match exato pelo nome do arquivo) |
| `IGNORAR` / `não` | descarta o candidato |
| data `AAAA-MM-DD` (na seção de data) | renomeia a nota de reunião |
| `confirmar` | mantém a data já detectada |

`apply_pending_triagem` roda a cada scan e usa o banco — não o arquivo — como fonte da
verdade sobre o que ainda está pendente: nota sem candidato pendente vai para o
arquivo; nota editada aplica cada resolução e mantém aberta a nota se ainda sobrar
algo. A mesma pessoa ou projeto pendente em várias transcrições recebe a mesma
resolução automaticamente, para você não repetir a decisão.

### Telegram

Mensagens de texto, áudio ou imagem chegam ao bot. Um classificador leve decide a
intenção antes de qualquer ação: uma pergunta nunca vira nota, uma captura de baixa
confiança é respondida em vez de virar rascunho, frases como "boleto pago" fecham um
foco do Cockpit direto. Comandos fixos (`/cockpit`, `/agenda`, `/decisoes`, `/triagem`,
`/recap`, `/sync`, `/dedup`, `/cancelar`, `/ajuda`) não passam pelo modelo. Um rascunho
de nota em andamento expira sozinho (TTL e limite de turnos) para nunca travar uma
conversa nova.

Estilo de resposta é regra fixa: sem travessão decorativo, sem markdown de negrito
(o Telegram já renderiza HTML), um parágrafo por pergunta, sem menu de opções que
ninguém pediu.

### Recap noturno

Cron 19:00. Envia um balanço do dia no Telegram a partir do Cockpit.

---

## 4. Banco de dados (SQLite, `data/rysos.db`)

12 tabelas em `src/rysos/db/models.py`. `init_db()` só roda `create_all` — não existe
migração nem `ALTER` em tabela já existente; uma tabela nova nasce no próximo boot.

| Tabela | Chave | Papel |
| --- | --- | --- |
| `sync_logs` | id | auditoria de cada rodada de sync |
| `processed_emails` | id da mensagem Gmail | dedup de e-mail processado |
| `processed_events` | conta + id do evento | dedup de evento de calendário |
| `processed_transcripts` | SHA-256 do arquivo | dedup de transcrição de reunião |
| `processed_diary_entries` | SHA-256 do arquivo | dedup de entrada de diário |
| `entity_candidates` | id, único por (transcrição, tipo, forma normalizada) | pessoa/projeto/decisão/recurso citado sem match automático, aguardando triagem |
| `decisions` | `DEC-AAAA-NNNNN` | registro executivo de decisões |
| `inbox_agent_items` | id | itens de triagem legados |
| `token_usage_logs` | id | custo real de cada chamada ao Gemini |
| `telegram_draft_sessions` | id do usuário | rascunho de nota em andamento no chat |
| `adaptive_concepts` | id | mapeamentos aprendidos (termo → categoria/área) |
| `adaptive_feedback_logs` | id | auditoria de classificação de intenção |

Duas ressalvas que importam para quem for consultar o banco direto:

- `token_usage_logs` é escrita por dois caminhos: o modelo SQLAlchemy declara
  `created_at` em UTC, mas o gravador real (`observability.py:record_ai_token_usage`)
  usa `sqlite3` cru com `CURRENT_TIMESTAMP`, que é hora local. Consultas devem assumir
  o esquema do `observability.py`, não o do modelo.
- `created_at` em toda tabela é UTC (`utc_now()`), mas `resolved_at` em
  `entity_candidates` é gravado com `datetime.now()` local (`triagem.py:513`,
  `entity_review.py:38`) — há 3 horas de diferença contra qualquer comparação feita em
  UTC. Normalize antes de filtrar por essa coluna.

**A ata de 17 campos.** `summarize_meeting_transcript` e `summarize_diary_entry` sempre
devolvem a mesma forma (`_coerce_minutes`, `gemini.py:98`): título, tipo de reunião,
indício de data, área PARA sugerida, listas de pessoas, organizações, projetos,
decisões, perguntas em aberto, itens de ação, recursos, riscos, dados financeiros,
prazos, próximas reuniões, tags, glossário e um resumo em tópicos. Todo consumidor
downstream pode confiar que essas chaves sempre existem.

---

## 5. Contratos de idempotência

Estas regras evitam nota duplicada, item preso na fila para sempre, ou fato
sobrescrito. Quebrar qualquer uma delas quebra o sistema de um jeito difícil de notar.

- **PK por SHA-256** em `processed_transcripts`/`processed_diary_entries`: um re-drop
  do mesmo arquivo, ou um crash no meio do pipeline, nunca duplica a ata.
- **Marcador `<!-- transcript:<sha12> -->` na nota**: é a verdade de idempotência do
  conteúdo, compartilhada de propósito entre reunião e diário, e fixada no código de
  três lugares (`patch_transcript_entity`, `entity_apply`, `apply_pending_triagem`).
  Um marcador diferente quebraria essa costura.
- **Marcador `<!-- triagem:<sha12> -->`**: uma nota que já tem o marcador não é
  reescrita — o que você já marcou sobrevive a um novo scan.
- **Ordem fixa em cada pipeline de ingestão**: bloco com marcador na nota, depois
  arquivar o arquivo cru, depois a linha no banco. Um crash entre essas etapas é
  recuperável sem duplicar nada.
- **O banco decide o que está pendente**, não o arquivo. `apply_pending_triagem`
  arquiva ou mantém aberta uma nota de triagem pela contagem real de candidatos
  pendentes.
- **Dedup semântico, não por id de provedor**: notas de reunião e lembretes de boleto
  deduplicam pela combinação de fatos do mundo real (data + participantes, ou
  vencimento + credor) — nunca pelo id que o Google ou o Gmail atribuem, que muda entre
  sincronizações.
- **Criação de nota de dimensão é create-only**: `ensure_person_note` e
  `ensure_project_note` nunca sobrescrevem uma nota existente; `enrich_person_note` só
  preenche campo vazio.

---

## 6. Configuração

`src/rysos/config.py` lê `.env` na raiz do projeto via `pydantic-settings`. Valores
abaixo são os defaults do código; o `.env.example` traz todas as variáveis comentadas.

| Variável | Default | Notas |
| --- | --- | --- |
| `OBSIDIAN_VAULT_PATH` | `~/Obsidian/rysos-vault` | pasta do cofre; `~` é expandido |
| `GEMINI_API_KEY` | nenhum | sem ela, toda função de IA cai num fallback heurístico ou esqueleto |
| `GEMINI_MODEL` / `GEMINI_MODEL_LITE` | `gemini-3.5-flash-lite` / `gemini-3.1-flash-lite` | o segundo atende as chamadas operacionais mais baratas |
| `USER_NAME` / `USER_ROLE` | vazio / `executivo` | entram nos prompts e nas mensagens do bot |
| `USER_EMAILS` | vazio | suas contas Google, separadas por vírgula; reconhecem você entre os participantes |
| `PERSONAL_EMAIL_DOMAINS` | `gmail.com`, `outlook.com`, ... | contas nesses domínios são "pessoais" (regra de boletos); as demais, "corporativas" |
| `TIMEZONE` | `America/Sao_Paulo` | fuso dos eventos criados na Agenda |
| `GOOGLE_CREDENTIALS_PATH` / `GOOGLE_TOKEN_PATH` | `credentials.json` / `token.json` | o token legado migra para `tokens/token_<email>.json` no boot |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | a interface web não tem autenticação; não exponha direto na internet |
| `MORNING_BRIEF_TIME` / `EVENING_RECAP_TIME` | `07:00` / `19:00` | horário local, formato `HH:MM` |
| `TRANSCRIPT_SCAN_INTERVAL_MIN` | `15` | intervalo do job de varredura |
| `TRANSCRIPT_MAX_CHARS` / `DIARY_MAX_CHARS` | `200000` | truncagem de segurança |
| `DIARY_MIN_CHARS` | `120` | piso mínimo para considerar uma entrada real |
| `TELEGRAM_BOT_TOKEN` | nenhum | ausente desliga o bot |
| `TELEGRAM_ALLOWED_USER_IDS` | vazio | IDs autorizados, separados por vírgula. Vazio não autoriza ninguém |
| `TELEGRAM_CHAT_ID` | nenhum | chat que recebe o resumo da manhã e o balanço da noite |
| `DATABASE_URL` | `sqlite+aiosqlite:///<projeto>/data/rysos.db` | |

Segredos nunca vão para o git nem para documentação: `.env`, `credentials.json`,
`token.json`, `tokens/`. Já estão no `.gitignore`.

---

## 7. Deploy e testes

Serviço systemd de usuário (sem sudo), Docker e acesso remoto seguro: veja
[`docs/deploy.md`](docs/deploy.md). No boot, o app inicializa o banco e a estrutura do
cofre, sobe o agendador, limpa rascunhos de Telegram esquecidos e liga o long-polling do
bot, se configurado.

### Testes

```bash
uv sync --extra dev          # instala também pytest e plugins
uv run pytest -q
```

`tests/conftest.py` monta um cofre temporário e um banco SQLite isolados a cada teste, com
uma identidade de dono fictícia; nenhum teste toca o seu cofre real. Mudanças devem ser
aditivas: as suítes existentes precisam continuar verdes.

---

## 8. Regras para estender o sistema

1. Sem migração de schema: pode criar tabela nova, nunca adicionar coluna a uma
   existente.
2. A estrutura da nota é sempre código. Um tipo de nota novo é um template Jinja2 mais
   um método `create_*`/`ensure_*` em `VaultManager` — o modelo só escreve o corpo em
   prosa.
3. Nunca inventar. Entrada vazia ou ambígua vira uma afirmação do fato ("convite sem
   pauta"), nunca uma suposição.
4. Reutilize o marcador `transcript:` em qualquer pipeline novo que anexe blocos a uma
   nota — um marcador diferente quebra o parse-back em silêncio.
5. Toda chamada ao Gemini é síncrona e bloqueia o event loop; sempre envolva em
   `asyncio.to_thread`.
6. Notas de dimensão (pessoa, projeto) são create-only: nunca sobrescrever o que já
   existe.
7. Área é sempre um dos 4 valores fixos, resolvido via `_resolve_area`.
8. Um scan novo entra no job existente, não num `add_job` separado — dois jobs
   concorrentes na mesma tabela de candidatos causam corrida.
9. Ordem fixa do pipeline de ingestão: bloco com marcador, depois arquivar o cru,
   depois a linha no banco.
10. O cofre não é git. Editar um arquivo lá não é uma mudança versionada.

---

## 9. Limitações conhecidas

- O sistema é monousuário e em português do Brasil: prompts, notas geradas e mensagens do
  bot estão em pt-BR. O texto sobre fuso horário em alguns prompts assume `America/Sao_Paulo`.
- A interface web não tem autenticação; use-a só em localhost ou atrás de VPN.
- `scripts/` reúne migrações pontuais que serviram a um cofre específico; leia cada uma
  antes de rodar (todas têm modo de simulação, salvo indicação no cabeçalho).
- Match de projeto é pelo nome do arquivo, não pelo `title:` do frontmatter — renomear
  o arquivo muda a chave de match.
- Em `09_Journal`, um dia com várias entradas de diário pode ter um nome resolvido numa
  entrada antiga reescrevendo uma linha igual numa entrada mais nova do mesmo dia. Ficou
  para uma próxima fase do diário.
- Editar o título em negrito de uma linha de triagem quebra o parse-back em silêncio —
  a resolução deixa de ser associada a qualquer candidato.
- Uma pessoa citada uma única vez, sem cargo ou organização, é descartada por design
  (filtro anti-ruído de transcrição) e nunca vira candidato de triagem.
- Uma falha de rede do rclone pode abortar um tick de varredura; converge sozinho nos
  próximos ticks, sem perda de dados.
- A transcrição de áudio do diário é externa ao rysOS — um pipeline separado deposita o
  `.txt` na pasta observada. (Notas de voz curtas no Telegram são transcritas pelo
  próprio bot, caminho diferente.)
- Perguntas em aberto captadas no diário aparecem na nota, mas ainda não têm um fluxo
  de resposta.
- O `/dedup` de vault é hoje um relatório apenas de leitura mais um primitivo de fusão
  (`VaultManager.merge_notes`) para o caso pessoa/projeto/recurso; ainda falta o fluxo
  de aprovação pelo Telegram e o tratamento de decisões e menções de diário, que são
  formas de fusão diferentes.

---

## 10. Mapa de arquivos

| Arquivo | Responsabilidade |
| --- | --- |
| `src/rysos/core.py` | `RysOSCore`: sync diário, ingestão de transcrição e diário |
| `src/rysos/config.py` | configuração e criação de diretórios |
| `src/rysos/identity.py` | dono do assistente (nome, e-mails, tipo de conta) e substituição nos prompts |
| `src/rysos/db/models.py` | as 12 tabelas |
| `src/rysos/db/database.py` | engine async, `init_db`, sessão |
| `src/rysos/scheduler/agent_scheduler.py` | os 3 jobs do APScheduler |
| `src/rysos/ai/gemini.py` | cliente Gemini e normalização das respostas |
| `src/rysos/ai/prompts.py` | todos os prompts |
| `src/rysos/ai/classifier.py` | roteamento de intenção no Telegram |
| `src/rysos/ai/learning.py` | conceitos aprendidos, taxonomia dinâmica |
| `src/rysos/connectors/gmail.py`, `calendar.py` | ingestão Google multi-conta |
| `src/rysos/connectors/transcripts.py` | leitura de transcrição (reunião ou diário) |
| `src/rysos/connectors/entity_resolver.py` | decide vincular, sugerir ou descartar |
| `src/rysos/connectors/entity_apply.py` | aplica resoluções de triagem |
| `src/rysos/vault/manager.py` | toda escrita no cofre, incluindo merge de `/dedup` |
| `src/rysos/vault/templates.py` | templates Jinja2 |
| `src/rysos/vault/triagem.py` | escrita e leitura das notas de triagem |
| `src/rysos/vault/dedup.py` | varredura de integridade do cofre (leitura) |
| `src/rysos/vault/dedup_triage.py` | contexto para avaliação de fusões por IA |
| `src/rysos/telegram/bot.py` | cliente da API do Telegram |
| `src/rysos/telegram/handlers.py` | roteamento de mensagem e comando |
| `src/rysos/telegram/entity_review.py` | cards de triagem no Telegram |
| `src/rysos/web/app.py`, `routes.py` | FastAPI |
| `src/rysos/auth/google_auth.py` | OAuth multi-conta |
| `src/rysos/observability.py` | custo e uso de tokens |
| `src/rysos/cli.py` | CLI |
| `tests/` | suíte pytest |
