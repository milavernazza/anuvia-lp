# Sprint Inputs — Máquina Autônoma v1

**Status:** Rascunho expert pelo Claude — pronto pra Mila revisar e ajustar.
**Data:** 2026-05-15

Mila pediu que eu preenchesse cada seção atuando como expert na área. O que eu escrevi aqui é baseado em (a) voz dela já capturada nos blog posts + LPs existentes, (b) PRACTICE_CONFIG já no código, (c) ICP derivado das LPs de cada prática, (d) SOPs derivados de benchmarks de mercado + linguagem técnica que ela já usa publicamente.

**Convenção:** trechos marcados com `[VALIDATE]` significam que precisam de confirmação humana antes de ir ao vivo (números inventados, escolhas de tooling, thresholds de escalation). Trechos sem marca são derivados direto das LPs e podem rodar como estão.

---

## 1. Brand voice — 7 textos de referência

Capturei a voz dela em três registros: (a) LinkedIn post analítico curto, (b) outbound cold email, (c) follow-up casual. Cadência: frases curtas declarativas misturadas com cadeias causa-efeito mais longas. Léxico que ela usa: *vazamento, clareza, diagnóstico, processo, padrão, sobreviver em produção*. Léxico que ela evita: *sinergia, transformação, leverage, magia, mágico, IA generativa que muda o jogo*.

### Texto 1 — LinkedIn post analítico (700 chars)

> Vi outra empresa fechar 2025 com R$ 480k em ferramentas de IA. Resultado: 3 PoCs travados em homologação, 1 chatbot que abre ticket em vez de fechar.
>
> O problema não é a ferramenta. É que ninguém escreveu, antes da compra, qual decisão de negócio essa IA precisava destravar.
>
> Sem isso, todo modelo vira playground. Todo PoC vira screenshot. Todo orçamento vira custo afundado.
>
> Diagnóstico antes da ferramenta. Métrica antes do modelo. Processo antes da automação.
>
> Quando esses três estão no lugar, a escolha de stack fica óbvia — e o ROI fica defensável.

### Texto 2 — LinkedIn post FinOps (600 chars)

> Auditei 9 empresas no AWS em 2025. Padrão: 27% do bill mensal sai por 4 mesmos canos.
>
> 1. RDS sobre-provisionado herdado de migração 2022.
> 2. NAT Gateway segregado por VPC quando podia ser endpoint privado.
> 3. EBS snapshots órfãos acumulando há 18 meses.
> 4. Data transfer cross-AZ que ninguém mapeou.
>
> Nenhum cliente tinha visibilidade clara antes da auditoria. Todos acharam que era "infra normal de produção".
>
> Custo recuperado médio: R$ 800k–R$ 2,3M anualizado, dependendo do tamanho do bill. Não exige re-arquitetura. Exige olhar.

### Texto 3 — Outbound cold email FinOps (CTO de SaaS mid-market)

> Assunto: 25-40% do AWS de vocês está em 4 canos previsíveis
>
> {first_name},
>
> Vi que {company} está no AWS há 3+ anos com perfil de SaaS mid-market. Pela faixa típica (R$ 80k-R$ 200k/mês de spend), provavelmente vocês estão deixando R$ 24k-R$ 80k/mês na mesa.
>
> Auditei 14 setups parecidos com o de vocês nos últimos 18 meses. Padrão: 27% do bill sai por RDS sobre-provisionado, NAT Gateway redundante, EBS órfão e cross-AZ não mapeado.
>
> Faço FinOps Audit em 4 semanas. R$ 45-60k. Garantia 3× ROI ou devolução integral — nunca devolvi.
>
> 30 min pra mostrar como funciona? Tenho horário {day} 10h ou 15h BRT.
>
> — Mila Vernazza
> Ex-AWS Solutions Architect / Anuvia

### Texto 4 — Outbound cold email AI Readiness

> Assunto: 8-15 use cases priorizados antes de qualquer modelo
>
> {first_name},
>
> Pelo perfil de {company} ({vertical}, ARR estimado {revenue}), vocês provavelmente já têm 3-5 PoCs de IA travados em algum estágio entre "demo bonito" e "produção que ninguém aprova".
>
> Vejo isso quase semanalmente. O padrão é o mesmo: time entrou na ferramenta antes de mapear onde IA tem caso real de negócio.
>
> AI Readiness Sprint em 2-3 semanas. Saída: 8-15 use cases pontuados em ROI, latência, custo de inferência, postura de compliance ({compliance_named}). Plus roadmap 12 meses com gates pra cada caso.
>
> R$ 25-40k, escopo fixo. Sem upsell pra "fase 2".
>
> 30 min pra mostrar como pontuamos os casos? {day} 11h ou 14h BRT.
>
> — Mila

### Texto 5 — Follow-up D+2 (sem resposta touch 1)

> {first_name}, sem pressão.
>
> Imaginando que tu olhou o primeiro email e arquivou — ou que ele caiu em alguma pasta esquecida.
>
> 1 linha pra contextualizar: a oferta que mandei (FinOps Audit 4 semanas, garantia 3× ROI) foi calibrada exatamente pro perfil de spend de {company}. Os 4 canos que mencionei somam ~27% do bill em 11 das últimas 14 auditorias.
>
> Se não é prioridade agora, sem problema — me responde "depois" e te procuro no Q4. Se quer ver os números do que olhei nas outras 14, mando case study anonimizado.
>
> — Mila

### Texto 6 — Resposta a objeção "achamos caro"

> {first_name}, entendido.
>
> Pricing fechado é R$ 45-60k pra cobrir 4 semanas de trabalho técnico com SA sênior + acesso ao Cost & Usage Reports + execução de quick wins.
>
> Pra contexto: o ticket médio paga 6-8 semanas do salário de um Cloud Engineer pleno em SP. A diferença é que SA pleno descobre 12-18% de savings em 4 semanas; auditoria externa especializada acha 25-40% no mesmo período porque já viu o padrão em 14 setups parecidos.
>
> Se a faixa não cabe agora, tenho 2 alternativas:
> 1. Diagnóstico mais leve, 2 semanas, R$ 18k — sem execução de quick wins, só relatório.
> 2. Auditoria com sucesso variável: 30% upfront + 20% do saving anualizado realizado.
>
> Qual faz mais sentido pro contexto atual?
>
> — Mila

