# Anuvia Brand Voice — Rules for client-facing surfaces

This file codifies voice + UX rules every client-facing surface (emails, contract pages, sign/pix pages, intake forms, delivery emails, NPS) must follow.

## 1. No marketing-reassurance language

Strip every line that sounds like a sales pitch reassuring the reader.

Banned: "Documento curto, escopo fechado, sem letra miúda escondida." · "Leitura curta — três minutos." · "Sem cadastro inicial."

## 2. Voice plural — always

We are "Anuvia", "nosso time". Never first-person singular about the founder.

Banned: "leio todos", "Mila apresenta ao vivo", "Mila Vernazza analisa".

Exception: the actual legal contract PDF signature line carries "Mila Vernazza · Founder" because BR legal docs require a natural-person signatory.

## 3. No "Mila Vernazza · Founder" in email footers / page footers

Operational emails + transactional pages sign off as "Anuvia" only.

## 4. i18n is strict — no bilingual mixing on transactional pages

BR client (BRL) → PT only. US client (USD) → EN only. Side-by-side PT / EN is forbidden on sign, pix, contract emails, intake, delivery, NPS. The bilingual LP at anuvia.com.br/.net is the only exception.

Pattern: derive is_en from currency, build locale-keyed t = {...} dict, render with t["key"].

## 5. Payments email is generic, not personal

Pix confirmations, refunds, invoice questions → ANUVIA_PAYMENTS_EMAIL env var (defaults to pagamentos@anuvia.com.br). Never route financial flows through mila@anuvia.com.br.

## 6. White-glove ≠ founder-glove

Surface copy says "our team" / "nosso time" presents. Not "Mila presents".

## 7. Internal-operator copy can be casual

Slack DMs to Mila, admin dashboards, smoke endpoints, operator emails — may use first person or address Mila by name. Not covered by rules 1-6.
