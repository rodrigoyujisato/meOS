# Design — Simplificação do ciclo de vida da Triagem

Estado: **implementado** (`triagem.py`, `entity_review.py`, testes). Escrita antes de
mexer no código, a pedido.

Idioma: pt-BR, seguindo a conversa que originou o documento.

---

## 1. Problema

### 1.1 Sintomas

- **Item nunca expira.** Não há TTL, não há varredura de item velho, não há
  arquivamento. Uma linha de Triagem fica `pending` para sempre enquanto ninguém
  resolver.
- **Duas superfícies que se sobrepõem.** A nota em `07_Inbox_Agent/Triagem/<reunião>.md`
  e o card no Telegram cobrem o mesmo trabalho com regras próprias.
- **Duas fontes de verdade.** O `status:` no frontmatter da nota e o
  `EntityCandidate.status` no SQLite podem discordar.
- **Resolução parcial fecha a nota inteira.** `_rewrite_resolved`
  (`triagem.py:276`) faz `_STATUS_PENDING_RE.sub("status: resolved", …, count=1)`
  sempre que houve **pelo menos uma** anotação (`apply_pending_triagem`,
  `triagem.py:452` — `if annotations:`). As linhas não editadas continuam
  `pending` no banco: somem da nota (agora `resolved`), mas seguem vindo no
  `/triagem`, que lê o banco e ignora o frontmatter (`push_pending_reviews`,
  `entity_review.py:136`).
- **Não dá para reabrir.** Depois de `resolved`, `apply_pending_triagem` pula a
  nota (`_STATUS_PENDING_RE.search(text)`, `triagem.py:310`); novas edições nas
  linhas `resolução:` são ignoradas no scan.
- **Nota resolvida fica no inbox.** Nunca é movida para `08_Archive/` nem
  apagada — acumula em `07_Inbox_Agent/Triagem/`.

### 1.2 Causa-raiz

Duas superfícies e duas fontes de verdade sem um dono declarado. Na prática o
**banco já é a verdade operacional**: o `/triagem` só olha ele, e
`apply_resolution` age sobre ele. Mas a nota carrega um `status:` próprio que só
o parse-back atualiza, e de forma grosseira (tudo-ou-nada). O frontmatter finge
ser autoritativo sem ser.

---

## 2. Modelo-alvo

1. **O banco é a única fonte de verdade** do que está aberto. O `status:` da nota
   é derivado da contagem de `EntityCandidate pending` para aquele
   `transcript_sha`, nunca a chave de decisão.
2. **A nota é a única superfície de ação.** O Telegram vira notificação + link
   ("3 reuniões com pendências → abra a nota"), sem botões.
3. **Resolução é por item.** Linha resolvida é anotada com o desfecho na hora. A
   nota só "fecha" quando não sobra nenhum `EntityCandidate pending` para o sha.
4. **Fechou, sai do inbox.** Nota sem pendência → `08_Archive/Triagem/<stem>.md`
   (ou apagada — ver §5). O inbox só mostra o que está aberto.

---

## 3. Mudanças

### 3.1 `apply_pending_triagem` — `triagem.py:297`

- **Gate por banco, não por texto.** Trocar o filtro `_STATUS_PENDING_RE.search(text)`
  (`:310`) por "existe `EntityCandidate` com esse `sha_prefix` e `status ==
  'pending'`". Assim editar uma nota já semi-resolvida volta a funcionar, e a
  reabertura deixa de depender de mexer no frontmatter na mão.
- **Reconsultar o banco depois de aplicar.** Se ainda há `pending` para o sha:
  reescrever a nota anotando só as linhas aplicadas (`✅ …` / `⚠️ …`) e manter
  `status: pending`. Se zerou: chamar `archive_triagem_note` (§3.4).
- **Remover o flip incondicional** em `_rewrite_resolved` (`:279`). O `status:`
  passa a ser escrito pela função que sabe a contagem real, não por um `sub`
  cego.

### 3.2 Render da nota — `write_triagem_note` (`triagem.py:135`)

- `status: pending` no nascimento continua ok; deixa de ser lido como chave de
  decisão em qualquer lugar.
- **Remover o bloco `## ⚡ Ações em massa` da nota** (era `_bulk_block_lines`,
  renderizado logo após o cabeçalho). Primeiro foi movido para o fim; depois
  Rafael apontou que é duplicidade — tudo que os 4 macros fazem já é alcançável
  editando as linhas `resolução:` uma a uma, o `seed.xlsx` mata o cold-start que
  o justificava, e `aceitar sugestões fortes` vincula sem revisão (o oposto do
  motivo do candidato existir). A superfície passa a ser só a lista item a item.
  `_bulk_block_lines` e o parse de `SIM` em `read_triagem_resolutions` ficam para
  o `migrate_entity_candidates.py` e notas escritas antes da mudança. Fecha
  [[triagem-note-ux-open-thread]].