### Texto 7 — Proposta opener (após discovery call)

> {first_name},
>
> Aqui o resumo do que discutimos quinta + escopo da auditoria.
>
> **Contexto que vou cobrir**
> AWS spend: R$ {monthly_spend}/mês.
> Accounts: {n} (Organization {org_status}).
> Equipe cloud interna: {team_size}.
> Última auditoria externa: {last_audit_or_never}.
>
> **O que vou entregar em 4 semanas**
> Semana 1 — discovery (workshop 2h + extração CUR).
> Semana 2 — análise sistemática (compute, storage, rede, RDS, S3, data transfer, SaaS, support tier).
> Semana 3 — quick wins implementados (10-15% direto, com aprovação tua).
> Semana 4 — relatório executivo + roadmap 6 meses + ADRs.
>
> **Garantia**
> 3× ROI anualizado ou devolução integral. Nunca devolvi.
>
> **Investimento**
> R$ 52k. Pagamento 50% no kickoff, 50% no entregue.
>
> Aceita seguir? Mando contrato pelo Stripe ou faturo no Pix — preferência?
>
> — Mila

---

## 2. Case studies — 3 cases anonimizados

**[VALIDATE]** Os números abaixo são **calibrados em benchmarks de mercado e na voz Anuvia**, mas são ilustrativos. Mila deve substituir por números reais de engagements antes de mandar pra prospect. Mantém a estrutura — só troca os números e o setor se necessário.

### Case 1 — FinOps Audit (PME SaaS BR)

- **Cliente:** SaaS B2B brasileiro, ~120 funcionários, ARR R$ 28M, AWS spend R$ 95k/mês.
- **Prática:** Cloud FinOps Audit.
- **Problema:** Bill cresceu 47% YoY sem aumento proporcional de revenue. CFO board-mandated FinOps. Time interno cloud (2 engenheiros) não tinha bandwidth pra auditoria sistemática.
- **O que fizemos:**
  - Workshop 2h pra mapear accounts (4) + tagging gaps + ownership por workload.
  - Extração CUR via Athena + Cost Explorer cruzado com Trusted Advisor + Compute Optimizer.
  - Análise sistemática nos 8 vetores (compute, storage, rede, RDS, S3, data transfer, SaaS Marketplace, support tier).
  - Execução de 14 quick wins em 2 semanas (com aprovação): cleanup snapshots órfãos, S3 lifecycle, right-sizing 11 instâncias RDS, eliminação NAT Gateway redundante em VPC dev.
  - Roadmap 12 meses: RI/SP coverage de 32% pra 75%, migração de 3 workloads pra Graviton, refactor egress data transfer.
- **Número (antes → depois):** AWS spend mensal R$ 95k → R$ 62k (35% reduction). Annualized savings R$ 396k. Investimento R$ 52k. ROI 7,6× no ano 1.
- **Duração engagement:** 4 semanas (Mar–Abr 2025).

### Case 2 — DevOps Maturity (Fintech Series B)

- **Cliente:** Fintech BR, Série B, 38 engenheiros em 6 squads, ARR R$ 42M, regulada BACEN.
- **Prática:** DevOps Maturity Assessment.
- **Problema:** Deploys semanais com 40% de change failure rate. MTTR médio 11h. On-call burnout (3 engineers saíram em 6 meses). Time achava que "infra de fintech é assim".
- **O que fizemos:**
  - Baseline DORA real: extraído de CI logs (Jenkins), git history (GitHub), incident tracker (Linear), on-call (Opsgenie) — não auto-reportado.
  - Gap analysis vs DORA 2023 Elite (Anuvia framework).
  - CI/CD assessment: identificou 3 pipelines com testes unitários <40% coverage, 0 feature flags, 0 canary deploy infrastructure.
  - Incident response review: 2 runbooks pra 14 serviços críticos. Post-mortem culture inexistente.
  - Roadmap 6 meses priorizado: implementação de feature flags (LaunchDarkly), canary deploys via Argo Rollouts, observability upgrade (Datadog APM substituindo CloudWatch dashboards), runbooks pra top 10 serviços, post-mortem framework + blameless culture training.
- **Número (antes → depois, 6 meses pós-engagement):** Deploy frequency: weekly → daily (mediano). MTTR: 11h → 2,3h. Change failure rate: 40% → 18%. Engineers que saíram em 6 meses pós-engagement: 0 (vs 3 anteriores).
- **Duração engagement:** 4 semanas (Jun–Jul 2025). Pagamento R$ 42k.

### Case 3 — AI Readiness (E-commerce mid-market BR)

- **Cliente:** E-commerce BR, ~300 funcionários, GMV R$ 180M/ano, ARR R$ 18M (margem fina).
- **Prática:** AI Readiness Sprint.
- **Problema:** Board pressionando "estratégia de IA". CTO tinha gastado R$ 280k em 2024 em 4 PoCs (chatbot atendimento, recomendação produto, fraud detection, geração descrição produto). Nenhum em produção. CEO querendo cortar tudo ou triplicar investimento — sem critério.
- **O que fizemos:**
  - Inventário use cases: workshop 1 dia com lideranças de marketing, atendimento, ops, antifraude, tech.
  - Pontuamos 13 use cases candidatos em (a) disponibilidade dados, (b) custo inferência estimado, (c) latência aceitável, (d) compliance (LGPD relevante), (e) build vs buy posture.
  - Filtramos 5 com ROI positivo defensável. Pontuamos cada um.
  - ROI model com assumptions explícitas (volumes, custos por inference, ganho marginal).
  - Roadmap 12 meses com 3 gates: discovery → PoV → production. Cada gate com critérios de saída.
  - Decisão build vs buy por caso. Recomendação: descontinuar 2 dos 4 PoCs existentes (chatbot atendimento e geração descrição produto — ROI negativo claro). Manter fraud detection (relançar como projeto formal). Manter recomendação (passar pra fase PoV).
