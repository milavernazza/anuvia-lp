# anuvia/lp

Layer 1 capture engine — interactive landing pages with diagnostic agents per funnel.

v1 ships only **BR_SMB** at `https://diagnostico.anuvia.com.br`. Future routes:
- `/eng` → BR_ENG (AI Readiness Assessment, PT-BR)
- `/us-smb` → US_SMB (BR-diaspora SMB, EN bilíngue)
- `/us-eng` → US_ENG (Production-Readiness Checklist, EN-US)

## Flow

```
Visitor → diagnostico.anuvia.com.br (GET /)
   ↓ (clicks "Começar diagnóstico")
6 multiple-choice questions + 4 contact fields (name, email, whatsapp obrigatório, company opcional)
   ↓ (POST /api/diagnose)
1. Validate form (Pydantic + WhatsApp BR normalize)
2. Call Claude Sonnet 4.6 with diagnostic system prompt
3. Insert lead in Supabase (funnel_id=BR_SMB, source=lp_diagnostic)
   → triggers Workflow 2 (lead enrichment) automatically via Supabase DB Webhook
   → if score >= 80, Workflow 3 (hot lead alert) fires Slack
4. Slack DM to Mila (best-effort) com resumo do diagnóstico
5. Return rendered HTML deliverable to the SPA → renderiza na tela
```

## Local run

```bash
pip install -r requirements.txt
SUPABASE_KEY=... ANTHROPIC_API_KEY=... uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000

Health: `curl http://localhost:8000/health`

## Environment variables

| Var | Required | Default | Notes |
|---|---|---|---|
| `SUPABASE_KEY` | yes | — | Service role JWT |
| `ANTHROPIC_API_KEY` | yes | — | Claude API key |
| `SUPABASE_URL` | no | `https://api.anuvia.com.br/rest/v1` | |
| `ANTHROPIC_MODEL` | no | `claude-sonnet-4-6` | |
| `SLACK_NEW_LEAD_WEBHOOK` | no | (empty) | Optional Slack incoming webhook for new lead alerts |

## Deploy via Coolify (Public Repository)

1. Coolify → project Anuvia → **+ New** → **Public Repository**
2. Repo: `https://github.com/milavernazza/anuvia-lp`
3. Branch `main`, Build Pack `Dockerfile`
4. Network: ports `8000`, network alias `anuvia-lp`, domain `https://diagnostico.anuvia.com.br`
5. Env vars: `SUPABASE_KEY`, `ANTHROPIC_API_KEY` (transfer via cookie bridge from n8n)
6. Service Name = `anuvia-lp`. Deploy.

## DNS (no Cloudflare Access — público)

1. Cloudflare DNS: A record `diagnostico.anuvia.com.br` → `91.99.170.97` — DNS only (gray cloud) for Letsencrypt initial issue
2. After SSL OK, you may flip to Proxied (orange cloud) for DDoS / bot mitigation
3. **No Cloudflare Access app** — this LP must be publicly reachable

## Design decisions

- **Single-page wizard** instead of multi-page form: better mobile UX, no page reloads, progress bar clear.
- **Multiple-choice everywhere** for the 6 diagnostic questions: friction-free, structured input → easier for Claude to parse and benchmark against ICP.
- **WhatsApp obrigatório**: Anuvia operates in BR where WhatsApp is the dominant channel. Will be used for follow-up once Twilio Meta approves.
- **Email diagnostic copy is a stub for v1**: shown on screen + lead row stores all data; transactional email send to be wired when SendGrid is provisioned.
- **No authentication** on the LP itself — it's a public capture page. Backend only writes to `leads` table; cannot read other rows.
