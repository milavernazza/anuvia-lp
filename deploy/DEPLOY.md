# Anuvia — Rebuild runbook (v2, 2026-07-30)

Reconstrução da infra do zero após a pausa de junho. Stack simplificada:
**2 containers (app + Gotenberg) + Caddy TLS + Supabase managed + Resend +
Cloudflare DNS.** Sem Coolify, sem n8n, sem serviços extras.

Custo alvo: **€4-6/mês VPS + Anthropic pay-per-use**. Resto é free tier.

## Pré-requisitos (você faz — contas)

- [ ] **P0. Git local**: abrir Terminal e rodar:
      ```
      cd ~/Documents/Workspace/anuvia/04-agents/lp
      rm -f .git/index.lock && git rebase --abort
      git add -A && git commit -m "Rebuild v2: fixes + deploy kit"
      git push origin main --force-with-lease
      ```
      (o rebase quebrado de maio trava tudo; esses comandos destravam e
      sobem o working tree — que é a fonte de verdade com os 2 fixes)
- [ ] **P1. Domínio**: confirmar que `anuvia.com.br` ainda é seu no
      Registro.br (se expirou, renovar ~R$40). Decidir se mantém `anuvia.net`.
- [ ] **P2. Cloudflare**: logar em dash.cloudflare.com → verificar se a zona
      `anuvia.com.br` ainda existe (pausada). Se sim, un-pause. Se não,
      re-adicionar zona + atualizar nameservers no Registro.br.
- [ ] **P3. VPS novo**: Hetzner Cloud → criar **CX22** (€4.51/mês, Ubuntu 24.04,
      Falkenstein). Adicionar sua SSH key.
- [ ] **P4. Supabase**: criar projeto novo em supabase.com (free tier, região
      São Paulo se disponível). SQL Editor → rodar `deploy/schema_v2_full.sql`.
- [ ] **P5. Resend**: logar em resend.com → re-verificar domínio
      `send.anuvia.com.br` (os DNS records vão precisar ser recriados no
      Cloudflare — o Resend mostra quais). Gerar API key nova.
- [ ] **P6. Chaves novas**: gerar 5 secrets com `openssl rand -hex 32`
      (CONTRACT_HMAC_SECRET, TRACK_B_HMAC_SECRET, ORCHESTRATOR_SECRET,
      ADMIN_API_KEY, INBOUND_WEBHOOK_SECRET).
- [ ] **P7. Anthropic**: console.anthropic.com → API key nova.
- [ ] **P8. Slack**: recriar os 2 incoming webhooks (alerts + new lead).

## Deploy (30 min depois dos pré-requisitos)

```bash
# 1. SSH no VPS novo
ssh root@IP_DO_VPS

# 2. Docker
curl -fsSL https://get.docker.com | sh

# 3. Clonar o repo
git clone https://github.com/milavernazza/anuvia-lp.git /opt/anuvia
cd /opt/anuvia/deploy

# 4. Env vars
cp ENV_VARS.template .env
nano .env        # preencher com as chaves geradas nos pré-requisitos

# 5. Subir
docker compose up -d --build

# 6. Verificar
docker compose logs -f app | head -50
curl -s localhost:8000/ | head -5
```

## DNS (Cloudflare)

| Registro | Tipo | Valor | Proxy |
|---|---|---|---|
| anuvia.com.br | A | IP_DO_VPS | ✅ laranja |
| www | CNAME | anuvia.com.br | ✅ |
| send (+ records do Resend) | TXT/MX/CNAME | conforme Resend dashboard | ❌ cinza |

Subdomínios antigos (coolify, n8n, db, dashboard, cal, blog, roadmap):
**NÃO recriar**. Não existem mais na stack v2.

## Pós-deploy — smoke mínimo

```bash
# site up
curl -s -o /dev/null -w "%{http_code}" https://anuvia.com.br/        # 200

# admin auth
curl -s "https://anuvia.com.br/api/_admin/smoke/token"               # {"token": ...}

# debug resend (endpoint novo, incluído neste rebuild)
curl -s "https://anuvia.com.br/api/_admin/debug/resend_config?key=SEU_ADMIN_KEY"

# teste real de email
curl -s -X POST "https://anuvia.com.br/api/_admin/debug/resend_test?key=SEU_ADMIN_KEY" \
  -H "Content-Type: application/json" -d '{"to":"milavernazza@gmail.com"}'
```

Depois: reconectar Google Calendar em
`https://anuvia.com.br/api/admin/gcal/connect?key=SEU_ADMIN_KEY`.

## O que esta stack v2 NÃO tem (de propósito)

| Serviço antigo | Por que saiu | Volta quando |
|---|---|---|
| Coolify | docker compose + git pull resolve; menos 1 painel pra manter | nunca (provavelmente) |
| n8n (workflows 6-9) | proposal gen/daily brief/content multiplier não são o gargalo M0-M3 | M6+ se precisar |
| Supabase self-hosted | supabase.com free tier = zero manutenção | se estourar free tier (500MB) |
| Dashboard service separado | admin do próprio app cobre | nunca |
| PPTX sidecar | pre-discovery deck não é prioridade | M4+ na oferta Audit |
| Easyappointments | já tinha sido eliminado (slots locais) | — |

## Updates futuros (fluxo de deploy contínuo)

```bash
ssh root@IP_DO_VPS "cd /opt/anuvia && git pull && cd deploy && docker compose up -d --build"
```

Um comando. Sem Coolify, sem webhook, sem magia. Quando o time crescer (M7+),
migra pra GitHub Actions com deploy key.