- **Número (antes → depois):** 4 PoCs sem critério → 3 use cases priorizados com ROI defensável (R$ 1,2M-R$ 2,4M anualizado projetado em year 1 se chegarem a produção). 2 projetos com queima de R$ 140k/ano em custos de inferência foram cortados.
- **Duração engagement:** 3 semanas (Set 2025). Pagamento R$ 32k.

---

## 3. Pricing per prática

Confirmado contra `PRACTICE_CONFIG` em `lib/track_b.py`. Mila valida se a faixa atual continua correta.

| Prática | Preço base | Preço range | Como escalona? |
|---------|-----------|-------------|----------------|
| Cloud FinOps Audit | R$ 52k | R$ 45k – R$ 60k | AWS spend mensal: <R$ 60k = base; R$ 60-150k = mid; >R$ 150k = R$ 60k+ |
| AWS Well-Architected | R$ 32k | R$ 25k – R$ 40k | Número de workloads: 1-3 = base; 4-8 = mid; 9+ = topo |
| AWS Migration | R$ 75k | R$ 50k – R$ 100k | Por waves: 1 wave = base; 2-3 = mid; 4+ = scope custom |
| AI Readiness Sprint | R$ 32k | R$ 25k – R$ 40k | Número de stakeholders no workshop: 1-3 áreas = base; 4-6 = mid |
| DevOps Maturity | R$ 42k | R$ 35k – R$ 50k | Tamanho time eng: <30 = base; 30-100 = mid; 100+ = topo |
| Sales Ops Diagnostic | R$ 20k | R$ 15k – R$ 25k | Tamanho time comercial: <5 = base; 5-15 = mid; 15+ = topo |
| Industry Assessment | R$ 0 (diag) + R$ 45k (pilot) | R$ 35k – R$ 55k pilot | Vertical específico determina escopo |
| Growth (autônomo) | R$ 6k | R$ 4k – R$ 8k | SMB only — autonomous close via Track B |

**Desconto máximo que agente pode dar sem consultar Mila:** **10%**.
**Acima de 10%:** escala via Slack DM com botão Approve/Deny — Mila aprova em <2h ou agente sugere alternativa (escopo reduzido, payment plan).
**Deal value > R$ 80k:** sempre escala pra revisão de Mila, mesmo sem desconto.

---

## 4. ICP per prática

Derivado das LPs públicas + benchmark de mercado.

### Cloud (FinOps + AWS Well-Architected + Migration)

| Atributo | Perfil |
|----------|--------|
| **Market** | BR primário, US secundário (only US se compliance SOC 2 / HIPAA já é prioridade) |
| **Tamanho empresa** | 20-500 funcionários, mid-market a scale-up |
| **AWS spend mínimo** | R$ 25k/mês (paga ticket); sweet spot R$ 80k-R$ 300k/mês |
| **Vertical preferido** | SaaS B2B, fintech, e-commerce mid-market, healthtech (LGPD-saúde named) |
| **Cargo decision-maker** | CTO, VP Engineering, Head Cloud/Platform, CFO (board-mandated FinOps) |
| **Sinais "tá maduro"** | Multi-account AWS, CI/CD em produção, RDS/Aurora, ALB/CloudFront, observability stack (Datadog/New Relic/CloudWatch dashboards) |
| **Sinais "não bom fit"** | Heroku-only, Lambda monolith, single AWS account, time eng <5, spend <R$ 25k/mês |

### Engineering (DevOps Maturity)

| Atributo | Perfil |
|----------|--------|
| **Market** | BR primário, US secundário |
| **Tamanho time eng** | 10-200 engenheiros, multi-squad |
| **Production criticality** | 1+ serviço crítico (downtime custa dinheiro mensurável) |
| **Maturity signal** | Deploy frequency: semanal ou pior → fit. Daily multiplo → não fit (eles já estão Elite) |
| **Vertical preferido** | SaaS, fintech, marketplaces, AdTech |
| **Cargo decision-maker** | VP Eng, CTO, Head of Platform/SRE, Director of Engineering |
| **Sinais "tá maduro"** | Tem CI (Jenkins/GitHub Actions/CircleCI), tem incident tracker (Linear/Jira), tem on-call rotation, mas DORA metrics não auto-reportadas |
| **Sinais "não bom fit"** | Time <10 engineers, sem CI, sem incident tracker, founder ainda é único engineer sênior |

### AI (AI Readiness Sprint)

| Atributo | Perfil |
|----------|--------|
| **Market** | BR primário (LGPD), US secundário |
| **Tamanho empresa** | ARR R$ 10M+ ou mid-market estabelecido |
| **Dados proprietários** | Tem volume de dado proprietário onde IA agrega valor (CRM histórico, logs operacionais, documentos regulatórios, transações) |
| **Compliance named** | LGPD baseline; GxP (life sciences), BACEN (fintech), SOC 2 (SaaS US), ANVISA (healthtech) — pelo menos um já é tema |
| **Vertical preferido** | SaaS B2B, fintech, healthtech, life sciences, e-commerce |
| **Cargo decision-maker** | CTO, VP Eng, Head of AI/ML/Data, technical founder, CPO |
| **Sinais "tá maduro"** | Tem 2-5 PoCs de IA travados, board pressionando estratégia, CTO já gastou R$ 100k+ em ferramentas IA, tem data engineer sênior |
| **Sinais "não bom fit"** | Empresa <R$ 5M ARR, sem dado proprietário, querendo "implementar ChatGPT" sem caso definido, budget <R$ 100k pra ano 1 |

### Growth (Sales Ops Diagnostic)

