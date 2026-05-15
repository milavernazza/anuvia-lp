# Anuvia Funnel — Validation Checklist v1

**Updated:** 2026-05-13
**Purpose:** Single source of truth for what works, what should be tested, and what's still pending. Mila uses this to iterate until 100% confidence before pushing real traffic.

---

## How to use this doc

For each section: read the **Expected behavior** column, then run the **Test** column. Mark each row ✅ (works as expected) or ❌ (fails, log in Issues section at the bottom). Iterate top-to-bottom — earlier sections gate later ones.

---

## SECTION 1 — Site rendering & visual

| # | Item | Expected behavior | Test |
|---|------|-------------------|------|
| 1.1 | Tailwind loads | Site renders styled (fonts Playfair/Inter, max-w containers, proper spacing). No raw HTML. | Open https://anuvia.com.br/ in incognito; check fonts + layout. |
| 1.2 | i18n strict host | anuvia.com.br always PT, anuvia.net always EN, regardless of browser locale. | Open both domains side-by-side. Confirm headlines + body copy match locale. |
| 1.3 | Authority strip on every LP | "15+ years inside hyperscalers · Ex-AWS · Ex-Google · Ex-MongoDB · 15× AWS-certified · MongoDB-certified · GCP-certified". No personal name. | Visit /cloud, /engineering, /ai, /growth, /industry, /cloud/finops/audit, /cloud/aws/well-architected, /cloud/aws/migration, /cloud/aws/landing-zone, /cloud/aws/security-posture, /cloud/gcp/migration. |
| 1.4 | Home 6 cards | 5 practice cards + "Comece por aqui" card all in editorial-light style. No invisible white-on-white text. | https://anuvia.com.br/ scroll to "Uma firma. Cinco competências profundas." section. |
| 1.5 | Garantia traduzida readable | Dark grey text on light/bordered card. Numbers readable. | https://anuvia.com.br/cloud/finops/audit scroll to "A garantia, traduzida". |
| 1.6 | Dark CTA cards readable | Industry CTAs, Cloud orientation cards, AI Ops cards all have dark bg + readable light text. | Industry, practice_cloud, practice_growth, practice_ai, practice_industry pages. |
| 1.7 | Blog renders bilingually | blog.anuvia.com.br shows PT posts, anuvia.net/blog shows EN posts. Cross-domain home link works. | Click any blog post; click "Anuvia ←" home button. Should go to anuvia.com.br or anuvia.net respectively. |

---

## SECTION 2 — Diagnostic-first flow (Track A: Discovery-led)

