"""Obsidian Vault templates with YAML frontmatter, Dataview queries, and Graph links."""

DAILY_COCKPIT_TEMPLATE = """---
id: DAILY-{{ date_str }}
title: "Cockpit Diário: {{ date_formatted }}"
type: daily
date: {{ date_str }}
priority: alta
status: active
tags:
  - type/daily
  - cockpit
---

# 🚀 Cockpit Diário — {{ date_formatted }}

## 🎯 Focos de Alta Prioridade do Dia
{% for focus in high_priority_foci %}
- [ ] **{{ focus.title }}** ({{ focus.area }}) — {{ focus.description }}
{% else %}
- [ ] *Defina os 3 focos principais do dia*
{% endfor %}

---

## 📅 Agenda & Reuniões de Hoje
{% for event in calendar_events %}
### ⏰ {{ event.start_time.strftime('%H:%M') }} - {{ event.end_time.strftime('%H:%M') }}: [[04_Meetings/{{ event.meeting_note_name }}|{{ event.summary }}]]
- **Participantes:** {{ event.attendees_str }}
{% if event.meet_link %}- **Link:** [Google Meet]({{ event.meet_link }}){% endif %}
- **Contexto & Pauta:** {{ event.context_brief }}
{% else %}
*Nenhum compromisso registrado para hoje.*
{% endfor %}

---

## 📬 E-mails Críticos & Ações Necessárias
{% for email in actionable_emails %}
- [ ] **De:** {{ email.sender }} | **Assunto:** {{ email.subject }}
  - **Ação Sugerida:** {{ email.suggested_action }}
  - **Prioridade:** `{{ email.priority }}`
{% else %}
*Nenhum e-mail crítico pendente de ação.*
{% endfor %}

---

## 💡 Decisões & Pendências a Desbloquear
{% for dec in pending_decisions %}
- [ ] [[03_Decisions/{{ dec.note_name or dec.id }}|{{ dec.id }}: {{ dec.title }}]] (`{{ dec.priority }}`){% if dec.status == "bloqueado" %} 🔒 `bloqueado`{% endif %}
{% else %}
*Nenhuma decisão crítica pendente no radar.*
{% endfor %}

---

## 🌙 Fechamento do Dia (19:00 Recap)
- **Vitórias do dia:**
  -
- **O que ficou para amanhã:**
  -
- **Insights & Notas:**
  -

## ✍️ Notas Livres do Dia
<!-- notas-livres:start -->

<!-- notas-livres:end -->
"""

WEEKLY_REVIEW_TEMPLATE = """---
id: WEEKLY-{{ year }}-W{{ week_num }}
title: "Revisão Semanal: {{ year }}-W{{ week_num }}"
type: weekly
year: {{ year }}
week: {{ week_num }}
tags:
  - type/weekly
  - review
---

# 📊 Revisão Estratégica Semanal — {{ year }}-W{{ week_num }}

## 🎯 Status dos Projetos de Alta Prioridade
```dataview
TABLE status, deadline, area
FROM "01_Projects"
WHERE priority = "alta" AND status != "done" AND status != "archived"
SORT deadline ASC
```

## ⚖️ Decisões Tomadas nesta Semana
```dataview
TABLE decision_date, status, area
FROM "03_Decisions"
WHERE file.mtime >= date(today) - dur(7 days)
SORT file.mtime DESC
```

## 🧭 Auditoria de Tempo & Alocação por Área
- **Negócios & Governança:** 
- **Saúde & Vitalidade:** 
- **Patrimônio & Finanças:** 
- **Família & Vida Pessoal:** 
"""

PROJECT_TEMPLATE = """---
id: PROJ-{{ proj_id }}
title: "{{ title }}"
type: project
area: "[[02_Areas/{{ area_file }}|{{ area_title }}]]"
front: {{ front }}
priority: {{ priority }}
status: {{ status }}
deadline: {{ deadline }}
stakeholders:
{% for p in stakeholders %}
  - "[[05_People/{{ p }}|{{ p }}]]"
{% endfor %}
related_decisions:
{% for d in related_decisions %}
  - "[[03_Decisions/{{ d }}|{{ d }}]]"
{% endfor %}
tags:
  - type/project
  - priority/{{ priority }}
  - status/{{ status }}
created_at: {{ created_at }}
---

# 🚀 Projeto: {{ title }}

> **Status:** `{{ status }}` | **Prioridade:** `{{ priority }}` | **Prazo:** `{{ deadline }}`  
> **Área:** [[02_Areas/{{ area_file }}|{{ area_title }}]] (Frente: `{{ front }}`)

---

## 🎯 Objetivo & Resultado Chave (Outcome)
{{ outcome_description }}

---

## 📋 Próximas Ações
- [ ] 

---

## 🧠 Registro de Contexto & Reuniões

---

## ⚖️ Decisões Vinculadas

---

## 📚 Recursos & Referências
"""