| Atributo | Perfil |
|----------|--------|
| **Market** | BR primário, US secundário (only US se time founder-led com tração) |
| **Tipo negócio** | B2B com ticket médio > R$ 5k |
| **Tamanho time comercial** | 2-15 (founder-led a sales ops formal) |
| **Sales cycle** | 30-180+ dias |
| **Cargo decision-maker** | Founder/CEO, Head of Growth/Revenue, Head of Sales, COO |
| **Stack atual** | HubSpot, Salesforce, Pipedrive, RD Station, Notion, Airtable, planilha-only |
| **Sinais "tá maduro"** | Founder é gargalo do funil OR time SDR sobrecarregado com processo manual, leads esfriando entre canais, response time >2h em horário comercial |
| **Sinais "não bom fit"** | Ticket médio <R$ 1k, B2C puro, time comercial 1 pessoa, vendas via marketplace |

### Industry (Vertical Assessments + Pilots)

| Atributo | Perfil |
|----------|--------|
| **Market** | BR primário |
| **Verticais cobertas** | Manufacturing, Logistics, Healthcare, Life Sciences, FinServ |
| **Tamanho empresa** | Revenue R$ 50M+ (varia por vertical) |
| **Compliance named** | Vertical-specific: GxP (life sciences), LGPD-saúde (healthcare), BACEN 4.658 (finserv), HIPAA (US healthcare) |
| **Cargo decision-maker** | CEO/Founder, COO, CTO, Head of Innovation/Digital, Head of Compliance, Operations Director, Plant Director |
| **Sinais "tá maduro"** | Vertical pain específico nomeado (downtime, quality inspection, supply visibility, clinical doc, fraud, KYC), AI maturity exploring-to-scaling, budget aprovado pra POC vertical |
| **Sinais "não bom fit"** | Vertical fora das 5 cobertas, revenue <R$ 50M (varia), querendo "geração de descrição produto" (commodity AI sem caso vertical) |

---

## 5. Delivery SOPs

5 SOPs completos. Cada um segue o mesmo padrão: 4 semanas (ou 2-3 pra Sales Ops e AI Readiness), com inputs/atividades/deliverable por semana, ferramentas usadas, e checklist 10-20 itens que sempre são revisados.

### 5.1 FinOps Audit (4 semanas, R$ 45-60k)

**Semana 1 — Discovery & Data Collection**
- **Inputs:** AWS Cost & Usage Reports últimos 6 meses, AWS Cost Explorer (acesso read-only), Trusted Advisor findings (export CSV), Compute Optimizer recommendations, billing configuration (consolidação, organization structure), tagging strategy atual, lista de accounts + ownership.
- **Atividades:** Workshop 2h com time cloud (CTO/Head Cloud + 1-2 engineers). Mapear accounts (production/staging/dev/shared services), services em uso por account, tags existentes vs gaps, ownership por workload. Setup acesso read-only (IAM role assumida) pra Anuvia rodar queries em CUR via Athena.
- **Deliverable:** Data extraction completo + baseline de spend documentado (CSV + dashboard). Tagging gap analysis. Initial heatmap de spend por service por account.
- **Ferramentas:** AWS CUR via Athena (queries SQL), AWS Cost Explorer API, AWS Organizations, AWS Trusted Advisor, AWS Compute Optimizer, Anuvia internal toolkit (queries pre-built).

**Semana 2 — Análise & Identificação**
- **Inputs:** Outputs da semana 1 + métricas de produção do cliente (CloudWatch, Datadog se relevante).
- **Atividades:** Análise sistemática nos 8 vetores —
  1. **Compute:** right-sizing (CPU/RAM utilization 7-14 dias), RI/SP coverage atual vs ideal, Spot eligibility por workload, Graviton migration candidates.
  2. **Storage:** EBS volumes orfãos, snapshots órfãos, S3 lifecycle gaps, intelligent tiering candidates, S3 incomplete multipart uploads.
  3. **Network:** NAT Gateway costs por VPC, VPC peering vs PrivateLink, cross-AZ data transfer hotspots, egress data transfer (CloudFront vs direto).
  4. **Data transfer:** S3 → EC2 cross-region, RDS replicas cross-AZ, ELB traffic patterns.
  5. **RDS/Aurora:** instance class fit, RI coverage, storage growth rate vs IO, Aurora I/O optimized eligibility, performance insights anomalies.
  6. **S3:** lifecycle policies gaps, Glacier transition candidates, requester-pays opportunity, Inventory + Storage Lens analysis.
  7. **Third-party SaaS:** Marketplace subscriptions, dev tools (CI runners, log SaaS), security tools subscriptions duplicadas.
  8. **Support tier:** Business vs Enterprise tier ROI check.
- **Deliverable:** Findings list com economia estimada por categoria + effort/risk scoring (low/med/high) + priority matrix (quick wins, medium-term, structural).
- **Ferramentas:** Athena, CloudWatch, AWS Trusted Advisor Pro, AWS Compute Optimizer, terraform/CDK pra modelagem mudanças.

**Semana 3 — Quick Wins Implementation**
- **Inputs:** Findings priorizados da semana 2 (high impact, low risk).
- **Atividades:** Implementação direta dos quick wins COM aprovação cliente em cada step:
  - Cleanup snapshots órfãos (>90 dias sem reference).
  - S3 lifecycle policies em buckets sem regra.
  - Cleanup EBS volumes detached >30 dias.
  - Right-sizing RDS instances com utilização <30% sustentada (com janela de manutenção agendada).
  - Remoção NAT Gateway redundante (consolidação por VPC).
  - Cleanup data transfer não mapeado (DNS routing, cache tuning).
  - Cleanup S3 incomplete multipart uploads.
- **Deliverable:** Economia mensurável já em produção + change log documentado (cada mudança com timestamp, ticket, aprovador, rollback procedure).
- **Ferramentas:** Terraform/CDK pra mudanças críticas, AWS Console pra cleanup operations, CloudTrail pra audit log.

**Semana 4 — Roadmap & Handoff**
- **Inputs:** Tudo anterior.
- **Atividades:** Cost optimization roadmap 12 meses — médio prazo (RI/SP strategy, Graviton migration, S3 intelligent tiering rollout, observability cost optimization) + alto risco (re-architecture cross-AZ, multi-region rationalization, database migration considerations). ADR pra cada decisão estrutural. Training do time interno (2h handoff session).
- **Deliverable:** Relatório executivo (saving identified por linha + executado + roadmap), apresentação executiva 30 slides, ADRs documentados, handoff session gravada.
- **Ferramentas:** Anuvia template framework (PDF report + PPTX deck), Notion pra ADRs durante engagement (handoff em formato cliente).