| # | Item | Expected behavior | Test |
|---|------|-------------------|------|
| 2.1 | Diagnostic form loads | All 6 practice diagnostics render their analyze form. | Visit /cloud/finops/audit, /engineering/devops-maturity, /ai/readiness, /growth/sales-ops, /industry/assessment, /cloud/aws/well-architected, etc. Scroll to form. |
| 2.2 | Anonymous lead inserted | After submitting analyze form, row appears in Supabase `leads` with funnel_id (e.g. BR_FINOPS), anonymous_diagnostic=true, deliverable_html populated, current_stage='new'. | After form submit, check `SELECT * FROM leads ORDER BY created_at DESC LIMIT 1`. |
| 2.3 | Analysis HTML returned | Inline analysis card appears with savings estimate / readiness score / etc. Specific to the practice. | Visual: review inline analysis card after submitting form. |
| 2.4 | PII upgrade via /contact | After submitting contact form, the SAME lead row gets name/email/whatsapp/company. No duplicate row. current_stage='qualified'. | Submit contact form; check Supabase: should see existing row updated, not new row. |
| 2.5 | Email arrives via Resend | Email with deliverable HTML + report. From contato@anuvia.com.br or send.anuvia.com.br. Subject mentions diagnostic name. | Use real email; check inbox within 30s. |
| 2.6 | Footer is Anuvia, not personal | Email footer: "Anuvia · Engenharia sênior em Cloud, IA, Plataforma e RevOps" + credentials list. **No** "Mila Vernazza · Founder Anuvia". | Visual: scroll to email footer. |
| 2.7 | CTA in email has lead_id | "Agendar conversa" link goes to `https://anuvia.com.br/contact?lead_id={uuid}`. | Right-click → copy link. Should have ?lead_id= query string. |
| 2.8 | Cookie set on /contact submit | After contact form: cookies `anuvia_lead_id` (HttpOnly, 7-day, SameSite=Lax) set on response. | Browser DevTools → Application → Cookies → anuvia.com.br. |
| 2.9 | Booking widget recognizes returning lead | Reopen browser, click email CTA. /contact?lead_id=X redirects to /contact (clean URL) with cookie set. Widget shows "Agendando como NAME (email)" + hides PII fields. Only date/time + optional context visible. | Click email "Agendar conversa" link in fresh browser tab. Check widget visible state. |
| 2.10 | Slots respect Gcal busy | Slots in widget exclude times Mila has events in any active calendar. | Check today/tomorrow; compare to her real calendar. |
| 2.11 | Holiday blocking | BR national holidays (Carnaval, etc.) show no slots. | Click 2026-04-21 (Tiradentes) — should be empty. |
| 2.12 | Booking submission | Submit a slot. Response: `{ok: true, appointment_id, pretty, lead_id}`. Same lead_id as before — MERGED, not new lead. | Check Supabase: should still be same row, now with current_stage='qualified' or 'discovery_scheduled'. |
| 2.13 | 2 Gcal events created on Mila's calendar | (a) "Anuvia · Discovery · {diag label}" public, with lead as attendee + Meet link. (b) "Brief · {diag label} · {lead name}" private, no attendees, full brief in description. | Open Google Calendar for the booked time. Verify both events. |
| 2.14 | Public event description is CLEAN | Event A description: 3-5 sentences explaining the call. NO lead_id, NO Supabase user, NO qualification_data details. | Open Event A in calendar, read description. |
| 2.15 | Private event has full brief | Event B description: lead_id, name, email, phone, company, funnel_id, qualification_data summary, context, insights. | Open Event B (private/confidential) in calendar. |
| 2.16 | Confirmation email + ICS | Email "Conversa agendada" with Meet link OR fallback message, ICS file attached. ICS opens in Apple/Outlook/Gmail with Aceitar/Talvez/Recusar buttons. | Open email; download .ics; double-click. |
| 2.17 | Easyappointments title clean | The Easyappointments-synced calendar event (if visible) shows generic title "Anuvia · Discovery" not "Discovery Call — Growth Mesh BR". **Requires Mila to rename services 2 + 3 in Easyappointments admin or via SQL.** | Open Gcal, look for 3rd event (Easyappointments sync). Title should be neutral. |

---

## SECTION 3 — Autonomous funnel (Track B: Growth close)

| # | Item | Expected behavior | Test |
|---|------|-------------------|------|
| 3.1 | Classify_track routes to discovery for non-growth | FinOps/AI/Engineering/Industry leads → track='discovery'. | Create FinOps lead via API/UI; trigger orchestrator tick; verify track='discovery'. |
| 3.2 | Classify_track routes to autonomous when signals present | Growth + budget_declared + urgency + small team → track='autonomous'. | Create Growth lead with all 3 signals (currently no UI for these — use API: `POST /api/growth-sales-ops/analyze` with extras `budget_declared=true, urgency='high', company_size='1-10'`). Tick orchestrator. Verify track='autonomous'. |
| 3.3 | Proposal generated in ~15 min | next_action='generate_proposal_v1' fires. PDF appears at /static/proposals/{lead_id}_v1.pdf. Email sent. | Wait 15 min after autonomous classify, or manually patch next_action_at to now and tick. |
| 3.4 | Proposal email arrives | Subject mentions Anuvia + proposta. Body has link to PDF + Aceitar CTA with HMAC token. PT or EN based on locale. | Use real email; verify content. |
| 3.5 | HMAC accept link works | Click "Aceitar proposta" in email → goes to /api/track-b/accept?lead_id=X&token=Y → returns thank-you HTML. Lead lifecycle_status='proposal_signed'. | Test with real email; click accept. |
| 3.6 | Followup_proposal_d2 fires after 2 days | If no engagement, nudge email sent. lifecycle_status unchanged. next_action='followup_proposal_d5'. | Test by patching next_action_at to past, run tick. |
| 3.7 | Engagement signal pauses followups | If signal `email_open` or `email_click` exists after proposal_sent ts, d2/d5 handlers bail out. lifecycle_status='proposal_opened'. | Open proposal email → Resend webhook fires → /api/track-b/email-event records signal. |
| 3.8 | Ghosted after 10 days | If no engagement through d2 + d5, close_ghosted_d10 fires. lifecycle_status='ghosted'. | Test with synthetic lead, manually patch dates. |