AREA_NEGOCIOS_MOC_TEMPLATE = """---
id: AREA-NEGOCIOS-GOVERNANCA
title: "Negócios & Governança"
type: area
pillar: profissional
tags:
  - type/area
  - moc
  - negocios
---

# 🏛️ Área: Negócios & Governança (MOC)

Painel central de governança, estratégia e operações. Projetos e decisões distribuem-se dinamicamente nas 3 frentes de atuação abaixo.

---

## 💼 1. Frente: Estratégia, M&A e Conselho (Board)
Projetos e iniciativas voltadas para direção executiva, governança societária, expansão inorgânica e parcerias.

```dataview
TABLE priority, status, deadline
FROM "01_Projects"
WHERE front = "estrategia_ma" AND status != "archived"
SORT priority DESC, deadline ASC
```

---

## 🚀 2. Frente: Operações & Entregas de Clientes
Projetos de execução de contratos, satisfação de clientes, eficiência de times e entrega de serviços.

```dataview
TABLE priority, status, deadline
FROM "01_Projects"
WHERE front = "operacoes_clientes" AND status != "archived"
SORT priority DESC, deadline ASC
```

---

## 👥 3. Frente: Gente, Liderança & Cultura
Iniciativas de gestão executiva de pessoas, alinhamento cultural, liderança e contratações-chave.

```dataview
TABLE priority, status, deadline
FROM "01_Projects"
WHERE front = "pessoas_cultura" AND status != "archived"
SORT priority DESC, deadline ASC
```

---

## ⚖️ Diário de Decisões de Negócios
```dataview
TABLE priority, status, decision_date
FROM "03_Decisions"
WHERE area = "Negocios_e_Governanca"
SORT decision_date DESC
LIMIT 10
```
"""

AREA_GENERIC_TEMPLATE = """---
id: AREA-{{ area_id }}
title: "{{ title }}"
type: area
pillar: {{ pillar }}
tags:
  - type/area
  - {{ tag }}
---

# 🌿 Área: {{ title }}

Responsabilidade contínua e padrões de excelência pessoal.

---

## 🎯 Padrões & Objetivos Contínuos
{{ standards_description }}

---

## 🚀 Projetos Ativos nesta Área
```dataview
TABLE priority, status, deadline
FROM "01_Projects"
WHERE contains(area, this.file.name) AND status != "archived"
SORT priority DESC, deadline ASC
```

---

## 📚 Recursos & Referências Vinculadas
```dataview
LIST
FROM "06_Resources"
WHERE contains(file.outlinks, this.file.link)
```
"""

DECISION_TEMPLATE = """---
id: {{ decision_id }}
title: "{{ title }}"
type: decision
status: {{ status }} # [em_analise, decidido, bloqueado, descartado]
priority: {{ priority }} # [alta, baixa]
area: "[[02_Areas/{{ area_file }}|{{ area_title }}]]"
related_projects:
{% for p in related_projects %}
  - "[[01_Projects/{{ p }}|{{ p }}]]"
{% endfor %}
decision_date: {{ decision_date }}
tags:
  - type/decision
  - priority/{{ priority }}
  - status/{{ status }}
---

# ⚖️ Decisão: {{ title }} (`{{ decision_id }}`)

> **Status:** `{{ status }}` | **Prioridade:** `{{ priority }}` | **Data:** `{{ decision_date }}`  
> **Área:** [[02_Areas/{{ area_file }}|{{ area_title }}]]

---

## 🔍 1. Contexto & Problema
{{ context }}

## 🎯 2. Premissas & Critérios
{{ assumptions }}

## ⚖️ 3. Alternativas Analisadas & Trade-offs
| Opção | Prós | Contras | Riscos |
| :--- | :--- | :--- | :--- |
| **Opção A** | | | |
| **Opção B** | | | |

## 🏁 4. Decisão Tomada & Racional
{{ outcome }}

## 📅 5. Data de Revisão & Resultados Esperados
- **Revisar em:** `{{ review_date }}`
- **Critério de Sucesso:** {{ success_metric }}
"""