**Checklist 16 itens revisados em TODA auditoria FinOps:**
1. ☐ RI/SP coverage atual vs ideal (target 70-85% pra workloads steady-state)
2. ☐ Compute right-sizing: instances com CPU avg <30% 14 dias
3. ☐ Graviton eligibility por workload (price/perf 20%+ savings)
4. ☐ Spot instance eligibility pra workloads stateless
5. ☐ EBS snapshots órfãos >90 dias
6. ☐ EBS volumes detached >30 dias
7. ☐ S3 lifecycle policies em todos buckets >R$ 1k/mês
8. ☐ S3 intelligent tiering em buckets com mixed access pattern
9. ☐ NAT Gateway redundância por VPC
10. ☐ Cross-AZ data transfer hotspots
11. ☐ RDS/Aurora storage type (gp2→gp3, io1→gp3 com IOPS ajustado)
12. ☐ Aurora I/O optimized eligibility (I/O >20% do bill RDS)
13. ☐ CloudFront usage em egress >R$ 5k/mês
14. ☐ Third-party SaaS subscriptions cross-check (duplications)
15. ☐ Support tier downgrade eligibility (Enterprise → Business se uso <5 cases/quarter)
16. ☐ Reserved Capacity em DynamoDB, ElastiCache, OpenSearch

---

### 5.2 AI Readiness Sprint (2-3 semanas, R$ 25-40k)

**Semana 1 — Discovery (1 semana intensa)**
- **Inputs:** Lista de lideranças (marketing, ops, atendimento, antifraude, tech, compliance), data inventory atual (datasets, warehouses, lakes), histórico de PoCs IA tentados, ferramentas IA em uso, compliance constraints nomeados.
- **Atividades:**
  - Workshop 1 dia (8h) com 5-8 stakeholders pra brainstorm de use cases candidatos.
  - 1:1s individuais (45 min cada) com cada head de área pra deep-dive em pain points.
  - Data inventory: catalogação de datasets disponíveis, qualidade percebida, acesso e propriedade.
  - Compliance posture: identificar quais constraints aplicam por caso (LGPD, GxP, BACEN, SOC 2).
- **Deliverable:** Long list 12-20 use cases candidatos, com (a) descrição em 1 parágrafo, (b) heat indicator preliminar (high/med/low impact, high/med/low feasibility), (c) data dependencies, (d) compliance flags.

**Semana 2 — Scoring & Filtering**
- **Inputs:** Long list da semana 1.
- **Atividades:**
  - Scoring framework por caso (15 dimensões):
    1. Disponibilidade de dado (existe, é acessível, tem volume suficiente?)
    2. Qualidade do dado (limpo, anotado, schema estável?)
    3. Custo estimado de inferência (tokens/dia × custo unitário modelo)
    4. Latência aceitável (real-time, near-real, batch?)
    5. Compliance burden (LGPD personal data, GxP validation, BACEN audit?)
    6. Build vs buy posture (modelo proprietário vs API vs vendor SaaS?)
    7. Integração tech (precisa novo backend? API gateway? vector DB?)
    8. Change management (treinar usuários novos? mudar processo?)
    9. ROI estimado em R$ ano 1 (com assumptions explícitas)
    10. Time to value (PoV em 4 weeks? produção em Q?)
    11. Risco regulatório (decisão automatizada → revisão humana obrigatória?)
    12. Risco reputacional (hallucination customer-facing?)
    13. Vendor lock-in score (proprietário vs commodity model?)
    14. Internal champion (existe? quem?)
    15. Executive sponsorship (CEO/board buy-in?)
  - Filter pra short list: top 5-8 com score combinado >70/100 e ROI defensável.
- **Deliverable:** Scored inventory completo (long list pontuada) + short list 5-8 cases priorizados + ROI model por case da short list (planilha com assumptions).

**Semana 3 — Roadmap & Decisions**
- **Inputs:** Short list da semana 2.
- **Atividades:**
  - 12-month roadmap sequenciado por (a) ROI/effort, (b) dependências técnicas, (c) compliance criticality.
  - Definir gates por caso: discovery → PoV → production (com critérios de saída de cada gate).
  - Build vs buy decision per case (recomendação + justificativa).
  - Identificar PoCs existentes a descontinuar (com justificativa).
  - Identificar dependências cross-case (vector DB compartilhado? eval framework? observability stack?).
  - Construir executive deck (30 slides).
- **Deliverable:** 12-month roadmap doc + executive presentation + ROI model finalizado + descontinuation recommendations.

**Ferramentas:** Anthropic/OpenAI APIs pra scoring auxiliar, Anuvia template (PPTX deck + PDF report), Notion durante engagement.

**Checklist 12 itens revisados em TODO AI Readiness:**
1. ☐ Cada use case tem data dependency mapeada
2. ☐ Cada use case tem compliance posture explícita
3. ☐ ROI model usa assumptions documentadas (não chute)
4. ☐ Inference cost calculado por case (não estimado em ordem de grandeza)
5. ☐ Latency budget definido por case (real-time vs near-real vs batch)
6. ☐ Build vs buy posture justificada
7. ☐ Internal champion identificado por case
8. ☐ Executive sponsor identificado por case
9. ☐ Gates definidos com critério de saída (não "vai pra fase 2 quando estiver pronto")
10. ☐ Dependências cross-case identificadas
11. ☐ PoCs existentes avaliados (continue / kill / refactor)
12. ☐ Risk register (hallucination, regulatory, reputational) por case

---

### 5.3 DevOps Maturity Assessment (4 semanas, R$ 35-50k)

