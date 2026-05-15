# Anuvia Autonomous Sales+Delivery Machine v2

**Date:** 2026-05-14
**Status:** Building. Compressed sprint — multiple parallel sub-agents executing in isolated modules.
**Builds on:** ARCHITECTURE_AUTONOMOUS_v1.md (still valid for orchestrator, sessions, Track B Growth).

---

## Vision

End-to-end autonomous machine: prospects leads → outreaches → qualifies → closes → delivers → invoices → renews. Mila intervenes only on strategy and exceptional cases (deal > R$ 80k, custom requests, escalations).

---

## Module layout

```
04-agents/lp/
├── app.py                      # FastAPI app (existing)
├── lib/
│   ├── sessions.py             # unified lead memory (existing)
│   ├── orchestrator.py         # tick + handler dispatch (existing)
│   ├── track_b.py              # autonomous close — Growth (existing)
│   ├── outbound.py             # NEW: cold outbound engine
│   ├── reply_classify.py       # NEW: Resend inbound + classification
│   ├── contract.py             # NEW: Stripe/MP + e-sign + invoice
│   ├── prospecting.py          # NEW: Apollo/Clay + ICP scoring
│   ├── delivery/
│   │   ├── __init__.py
│   │   ├── finops_audit.py     # NEW: AWS CUR ingestion + analysis
│   │   ├── ai_readiness.py     # NEW: use case inventory + roadmap
│   │   ├── devops_maturity.py  # NEW: DORA metrics + assessment
│   │   ├── growth_salesops.py  # NEW: RevOps stack audit
│   │   └── industry.py         # NEW: vertical playbooks
│   └── eval_suite.py           # NEW: replay leads through prompt variants
├── outbound/
│   ├── prospects/              # CSV uploads land here
│   ├── templates/              # email templates per practice + language
│   └── sequences/              # 3-7 touch sequences config
├── scripts/
│   ├── smoke_e2e.py            # existing
│   └── outbound_run.py         # NEW: dispatch outbound sequence batch
└── delivery_artifacts/         # NEW: where delivery agents output reports
```

---

## Track B handler expansion

Same pattern as Growth (`lib/track_b.py`) but per practice. Each registers handlers via `@register`:

| Practice | Funnel ID | Ticket | Handlers |
|----------|-----------|--------|----------|
| Growth | BR_GROWTH | R$ 4-8k | classify, gen_proposal_v1, fwup_d2, fwup_d5, close_ghosted_d10 ✅ |
| Cloud FinOps | BR_FINOPS | R$ 45-60k | classify_finops, gen_proposal_finops, fwup variants, fwup_ghosted |
| Cloud AWS WA | BR_AWS_WA | R$ 30-45k | classify_aws_wa, ... |
| AI Readiness | BR_AI | R$ 25-40k | classify_ai, ... |
| DevOps Maturity | BR_DEVOPS | R$ 30-50k | classify_devops, ... |
| Industry Assessment | BR_INDUSTRY | R$ 35-55k | classify_industry, ... |

All handlers follow the same return contract:
```python
{"next_action": str | None, "next_action_at": datetime | None,
 "status": str | None, "detail": str}
```

---

## Outbound contract

`lib/outbound.py` exposes:

```python
async def send_outbound_sequence(prospect: dict, practice: str, sequence_id: str = "v1") -> dict:
    """Kick off a multi-touch sequence for a prospect. Schedules touch 1 immediately,
    subsequent touches via orchestrator queue. Returns {sequence_id, first_touch_at}."""
    
async def render_personalized_email(prospect: dict, template: str, practice: str) -> dict:
    """Claude generates the per-prospect email body + subject from a template +
    prospect enrichment. Returns {subject, html_body, plain_body}."""

@register("outbound_touch_2")
async def h_outbound_touch_2(lead): ...

@register("outbound_touch_3")
async def h_outbound_touch_3(lead): ...

@register("outbound_stop")
async def h_outbound_stop(lead): ...
```

Prospects schema in Supabase (new table `prospects`):
```sql
CREATE TABLE prospects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT,
  first_name TEXT,
  last_name TEXT,
  title TEXT,
  company TEXT,
  company_size_band TEXT,
  vertical TEXT,
  country TEXT,
  enriched_data JSONB DEFAULT '{}',
  icp_score INT,
  practice_fit TEXT, -- which practice they best match
  source TEXT, -- 'apollo', 'manual_csv', 'linkedin', etc
  status TEXT DEFAULT 'new', -- new | sequence_running | replied | bounced | unsubscribed | converted | stopped
  current_touch INT DEFAULT 0,
  next_touch_at TIMESTAMPTZ,
  last_engaged_at TIMESTAMPTZ,
  converted_lead_id UUID REFERENCES leads(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

When prospect replies (Resend inbound webhook), `lib/reply_classify.py` runs:
1. Match prospect by email
2. Classify intent via Claude (interested/question/objection/unsubscribe)
3. If interested → upgrade to lead, book discovery via Track A flow
4. If question → auto-reply with answer (Claude generates)
5. If objection → reply attempt OR escalate Slack with context
6. If unsubscribe → mark, never contact

---

## Delivery agents contract

Each `lib/delivery/<practice>.py` exposes:

```python
async def kickoff(engagement_id: str, intake_data: dict) -> dict:
    """Run when contract is signed. Sends client intake form, creates Slack channel,
    schedules first delivery milestone."""