MEETING_TEMPLATE = """---
id: MEET-{{ date_str }}-{{ slug }}
event_id: "{{ event_id }}"
title: "{{ summary }}"
type: meeting
date: {{ date_str }}
time: "{{ start_time_str }} - {{ end_time_str }}"
priority: {{ priority }}
area: "{{ area_link }}"
attendees:
{%- for a in attendees %}
  - "[[05_People/{{ a }}|{{ a }}]]"
{%- endfor %}
related_projects:
{%- for p in related_projects %}
  - "[[01_Projects/{{ p }}|{{ p }}]]"
{%- endfor %}
topics:
{%- for t in topics %}
  - {{ t }}
{%- endfor %}
tags:
  - type/meeting
  - priority/{{ priority }}
---

# 🎙️ Reunião: {{ summary }}

> **Data:** `{{ date_str }}` ({{ start_time_str }} - {{ end_time_str }})  
> **Participantes:** {% for a in attendees %}[[05_People/{{ a }}|{{ a }}]]{% if not loop.last %}, {% endif %}{% endfor %}  
{% if meet_link %}> **Link da Reunião:** [Entrar no Meet]({{ meet_link }}){% endif %}

---

## 🎯 Pauta & Contexto Prévio (rysOS Brief)
{{ context_brief }}

---

## 📝 Notas & Discussão
- 

---

## ✅ Ações & Follow-ups Decididos
- [ ] 
"""

IDEA_TEMPLATE = """---
id: IDEA-{{ idea_id }}
title: "{{ title }}"
type: resource
resource_type: idea
tags:
  - type/resource
  - ideia
created_at: {{ created_at }}
---

# 💡 {{ title }}

## 🔭 Visão Geral
{{ overview or "A detalhar." }}

## 🧠 Racional & Premissas
{{ rationale or "A detalhar." }}

## 🚀 Próximos Passos
{% for step in next_steps %}
- [ ] {{ step }}
{% else %}
- [ ] Definir próximos passos
{% endfor %}
"""

FRAMEWORK_TEMPLATE = """---
id: FRAMEWORK-{{ fw_id }}
title: "{{ title }}"
type: resource
resource_type: framework
tags:
  - type/resource
  - framework
created_at: {{ created_at }}
---

# 📐 {{ title }}

## 🎯 Objetivo & Quando Utilizar
{{ description or "A detalhar." }}

## 🧩 Componentes & Fases
{% for c in components %}
- {{ c }}
{% else %}
- Descrever os componentes do método
{% endfor %}
"""

RESOURCE_TEMPLATE = """---
id: RESOURCE-{{ resource_id }}
title: "{{ title }}"
type: resource
resource_type: generic
area: "{{ area_link }}"
related_projects:
{% for p in related_projects %}
  - "[[01_Projects/{{ p }}|{{ p }}]]"
{% endfor %}
tags:
  - type/resource
  - recurso
{% for t in tags %}  - {{ t }}
{% endfor %}created_at: {{ created_at }}
---

# 📚 {{ title }}

## Resumo
{{ summary or "A detalhar." }}

## Fonte
{{ source or "A detalhar." }}

## Área
{{ area_link or "A detalhar." }}
"""

PERSON_TEMPLATE = """---
id: PERSON-{{ slug }}
name: "{{ name }}"
type: person
organization: "{{ organization }}"
role: "{{ role }}"
email: "{{ email }}"
tags:
  - type/person
  - stakeholder
---

# 👤 {{ name }}

> **Cargo:** `{{ role }}` | **Organização:** `{{ organization }}`  
> **E-mail:** `{{ email }}`

---

## 🤝 Reuniões & Interações Recentes
```dataview
TABLE date, summary
FROM "04_Meetings"
WHERE contains(file.outlinks, this.file.link)
SORT date DESC
LIMIT 10
```

---

## 🚀 Projetos Compartilhados
```dataview
TABLE priority, status, deadline
FROM "01_Projects"
WHERE contains(stakeholders, this.file.name)
```
"""

JOURNAL_TEMPLATE = """---
id: JOURNAL-{{ date_str }}
title: "Diário — {{ date_formatted }}"
type: journal
date: {{ date_str }}
tags:
  - type/journal
  - diario
---

# 📓 Diário — {{ date_formatted }}

> Entradas faladas do dia, transcritas por uma ferramenta de voz para texto e estruturadas pelo rysOS.
> A narração bruta é preservada literalmente ("jamais inventar"); a extração é apoio.
"""