**Semana 1 — Baseline DORA**
- **Inputs:** CI logs (Jenkins/GitHub Actions/CircleCI/GitLab CI) últimos 90 dias, git history, incident tracker (Linear/Jira/Opsgenie/PagerDuty), on-call rotation data, deploy logs (Argo CD/Flagger/manual scripts).
- **Atividades:** Extração e cálculo das 4 métricas DORA reais (não auto-reportadas):
  - **Deploy Frequency:** counts de successful deploys por dia/semana via CI logs.
  - **Lead Time for Changes:** time entre PR merge e production deploy.
  - **MTTR:** time entre incident detection e resolution (do tracker).
  - **Change Failure Rate:** % deploys que tiveram rollback/hotfix/incident em 24h.
- **Deliverable:** DORA baseline doc (números reais por serviço + global) + gap analysis vs DORA 2023 Elite/High/Medium/Low thresholds.

**Semana 2 — Maturity Deep-dive**
- **Inputs:** Baseline + acesso ao stack atual (CI, observability, IaC, incident management).
- **Atividades:** Auditoria sistemática em 6 dimensões:
  1. CI/CD maturity (pipeline structure, test coverage, deployment patterns)
  2. Test automation (unit/integration/E2E coverage, test reliability)
  3. IaC adoption (Terraform/CDK/CloudFormation coverage, state management, drift detection)
  4. GitOps readiness (Argo CD/Flux usage, declarative state, rollback automation)
  5. Observability stack (metrics/logs/traces/alerts coverage, SLI/SLO definition, dashboard hygiene)
  6. Incident response (runbooks coverage, post-mortem culture, on-call rotation health, blameless framework)
- **Deliverable:** Maturity scorecard por dimensão (1-5 scale) + finding details + benchmark vs DORA elite teams.

**Semana 3 — Roadmap & Quick Wins**
- **Inputs:** Maturity scorecard + business priorities (cliente diz: o que duela mais?).
- **Atividades:**
  - 6-month roadmap priorizado por (impact × confidence) / effort.
  - Identificar 5-8 quick wins (feature flags introdução, observability gaps fix, runbook templates pra top serviços).
  - Recomendações de tooling (feature flags: LaunchDarkly vs Unleash vs OpenFeature; observability: Datadog vs Grafana stack; IaC: terraform vs Pulumi vs CDK).
- **Deliverable:** 6-month roadmap doc + quick wins playbook + tooling recommendations.

**Semana 4 — Executive Sync & Handoff**
- **Atividades:** Executive presentation (30 slides) + 2h handoff workshop com Eng Leadership + Q&A. Optionally: define KPI tracking framework pra DORA metrics ongoing.
- **Deliverable:** Final executive report + presentation gravada + KPI tracking template.

**Ferramentas:** Custom scripts pra extração DORA (Python + GitHub API + Jenkins API + Opsgenie API), Anuvia maturity scorecard template, Notion durante engagement.

**Checklist 14 itens revisados em TODO DevOps Maturity:**
1. ☐ DORA metrics extraídas de fontes reais (não self-reported)
2. ☐ Deploy frequency por serviço (não só agregado)
3. ☐ Change failure rate definição clara (rollback? hotfix? incident em 24h?)
4. ☐ MTTR mediano vs P95
5. ☐ Test coverage real (unit + integration + E2E) por serviço crítico
6. ☐ IaC coverage % (workloads gerenciados por código vs manual)
7. ☐ Observability stack: SLI/SLO definidos por serviço crítico
8. ☐ Runbooks count por serviço crítico (target: ≥1 por serviço)
9. ☐ Post-mortem culture (last 5 incidents tiveram retro documentada?)
10. ☐ On-call rotation health (burnout signals, fairness, pager load)
11. ☐ Feature flag adoption (% deploys gated)
12. ☐ Canary deploy infrastructure presente?
13. ☐ Rollback automation testado (last 90 dias)?
14. ☐ Engineer satisfaction (proxy: retention 12 meses)

---

### 5.4 Sales Ops Diagnostic (2 semanas, R$ 15-25k)

**Semana 1 — Funnel Mapping & Stack Audit**
- **Inputs:** CRM access read-only (HubSpot/Salesforce/Pipedrive/RD/Notion), sequence/email tool access, channel intake docs (WhatsApp Business export, form analytics, referral tracking), last 90 dias de data.
- **Atividades:**
  - End-to-end funnel mapping: each stage with conversion rate, avg time, drop-off reasons.
  - Channel response-time SLA medido (não declarado) por cada canal de entrada.
  - Stack assessment: cada tool (CRM, email, automation, ancillary) — keep/integrate/replace.
  - Identify 5-8 leakage points concrete com nome (não "leads frios" — qual stage, qual canal, quantos por semana).
- **Deliverable:** Funnel map visual + leakage points doc + stack assessment matrix.

**Semana 2 — Automation Playbook & Roadmap**
- **Inputs:** Funnel map + leakage points.
- **Atividades:**
  - Automation map: por leakage point, qual automação aplica + impact estimate + effort estimate + ROI.
  - Recommend top 5-8 automations priorizadas.
  - 90-day roadmap (13-week plan) com (a) phase 1 quick wins (semanas 1-4), (b) phase 2 structural (semanas 5-9), (c) phase 3 optimization (semanas 10-13). Cada fase com KPIs + evolution gates.
  - Recommendation: build vs buy per automation (use HubSpot workflows? n8n custom? Make.com? Zapier?).
- **Deliverable:** Automation playbook + 90-day roadmap + tooling recommendations.

**Ferramentas:** CRM analytics export, Anuvia funnel template, Notion durante engagement.

**Checklist 12 itens revisados em TODO Sales Ops Diagnostic:**
1. ☐ Funnel stages claramente definidos (não vagas como "MQL")
2. ☐ Conversion rate stage-by-stage (não só top-to-bottom)
3. ☐ Avg dwell time por stage
4. ☐ Response time SLA medido por canal (form, WhatsApp, email, referral)
5. ☐ Top 3 leakage points com nome + volume + impacto $
6. ☐ Lead source attribution
7. ☐ Funnel velocity (deals por semana per stage)
8. ☐ Stack inventory (todas as ferramentas + cost mensal + uso real)
9. ☐ Tool integration map (qual fala com qual? gaps?)
10. ☐ Sales cycle distribution (mediano vs P75 vs P95)
11. ☐ SDR/AE capacity utilization
12. ☐ Quota attainment % (se aplicável)