---

## SECTION 4 — Orchestrator & memory

| # | Item | Expected behavior | Test |
|---|------|-------------------|------|
| 4.1 | All 5 handlers registered | classify_track, generate_proposal_v1, followup_proposal_d2, followup_proposal_d5, close_ghosted_d10. | `curl https://anuvia.com.br/api/orchestrator/handlers` |
| 4.2 | In-process scheduler ticks every 10 min | Logs show "orchestrator scheduled tick: {...}" periodically. | Coolify logs. |
| 4.3 | Daily smoke runs at 7am BRT | Script `scripts/smoke_e2e.py` runs, creates synthetic lead, walks funnel, alerts Slack on fail. | Wait for next 7am. Or trigger manually. |
| 4.4 | agent_history persisted | After each handler, lead.qualification_data... no, leads.agent_history (jsonb column). Each tick appends. | `SELECT id, agent_history FROM leads WHERE id='...'`. |
| 4.5 | Retries on transient failure | 3× attempts with backoff. Alert to Slack on 3rd fail. | Force an error (e.g., temporarily set RESEND_API_KEY wrong); observe alert. |
| 4.6 | Slack alerts wired | SLACK_ALERTS_WEBHOOK env var set. Failures post to channel. | Channel #anuvia-alerts (or wherever). |

---

## SECTION 5 — Security & data hygiene