- Manter o gesto de resolução como está — linha `resolução:` preenchida
  (`NOVO` / `IGNORAR` / `=Nome exato` / data / `SIM`). O `- [x]` segue cosmético;
  ver decisão aberta §5.4.

### 3.3 Telegram — `entity_review.py`

- `push_meeting_review` (`:99`, card automático pós-ingestão) e
  `push_pending_reviews` (`:136`, `/triagem`): **manter o texto-resumo, remover o
  `inline_keyboard`** de ações em massa (`_card`, `:68`–`:94`). No lugar, uma
  linha "Edite a nota de Triagem" + caminho/nome da nota.
- Manter o handler de callback `entgrp:` só por compatibilidade com mensagens
  antigas (já é a intenção declarada no módulo).
- `/triagem` (`handlers.py:358`) segue reenviando o resumo, agora só informativo.
- **Consequência:** as macros em massa não somem — continuam disparadas pela
  `resolução: SIM` numa linha do bloco `## ⚡` (já suportado, `_BULK_PHRASES`
  `triagem.py:52`, parse em `read_triagem_resolutions` `:257`–`:261`). Só deixam
  de ter botão.

### 3.4 Arquivamento — novo helper

- `archive_triagem_note(vault, note_path)` em `triagem.py`: move para
  `08_Archive/Triagem/<stem>.md`, grava `status: resolved` + um rodapé com data e
  resumo (`N itens resolvidos`). Idempotente (se o destino existe, sobrescreve
  com o conteúdo final). `08_Archive` já é pasta oficial (`config.py:120`,
  `manager.py:61`).
- Chamado por `apply_pending_triagem` quando a contagem de `pending` zera.

---

## 4. Migração

- **Notas já `status: resolved`** em `07_Inbox_Agent/Triagem/`: `apply_pending_triagem`
  agora as varre para `08_Archive/Triagem/` no primeiro run (bloco de legacy-sweep no
  topo do loop). Nenhum passo manual.
- **`scripts/migrate_entity_candidates.py`** (backlog de `EntityCandidate` órfão):
  **não alterado**. Ele ainda escreve `status: resolved` / `status: pending` em
  `07_Inbox_Agent/Triagem/`; o legacy-sweep arquiva as `resolved` no scan seguinte,
  que é o comportamento desejado. Segue como follow-up alinhar o `_render` dele
  (bloco `## ⚡` ainda no topo).
- **Sem mudança de schema** no banco.

---

## 5. Decisões (resolvidas na implementação)

1. **Arquivar**, não apagar — preserva a trilha de auditoria. `_archive_triagem_note`
   move para `08_Archive/Triagem/`, flipa `status: resolved` e anexa rodapé datado
   (`_Triagem concluída em AAAA-MM-DD. N item(ns) resolvidos._`).
2. **Subpasta** `08_Archive/Triagem/` (`_ARCHIVE_SUBDIR`), não plano.
3. **Notificação = texto + caminho da nota**, sem `inline_keyboard`. `_card` agora
   devolve `str | None`. O handler `entgrp:` fica só para cards antigos.
4. **Só a `resolução:` preenchida** dispara. `- [x]` segue cosmético — uma regra a
   menos.

### 5.1 Ajuste não previsto na proposta — retry de `⚠️`

`_rewrite_resolved(text, annotations, *, resolved)` passou a distinguir:
`✅`/`—` **substituem** a linha `resolução:` (item fechado, sem retry); `⚠️` é
**anexado** numa linha própria e a `resolução:` do usuário fica intacta. Como a nota
agora é re-scaneada enquanto tiver pendência, um `= Nome` cujo alvo ainda não existe
é **re-tentado** no próximo scan assim que a nota-alvo for criada — antes o `⚠️`
sobrescrevia o pedido e a nota congelava em `resolved`. Linhas `⚠️`/`✅`/`—` órfãs são
removidas e regeradas a cada rewrite (`_ANNOTATION_LINE`), então uma nota que
continua `pending` não acumula anotações.

### 5.2 Bloco `## ⚡` removido da nota

Primeira versão deste refactor moveu o bloco para o fim da nota. Numa segunda
passada Rafael pediu para tirá-lo de vez (ver §3.2): é duplicidade da lista item
a item, e o macro mais útil (`aceitar sugestões fortes`) é o mais arriscado.
`write_triagem_note` não renderiza mais o bloco; `_bulk_block_lines` e o parse de
`SIM` continuam vivos só para o `migrate_entity_candidates.py` e notas legadas.
Coberto por `test_parseback_legacy_bulk_block_still_parses`.

---

## 6. Fora de escopo

- O `EntityResolver` e o critério do que vira candidato.
- TTL / auto-dismiss de item velho. Possível follow-up: `EntityCandidate pending`
  há mais de N dias → lembrete no digest diário, nunca descarte automático.