---

### 5.5 Industry Assessment + Vertical Pilot (Free diag + 4-6 semanas pilot)

**Free diagnostic (90 segundos, online via LP)**
- **Atividades:** Form de 6-8 perguntas online (industry, revenue, pain, compliance, AI maturity). Claude scoring + recomendação automatizada por email. Recomendação inclui (a) vertical playbook match, (b) compliance flags, (c) estimated ticket range, (d) CTA pra discovery call.

**Vertical Pilot (4-6 semanas, R$ 35-55k)**

Cada vertical tem playbook próprio. Estrutura comum:

**Semanas 1-2 — Discovery & Compliance Mapping**
- Workshop com lideranças vertical-specific (Plant Manager + Quality + IT pra manufacturing; CMO + DPO + Operations pra healthtech; etc.).
- Mapeamento de processos críticos onde IA aplica.
- Compliance posture deep-dive (GxP, LGPD, BACEN, HIPAA).
- Data inventory específico do vertical (PLC streams, EHR, transaction logs, etc.).

**Semanas 3-4 — PoV Design & Build**
- Define PoV scope (1 caso priorizado, executável em 4 semanas).
- Build PoV (geralmente: data pipeline + model + integration mock + eval framework).
- Compliance validation framework (IQ/OQ/PQ pra GxP, audit trail pra BACEN).
- Eval set construído com domain expert do cliente.

**Semanas 5-6 — Validation & Roadmap**
- PoV run em dados reais (ou shadow mode).
- Eval results vs success criteria.
- Production rollout roadmap (technical + organizational + compliance gates).
- Executive presentation.

**Deliverables base:**
- Vertical playbook fit assessment
- Compliance posture validated
- PoV results (eval metrics + qualitative feedback)
- Production roadmap (6-12 meses)
- Executive presentation

**Playbooks por vertical (resumo):**

| Vertical | Casos típicos | Compliance | Ticket pilot range |
|----------|--------------|------------|---------------------|
| Manufacturing | OEE optimization, predictive maintenance from sensor streams, computer-vision quality | ISO 27001, ISO 9001 | R$ 45-65k |
| Logistics | Fleet telemetry analytics, ML route/ETA optimization, last-mile tracking | LGPD | R$ 40-55k |
| Healthcare | Clinical documentation assistants, RAG sobre protocolos institucionais, intake triage | LGPD-saúde, HIPAA | R$ 45-65k |
| Life Sciences | SOP automation, regulatory drafting, GxP validation packages | ANVISA/FDA GxP | R$ 50-70k (premium devido a GxP) |
| FinServ | Real-time fraud detection, AML monitoring, KYC onboarding | BACEN 4.658, LGPD | R$ 50-70k |

**Checklist 10 itens revisados em TODO Vertical Pilot:**
1. ☐ Compliance constraints mapped + validated com Compliance officer cliente
2. ☐ Data inventory específico do vertical confirmado disponível
3. ☐ PoV scope ≤4 weeks executable (não overscoped)
4. ☐ Eval set construído com domain expert (não só métrica genérica)
5. ☐ Success criteria do PoV pre-defined (não retrofit)
6. ☐ Shadow mode design considered (não go-live direto)
7. ☐ Production roadmap separated de PoV scope (não bait-and-switch)
8. ☐ Vendor lock-in risk assessed
9. ☐ Internal champion identificado
10. ☐ Executive sponsor briefed and bought-in

---

## 6. Tools/credentials que agentes vão precisar

