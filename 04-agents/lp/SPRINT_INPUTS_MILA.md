# Sprint Inputs — Máquina Autônoma v1

**Para Mila preencher em paralelo enquanto Claude constrói (~1h teu tempo).**

Cada seção vira config dos agentes. Quanto mais detalhe e exemplo real, melhores os agentes ficam logo no primeiro deploy.

---

## 1. Brand voice (15 min)

**Cola 5-10 textos teus que representam tua voz** (LinkedIn posts, emails de proposta, mensagens de WhatsApp pra clientes). Não importa formato — só cola. Usado pra RAG/few-shot dos agentes.

```
[Texto 1]


[Texto 2]


[Texto 3]


...
```

---

## 2. Case studies (20 min)

3 engagements reais. Pode ser anonimizado (cliente = "PME de SaaS BR" ou "mid-market US health-tech"). O que importa: número real.

### Case 1

- **Cliente:** [setor + porte, sem nome]
- **Prática:** [Cloud / AI / Engineering / Growth / Industry]
- **Problema (1 parágrafo):**
- **O que fizemos (3-5 bullets):**
- **Número (antes → depois):**
- **Duração engagement:**

### Case 2

[mesma estrutura]

### Case 3

[mesma estrutura]

---

## 3. Pricing per prática (10 min)

Se já está nas LPs, só confirma. Se não, define agora.

| Prática | Preço base | Preço range | Como escalona? |
|---------|-----------|-------------|----------------|
| Cloud FinOps Audit | R$ | R$ X – Y | Por % savings? Por tamanho cluster? |
| AWS Well-Architected | R$ | R$ X – Y | |
| AWS Migration | R$ | R$ X – Y | |
| AI Readiness | R$ | R$ X – Y | |
| DevOps Maturity | R$ | R$ X – Y | |
| Growth Sales Ops | R$ | R$ X – Y | |
| Industry Assessment | R$ | R$ X – Y | |

**Mínimo de desconto que agente pode dar sem te consultar:** ___% (default sugerido: 10%)
**Acima desse desconto:** escala pro Slack pra você aprovar.

---

## 4. ICP per prática (15 min)

Pra cada prática, define o perfil ideal de cliente:

### Cloud (FinOps + AWS + GCP)
- **Market:** [BR / US / Ambos]
- **Tamanho empresa (employees):** ex. 20-500
- **AWS spend mínimo (cobre o ticket):** ex. R$ 25k/mês
- **Vertical/Setor preferido:** ex. SaaS, fintech, e-commerce
- **Cargo do decision-maker:** ex. CTO, VP Engineering, Head Cloud
- **Sinais de "tá maduro":** ex. tem RDS, tem mais de 3 AWS accounts, tem CI/CD

### Engineering (DevOps)
[mesma estrutura]

### AI
[mesma estrutura]

### Growth
[mesma estrutura]

### Industry
[mesma estrutura]

---

## 5. Delivery SOPs (20 min)

**Pra cada prática que você JÁ entregou pra cliente real**, descreve o processo:

### Exemplo: FinOps Audit (4 semanas, R$ 45-60k)

**Semana 1 — Discovery & Data Collection**
- Inputs: AWS Cost & Usage Reports últimos 6 meses, AWS Cost Explorer, Trusted Advisor findings, billing config
- Atividades: workshop 2h com time cloud, mapear accounts/services/tags/ownership
- Deliverable: data extraction completo + baseline de spend documentado

**Semana 2 — Análise & Identificação**
- Inputs: outputs da semana 1
- Atividades: análise sistemática compute (right-sizing, RI/SP coverage, Spot), storage (lifecycle, intelligent tiering, snapshots órfãos), network (egress, NAT, transit gateway), data transfer, RDS/Aurora, S3, third-party SaaS, support tier
- Deliverable: findings list com economia estimada por categoria + effort/risk scoring

**Semana 3 — Quick Wins Implementation**
- Inputs: findings priorizados (high impact / low risk)
- Atividades: implementação direta (com aprovação) — idle resources, snapshot cleanup, unused EBS, redundant data transfer, S3 lifecycle
- Deliverable: economia mensurável já em produção + change log documentado

**Semana 4 — Roadmap & Handoff**
- Inputs: tudo anterior
- Atividades: cost optimization roadmap 12 meses (médio + alto risco), ADR pra cada decisão grande, training do time interno
- Deliverable: roadmap completo + ADRs + handoff session 1h

**Ferramentas que uso:** AWS CUR via Athena, Cost Explorer API, Trusted Advisor, AWS Compute Optimizer, terraform/CDK pra mudanças

**O que SEMPRE checo:** [listar 10-20 itens que você revisa toda auditoria — esse vira o "playbook" do agent]

---

### Pra outras práticas: mesmo formato

(Se não tem SOP escrito pra alguma prática ainda — diz. Eu construo agent placeholder e iteramos quando tiver clientes reais.)

---

## 6. Tools/credentials que agentes vão precisar

**Já tenho conta + posso gerar API key:**
- [ ] AWS (pra FinOps agent puxar Cost & Usage Reports)
- [ ] HubSpot/Salesforce (pra Growth agent — qual usas hoje?)
- [ ] Stripe BR (pra contract/payment)
- [ ] Mercado Pago (pra contract/payment Pix/boleto)
- [ ] Apollo.io ou Clay (pra prospecting — qual prefere?)
- [ ] LinkedIn Sales Navigator
- [ ] DocuSign / PandaDoc (pra e-sign)
- [ ] Conta Azul ou similar (pra NF-e)

**Já posso gerar agora (você me passa key):**
- Resend (já temos)
- Anthropic (já temos)
- Supabase service_role (já temos)
- GitHub PAT (já temos)

---

## 7. Limites/preferências

- **Outbound diário máximo:** ex. 50 emails/dia, 30 LinkedIn DMs/dia (pra não queimar reputação inicial)
- **Horário envio:** ex. seg-sex 9h-17h BRT
- **Quando agente DEVE escalar pra você:**
  - Deal value > R$ ___ (default sugerido: R$ 80k)
  - Pedido de desconto > ___%
  - Pergunta técnica que ele não responde com confiança > 80%
  - Cliente VIP/conhecido (lista de domínios em "Trusted accounts")
- **Estilo escalation:** Slack DM com botão Approve/Deny? Email? WhatsApp?

---

## Pronto pra deploy

Quando preencher tudo, commita esse arquivo no repo (`anuvia-lp`) que eu detecto + integro nos agentes. Ou cola direto no chat comigo.

**Eu enquanto isso já estou construindo a infra que não depende desses inputs:** outbound engine, sequence engine, contract automation skeleton, Track B handlers, KPI dashboard, etc.