async def run_phase(engagement_id: str, phase: int) -> dict:
    """Execute one phase of delivery. Returns artifacts list, next_phase_at."""

async def generate_deliverable(engagement_id: str, deliverable_type: str) -> dict:
    """Generate final report PDF, recommendations matrix, change log, etc."""

@register("delivery_finops_phase_1_data_collection")
async def h_finops_p1(engagement): ...
# ... etc
```

Engagements table:
```sql
CREATE TABLE engagements (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID REFERENCES leads(id),
  practice TEXT, -- 'finops', 'ai', 'devops', 'growth', 'industry'
  started_at TIMESTAMPTZ DEFAULT now(),
  contract_signed_at TIMESTAMPTZ,
  first_payment_at TIMESTAMPTZ,
  current_phase INT DEFAULT 1,
  total_phases INT,
  status TEXT DEFAULT 'kickoff', -- kickoff | in_progress | review | delivered | invoiced | closed
  intake_data JSONB DEFAULT '{}',
  artifacts JSONB DEFAULT '[]',
  next_phase_at TIMESTAMPTZ,
  total_value_brl NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

---

## Contract & payment contract

`lib/contract.py` exposes:

```python
async def generate_contract(lead_id: str, practice: str, value_brl: int) -> dict:
    """Generate signed contract PDF. Returns {contract_id, pdf_url, sign_url}."""

@router.get("/api/contract/sign")
async def sign_contract(contract_id: str, token: str) -> HTMLResponse:
    """Lead clicks sign link from email. HMAC-verified."""

@router.post("/api/contract/webhook/stripe")
async def stripe_webhook(...): ...

@router.post("/api/contract/webhook/mercadopago")  
async def mp_webhook(...): ...

async def issue_invoice(engagement_id: str) -> dict:
    """Generate NF-e via Conta Azul API. Returns {invoice_id, url}."""
```

Contracts table:
```sql
CREATE TABLE contracts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  lead_id UUID REFERENCES leads(id),
  engagement_id UUID REFERENCES engagements(id),
  practice TEXT,
  value_brl NUMERIC,
  status TEXT DEFAULT 'sent', -- sent | viewed | signed | paid | refunded
  sent_at TIMESTAMPTZ,
  signed_at TIMESTAMPTZ,
  paid_at TIMESTAMPTZ,
  pdf_url TEXT,
  sign_url TEXT,
  stripe_session_id TEXT,
  invoice_id TEXT, -- Conta Azul NF-e
  hmac_token TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
```

---

## Per-practice config (driven by Mila's inputs in SPRINT_INPUTS_MILA.md)

Loaded at startup from `lib/practice_config.py`:

```python
PRACTICE_CONFIG = {
    "finops": {
        "ticket_min": 45000,
        "ticket_max": 60000,
        "icp": {
            "company_size_band": ["20-500"],
            "aws_spend_min_brl": 25000,
            "verticals": ["saas", "fintech", "ecommerce"],
            "decision_makers": ["CTO", "VP Engineering", "Head Cloud"],
            "maturity_signals": ["multi_account_aws", "ci_cd_present", "rds_present"],
        },
        "discount_authority_pct": 10,
        "delivery_phases": 4,
        "sop": "FINOPS_SOP.md",
    },
    # ... etc per practice
}
```

---

## Sprint sub-agents

15 parallel agents building in isolation. Each module is its own file. Integration happens in `app.py` via `include_router` + handler imports.

| Agent | Module | Wave | Depends on Mila inputs |
|-------|--------|------|------------------------|
| A1 | `lib/outbound.py` + templates skeleton | 1 | partial (full personalization later) |
| A2 | `lib/track_b.py` expansion (4 practices) | 1 | partial (pricing in code, refines later) |
| A3 | `lib/contract.py` + Stripe/MP integration | 1 | no |
| A4 | `lib/reply_classify.py` + Resend inbound | 1 | no |
| A5 | `lib/prospecting.py` + Supabase prospects table | 1 | no |
| D1 | `lib/delivery/finops_audit.py` | 2 | yes (SOP) |
| D2 | `lib/delivery/ai_readiness.py` | 2 | yes (SOP) |
| D3 | `lib/delivery/devops_maturity.py` | 2 | yes (SOP) |
| D4 | `lib/delivery/growth_salesops.py` | 2 | yes (SOP) |
| D5 | `lib/delivery/industry.py` | 2 | yes (SOP) |
| O1 | `lib/eval_suite.py` | 3 | no |
| O2 | KPI dashboard upgrade (revenue tracking) | 3 | no |
| O3 | Brand voice RAG over Mila's posts | 3 | yes (her posts) |
| O4 | Migration SQL for prospects + engagements + contracts | 1 | no |
| O5 | n8n cron updates + webhook routing | 1 | no |

---

## Integration & deploy

After all agents return:
1. Verify all syntax: `python3 -c "import ast; ..."` per module
2. Run migration SQL against Supabase
3. Add env vars: STRIPE_SECRET_KEY, MERCADO_PAGO_TOKEN, APOLLO_API_KEY, CONTA_AZUL_TOKEN, RESEND_INBOUND_SECRET
4. `app.py` includes all new routers + registers all new handlers
5. Push to GitHub → auto-deploy via Coolify
6. Smoke tests per module
7. Hand off SPRINT_INPUTS_MILA.md for population