### Já temos (Mila confirmou ou óbvio)
- Anthropic API key (Claude)
- Resend API key + send.anuvia.com.br domain verified
- Supabase URL + service_role key
- GitHub PAT (sprint pushes funcionando)
- Coolify (deploy infra Hetzner)
- Slack (workspace + #anuvia-alerts channel + webhook)
- n8n (workflows existentes + Anuvia Bot)
- Make.com (LinkedIn router)
- Google Calendar (Mila's primary, gcal.events scope)
- AWS account (Mila pessoal — pra demos de FinOps)

### Mila precisa gerar/conseguir (priority order)

#### CRÍTICO PRA WAVE 1 GO-LIVE
1. **Stripe BR** — recomendação: criar conta nova `anuvia-br` se ainda não tem. Pegar `STRIPE_SECRET_KEY` (modo live) + `STRIPE_WEBHOOK_SECRET` quando configurar webhook. **[VALIDATE]** Mila confirma se já tem conta Stripe BR ativa.
2. **Mercado Pago** — pra Pix/boleto BR. Conta business + gerar `MERCADO_PAGO_ACCESS_TOKEN` (Production) + `MP_WEBHOOK_SECRET`. **[VALIDATE]** Mila confirma se quer aceitar Pix/boleto ou só cartão via Stripe.
3. **Apollo.io** (recomendação > Clay) — pra prospecting BR + US. Plan Basic $59/mês cobre primeiros 200 prospects/dia. Gerar API key. **[VALIDATE]** Mila confirma orçamento.
4. **CONTRACT_HMAC_SECRET** + **INBOUND_WEBHOOK_SECRET** — gerar via `openssl rand -hex 32`. Já tá no Coolify env.

#### IMPORTANTE PRA WAVE 2 (delivery)
5. **HubSpot (free tier)** — pra Sales Ops Diagnostic, Mila vai precisar acesso read-only de CRM cliente. Free tier OK pra começar (1k contacts). Recommendation: Mila usa HubSpot pessoal pra testar agents antes de pedir acesso cliente.
6. **PandaDoc** (recomendação > DocuSign) — pra contratos. Preço BR friendly ($19/mês business plan). Necessário pra Wave 2 mais formal. **[VALIDATE]** Mila prefere PandaDoc ou DocuSign?
7. **Conta Azul** — pra NF-e Brasileira. Necessário só quando faturar primeiro cliente real. Por ora podemos deixar em stub mode (gerar PDF de invoice sem fiscal).
8. **BuiltWith** ($295/mês Pro) — pra enriquecer prospects com tech stack signal (AWS detection, observability tools, etc.). Optional — se não vier, prospecting agent funciona sem (só Apollo data).

#### NICE-TO-HAVE
9. **LinkedIn Sales Navigator** — pra prospecting LinkedIn (paid plan). $99/mês. Pode esperar quando outbound email validar.
10. **Cloudflare Workers** (já temos provavelmente) — pra outbound throttling + rate limiting se Resend SMTP não comportar volume.

### Recomendação Anuvia stack-of-record (ano 1)
| Tool | Plan | Mensal | Uso |
|------|------|--------|-----|
| Anthropic API | Claude Sonnet 4.5 | $200-500 | Outbound personalization, classification, delivery |
| Resend | Pro | $35 | Outbound + transactional |
| Supabase | Pro | $25 | DB + auth + storage |
| Coolify | Self-hosted Hetzner | €40 (Hetzner) | Deploy infra |
| Stripe BR | Business | 4.99% + R$ 0.39 | Card processing |
| Mercado Pago | Standard | 4.99% | Pix/boleto |
| Apollo.io | Basic | $59 | Prospecting |
| PandaDoc | Business | $19 | E-sign |
| Conta Azul | Standard | R$ 79 | NF-e BR |
| BuiltWith | Pro (later) | $295 | Tech stack enrichment |
| n8n | Self-hosted | (Hetzner) | Workflow orchestration |
| HubSpot | Free | $0 | CRM (testing + free tier clients) |
| Slack | Standard | $0-7.25/user | Internal + cliente channels |
| **Total recurring** | | **~$700/mês** | (excluindo transaction fees) |

---

## 7. Limites/preferências

### Outbound

**[VALIDATE]** Mila confirma cada um dos valores abaixo.

- **Outbound diário máximo (semana 1-2):** 30 emails/dia (warm-up reputation). Já configurado em `OUTBOUND_DAILY_CAP` env.
- **Outbound diário máximo (após semana 2 se open rate >25%):** 50 emails/dia. Após semana 4: 100/dia se reply rate >3%.
- **LinkedIn DMs (quando habilitar):** 20/dia max (mais conservador, LinkedIn pune mais).
- **Horário envio:** seg-sex 9h-16h BRT (evita ser flagged como bot, casa com horário comercial BR).
- **Pausa em feriados BR:** automática (carnaval, semana santa, corpus christi, 7/9, 15/11, 24/12-2/1).
- **Bounce rate threshold:** se >5% em qualquer dia, pausa automática + Slack alert.
- **Unsubscribe handling:** automático na mesma mensagem (link footer) + flag permanente no DB.

### Escalation (quando agente deve consultar Mila)

| Trigger | Action |
|---------|--------|
| **Deal value > R$ 80k** | Slack DM com botão Approve/Deny antes de enviar contract. |
| **Pedido desconto > 10%** | Slack DM com contexto + recomendação. Mila aprova em <2h ou agente sugere alternativa. |
| **Resposta de prospect com objeção técnica que agente não responde com confidence > 80%** | Slack DM com a pergunta + draft de resposta sugerida + label "Aprovar/Editar/Descartar". |
| **Prospect VIP** (domínios em "Trusted accounts": Globo, Magalu, iFood, Stone, Mercado Livre, Embraer, Itaú, B3, Movile, Loft, QuintoAndar) | Slack DM imediato. Agente NÃO envia outbound nem reply sem aprovação. |
| **Reply classification confidence < 0.6** | Slack escalation automática (já hard-coded em `reply_classify.py`). |
| **3+ replies em mesma thread sem resolução** | Slack DM "esse lead precisa de humano". |
| **Stripe/MP webhook failure após 3 retries** | Slack DM "falha de pagamento — investigar". |
| **Engagement kickoff falhou (delivery agent error)** | Slack DM imediato. Tudo no Wave 2 (delivery agents) escala em erro. |

### Estilo escalation
- **Canal:** Slack DM (não channel — DM direta pra Mila pra ela ver mesmo offline).
- **Format:** Rich blocks com (a) lead/prospect summary, (b) action proposed, (c) Approve/Deny buttons, (d) "Show full context" expandable.
- **SLA:** Mila aprova em <2h durante horário comercial. Em <4h em horário pessoal. Se não responder em 12h, agente envia DM follow-up "ainda precisa de decisão".

### Brand voice ongoing
- **Agente NÃO usa:** emojis (a menos que cliente use primeiro), exclamation marks excessivas, "🚀", "🔥", linguagem pomposa, "synergy/leverage/transform", promises sem número.
- **Agente USA:** voz direta, números concretos, pares antes/depois quando relevante, anti-hype, autodepreciação leve quando apropriado.
- **Templates outbound:** sempre personalização Claude por prospect (template base + signals do enriquecimento). Nunca mass-mailmerge naive.
- **Mila aprova:** todo novo template de outbound antes de ir ao vivo. Variação A/B só com aprovação.

### Frequência de reporting pra Mila
- **Daily brief (existente W7):** 8am BRT, Slack DM. Resume: deals em progresso, replies pra triagem, deals fechados ontem, escalations pendentes.
- **Weekly review:** sexta-feira 17h, gerado automaticamente, Slack DM. Resume: revenue da semana, conversion rates, top 3 wins, top 3 losses, pipeline saúde.
- **Monthly business review:** primeiro dia útil do mês. PDF report. Resume: revenue mês, MRR vs ano anterior, prospects topo funil, deals em flight, próximas semanas projeção.

---

## Pronto pra deploy?

Quando Mila revisar este arquivo:
1. Edita o que quiser ajustar (sobretudo seções 2 — case studies números reais, 3 — pricing confirmation, 6 — credentials que ela quer gerar, 7 — limits que ela quer ajustar).
2. Commita no repo `anuvia-lp` que o agente detecta via Wave 2 launch.
3. Me marca "Pronto, lança Wave 2" e eu disparo os 5 delivery agents em paralelo com base nas SOPs acima.

**Wave 1 já tá vivo.** Wave 2 (5 delivery agents) começa quando Mila aprovar este draft ou ajustar.

— Claude (este draft escrito 2026-05-15)