| # | Item | Expected behavior | Test |
|---|------|-------------------|------|
| 5.1 | admin_gcal_accounts RLS on | RLS enabled. anon role cannot read. Service role still reads (app works). | `SELECT relrowsecurity FROM pg_class WHERE relname='admin_gcal_accounts'` → t. |
| 5.2 | Only 1 active Gcal account | mila@anuvia.com.br active=true, scope=calendar.events. Old accounts deactivated. | `SELECT email, is_active FROM admin_gcal_accounts`. |
| 5.3 | Lead PII never exposed in client emails | Footer is Anuvia org credentials. No personal data in public/client-facing event descriptions. | Already covered in 1.x, 2.6, 2.14. |
| 5.4 | Smoke leads cleaned | No `smoke+%` or `milavernazza+id%` leads accumulating. | `SELECT count(*) FROM leads WHERE email LIKE 'smoke+%' OR email LIKE 'milavernazza+id%'`. Should grow during testing then be cleaned periodically. |
| 5.5 | Cloudflare Access on admin panels | /api/admin/* requires CF Access + ADMIN_API_KEY query param. | Try without auth → 401 or CF login. |
| 5.6 | GitHub App auto-deploy | Push to main triggers Coolify redeploy via webhook from "anuvia-github" GitHub App. | Push trivial commit; observe Coolify deployment within 30-60s. |

---

## SECTION 6 — Email & calendar polish

| # | Item | Expected behavior | Test |
|---|------|-------------------|------|
| 6.1 | "Estimativa preliminar" block visual | In diagnostic-report email: solid bg card, 36px serif amount, eyebrow + sub-explanation hierarchy. | Open diagnostic email in Gmail/Apple Mail. |
| 6.2 | Multi-calendar freebusy | If Mila has secondary calendars connected with `is_active=true`, all their busy times block slots. | Test by adding event on secondary cal → confirm slot disappears. |
| 6.3 | Tailwind self-hosted | /static/css/tailwind.css served 200, 17KB, no fragile CDN dependency. | `curl -I https://anuvia.com.br/static/css/tailwind.css`. |
| 6.4 | Watermarked sample PDFs | /static/samples/sample_*.pdf accessible, watermarked "SAMPLE · CONFIDENTIAL". | Open https://anuvia.com.br/static/samples/sample_finops_audit_report.pdf. |

---

## SECTION 7 — Pending (not yet validated)

These items are partially built or not yet integrated. Address before claiming 100%.

- [ ] **Dashboard repo `anuvia-dashboard` sync.** Coolify source still pointing at deprecated config (auto-deploy failed earlier). Swap to `anuvia-github` source, then update dashboard code to read new columns (lifecycle_status, track, agent_history, etc.).
- [ ] **Easyappointments service rename.** Run SQL on Easyappointments MySQL DB to rename services 2 + 3 to generic "Anuvia · Discovery". (Provided in handoff doc.)
- [ ] **Track B autonomous UI signals.** Growth analyze form doesn't have UI fields for budget_declared / urgency / company_size. Add explicit dropdowns/toggles so leads can self-declare and trigger autonomous routing without curl.
- [ ] **Resend webhook smoke.** Manually open the proposal email, verify `email_opened` signal appears in lead.signals jsonb. If not, debug Svix signature verification.
- [ ] **End-to-end mobile test.** Booking widget on mobile (iPhone Safari, Android Chrome). Form fields, slot picker, submit flow.
- [ ] **Workflow 9 Phase 2 Slack DM drafts.** Long-pending bug (task #30) — blocks content velocity.
- [ ] **Real case studies.** /cases page has placeholders. Document 3 real engagements anonymized.
- [ ] **Cold outbound.** Not started.

---

## SECTION 8 — Manual steps Mila needs to do once

These are one-time setup actions, NOT recurring tests.

1. **Rename Easyappointments services** (cal.anuvia.com.br/index.php/backend → Services → 2 and 3 → "Anuvia · Discovery"). Or SQL:
   ```sql
   UPDATE ea_services SET name = 'Anuvia · Discovery',
     description = 'Veja o calendário do organizador para detalhes.'
     WHERE id IN (2, 3);
   ```

2. **Configure Resend webhook** at https://resend.com/webhooks:
   - Endpoint: `https://anuvia.com.br/api/track-b/email-event`
   - Events: email.opened, email.clicked, email.bounced, email.complained
   - Save signing secret as env `RESEND_WEBHOOK_SECRET` in Coolify

3. **Delete leftover test events** from Mila's Gcal (14 May 09:00, 14 May 09:30, 18 May 12:00, 19 May 11:00, 20 May 10:00).

4. **Reset old Easyappointments admin appointments** (IDs ~7-14 from smoke tests). Delete via cal.anuvia.com.br admin if cluttering.

---

## SECTION 9 — Issues log (fill in as you find bugs)

| Date | Section | Issue | Status |
|------|---------|-------|--------|
| ... | ... | ... | ... |

---

## SECTION 10 — Definition of "done" (100%)

Claim 100% when ALL of:

- [ ] Sections 1, 2, 3, 4, 5, 6 all ✅ on a fresh manual test (incognito, real email, both PT and EN)
- [ ] Section 7 pending items all resolved or explicitly deferred with a date
- [ ] Section 8 manual steps all done
- [ ] One real (non-test) lead completed the full Track A flow successfully
- [ ] One real (non-test) lead completed the full Track B flow successfully (Growth + autonomous signals)
- [ ] No critical issues in Section 9 for 7 consecutive days

---

**Maintained at:** `/04-agents/lp/VALIDATION_CHECKLIST.md` in the anuvia-lp repo. Edit and commit as you go.
