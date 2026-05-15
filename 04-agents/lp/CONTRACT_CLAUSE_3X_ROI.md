# Cláusula Contratual — Garantia 3× ROI (AWS Cost Audit)
# Contract Clause — 3× ROI Guarantee (AWS Cost Audit)

**Purpose.** This document contains the contractual language operationalizing Anuvia's public marketing claim: *"if we don't identify at least 3× the investment in annualized savings, you don't pay"* for the AWS Cost Audit offering (R$ 45–60k, 4-week engagement). It is drafted to be pasted as a standalone clause into a Master Services Agreement (MSA) or attached as a schedule to a Statement of Work (SOW).

**Target reader.** Mila's external counsel. The draft assumes counsel will harmonize defined terms (e.g., "Cliente", "Anuvia", "Auditoria") with the rest of the MSA and adjust formatting to the firm's house style.

**Known unknowns Mila must confirm with counsel before signature.** (1) Governing law and venue — placeholders are marked inline. (2) Currency of refund — whether refund is paid in the currency originally invoiced or converted at an FX reference (PTAX, spot, contract date). (3) Dispute resolution mechanism — courts vs. arbitration (CAM-CCBC, ICC). (4) Whether the guarantee should be capped at 100% of the Audit Fee paid, or at total contract value across follow-on services. (5) Whether an NDA is bundled into the same instrument or kept separate.

---

## Cláusula em Português (contratos brasileiros)

### CLÁUSULA [N] — GARANTIA DE IDENTIFICAÇÃO DE ECONOMIAS (3× ROI)

#### 1. Objeto da Garantia

1.1. A Anuvia compromete-se a entregar ao Cliente, no prazo do engajamento de Auditoria de Custos AWS ("Auditoria"), um relatório final ("Relatório Final") contendo recomendações de remediação cujas Economias Anualizadas Identificadas (conforme definido na Cláusula 3) sejam, no mínimo, equivalentes a 3 (três) vezes o valor dos honorários pagos pelo Cliente pela Auditoria ("Honorários da Auditoria").

1.2. Caso, ao final do procedimento descrito nesta Cláusula, as Economias Anualizadas Identificadas não atinjam o limiar de 3× os Honorários da Auditoria, o Cliente fará jus ao reembolso integral dos Honorários da Auditoria, nos termos da Cláusula 5.

1.3. Esta garantia aplica-se exclusivamente aos Honorários da Auditoria e não se estende a quaisquer serviços de implementação, retainers, consultoria contínua ou engajamentos subsequentes contratados separadamente.

#### 2. Condições de Elegibilidade do Cliente

A presente garantia é concedida sob a condição cumulativa de que o Cliente atenda, durante toda a vigência da Auditoria, aos seguintes requisitos:

2.1. **Acesso técnico.** O Cliente concederá à Anuvia acesso de leitura ("read-only") aos seguintes serviços AWS da(s) conta(s) auditada(s): AWS Cost Explorer, AWS Trusted Advisor, AWS Compute Optimizer, AWS Billing & Cost Management e AWS Resource Discovery (ou serviços equivalentes que venham a substituí-los).

2.2. **Dados de faturamento.** O Cliente fornecerá os 12 (doze) meses imediatamente anteriores de dados de faturamento AWS (Cost & Usage Reports ou exportações equivalentes), em formato íntegro e não redigido.

2.3. **Tempo mínimo de conta.** A(s) conta(s) AWS objeto da Auditoria deverá(ão) possuir, no mínimo, 12 (doze) meses de existência ("AWS tenure") na data de início da Auditoria. Contas recém-criadas ("greenfield") estão excluídas do escopo desta garantia.

2.4. **Gasto mensal mínimo.** O gasto mensal médio do Cliente na AWS, considerada a média dos 3 (três) meses imediatamente anteriores à assinatura, deverá ser igual ou superior a USD 30.000,00 (trinta mil dólares dos Estados Unidos da América) ou seu equivalente em BRL, calculado pela taxa PTAX de venda divulgada pelo Banco Central do Brasil na data de assinatura.

2.5. **Exclusividade do engajamento FinOps.** O Cliente declara que não está, na data de início da Auditoria, contratando ou recebendo serviços concorrentes de auditoria FinOps, otimização de custos AWS ou natureza equivalente, prestados por terceiros, sobre as mesmas contas objeto desta Auditoria.

2.6. **Estabilidade operacional.** Durante a Auditoria, o Cliente compromete-se a comunicar formalmente à Anuvia, em até 2 (dois) dias úteis, qualquer reorganização corporativa relevante, migração significativa de cargas de trabalho, operação de fusão ou aquisição, ou evento equivalente que afete materialmente a base de custos AWS. Nesta hipótese, as partes acordarão uma data de corte ("cutoff date") para fins de congelamento das constatações da Auditoria, sem que tal evento prejudique a garantia.

#### 3. Definição de "Economias Anualizadas Identificadas"

3.1. Para os fins desta Cláusula, entende-se por "Economias Anualizadas Identificadas" o valor agregado, em USD ou BRL, das oportunidades de remediação documentadas pela Anuvia no Relatório Final, classificadas em pelo menos uma das seguintes categorias:

   (a) computação (EC2, Lambda, ECS, EKS, Fargate e correlatos);
   (b) armazenamento (EBS, S3, EFS, FSx, Glacier e correlatos);
   (c) rede (NAT Gateway, Load Balancers, VPC endpoints e correlatos);
   (d) transferência de dados ("data transfer");
   (e) SaaS de terceiros faturados via AWS Marketplace;
   (f) rebalanceamento de Reserved Instances e Savings Plans.

3.2. Cada oportunidade será **anualizada** pela projeção do valor mensal de economia sobre um período de 12 (doze) meses, tomando-se como referência ("baseline") o consumo verificado no período acordado entre as partes no kickoff da Auditoria.

3.3. As Economias Anualizadas Identificadas **não estão condicionadas** à efetiva implementação das recomendações pelo Cliente. A Anuvia assume obrigação de meio quanto à identificação e documentação; a obrigação de implementação, quando contratada, é objeto de instrumento separado.

#### 4. Procedimento de Contestação ("Dispute Window")

4.1. O Cliente terá o prazo de 30 (trinta) dias corridos, contados da data de entrega formal do Relatório Final, para contestar, por escrito, qualquer constatação cuja Economia Anualizada Identificada considere incorreta.

4.2. A contestação deverá ser tecnicamente fundamentada, mediante apresentação de evidências objetivas, incluindo, exemplificativamente: logs de execução, capturas de tela de configuração, linhas de fatura AWS, registros de Reserved Instances ou Savings Plans existentes e demais elementos pertinentes.

4.3. As partes negociarão de boa-fé a resolução das contestações apresentadas, dispondo de 15 (quinze) dias úteis, contados da apresentação tempestiva da contestação, para acordarem o valor final.

4.4. Não havendo contestação tempestiva, o Relatório Final torna-se definitivo e seu valor agregado constitui as "Economias Anualizadas Identificadas Finais".

#### 5. Acionamento do Reembolso

5.1. Se, encerrado o procedimento de contestação descrito na Cláusula 4, o valor das Economias Anualizadas Identificadas Finais for inferior a 3× os Honorários da Auditoria, o Cliente terá direito ao reembolso integral dos Honorários da Auditoria efetivamente pagos.

5.2. O reembolso será processado pela Anuvia no prazo de 15 (quinze) dias úteis contados do encerramento da janela de contestação, na mesma forma, conta e moeda do pagamento original, salvo se as partes acordarem por escrito de forma diversa.

5.3. O reembolso previsto nesta Cláusula constitui o único e exclusivo remédio do Cliente em razão do não atingimento do limiar de 3× ROI, ficando expressamente afastada qualquer pretensão indenizatória adicional, lucros cessantes ou danos consequenciais.

#### 6. Exclusões e Limitações

6.1. **Economias realizadas.** A Anuvia não garante o valor das economias efetivamente realizadas pelo Cliente após a Auditoria, uma vez que tal realização depende exclusivamente da execução das recomendações pelo Cliente. Esta Cláusula refere-se exclusivamente às Economias Anualizadas **Identificadas**, conforme definição da Cláusula 3.

6.2. **Comportamento futuro de custos.** Não há qualquer garantia, expressa ou implícita, quanto ao comportamento futuro dos custos da(s) conta(s) AWS auditada(s), incluindo variações de preço pela AWS, mudanças no padrão de consumo do Cliente ou alterações na arquitetura.

6.3. **Obstrução do Cliente.** Caso o Cliente, por ação ou omissão material, deixe de prover acesso técnico, dados de faturamento ou informações solicitadas pela Anuvia, e tal falha comprometa a Auditoria, a garantia prevista nesta Cláusula será considerada **automaticamente extinta**, permanecendo integralmente devidos os Honorários da Auditoria. A Anuvia notificará o Cliente por escrito de qualquer evento de obstrução, concedendo prazo de 5 (cinco) dias úteis para regularização antes de invocar esta subcláusula.

6.4. **Caso fortuito e força maior.** Eventos de caso fortuito ou força maior, incluindo, exemplificativamente, indisponibilidade de serviços AWS que afetem o período de análise, suspendem os prazos previstos nesta Cláusula pelo período correspondente, sem que tal suspensão acarrete a extinção do engajamento ou da garantia.

#### 7. Lei Aplicável e Foro

7.1. Esta Cláusula é regida pelas leis da República Federativa do Brasil.

7.2. Fica eleito o foro da Comarca de **[São Paulo, Brasil — a confirmar]** para dirimir quaisquer controvérsias decorrentes desta Cláusula, com renúncia a qualquer outro, por mais privilegiado que seja, ressalvada eventual cláusula compromissória diversa pactuada pelas partes no instrumento principal.

---

## Clause in English (international contracts)

### CLAUSE [N] — IDENTIFIED SAVINGS GUARANTEE (3× ROI)

#### 1. Scope of the Guarantee

1.1. Anuvia agrees to deliver to the Client, within the term of the AWS Cost Audit engagement (the "Audit"), a final report (the "Final Report") containing remediation recommendations whose aggregate Identified Annualized Savings (as defined in Clause 3) are at least equal to three (3×) times the fees paid by the Client for the Audit (the "Audit Fee").

1.2. If, upon conclusion of the procedure set forth in this Clause, the Identified Annualized Savings do not reach the 3× Audit Fee threshold, the Client shall be entitled to a full refund of the Audit Fee, in accordance with Clause 5.

1.3. This guarantee applies exclusively to the Audit Fee and does not extend to any implementation services, retainers, ongoing advisory, or follow-on engagements contracted separately.

#### 2. Client Eligibility Conditions

This guarantee is granted on the cumulative condition that, throughout the Audit, the Client complies with the following requirements:

2.1. **Technical access.** The Client shall grant Anuvia read-only access to the following AWS services in the audited account(s): AWS Cost Explorer, AWS Trusted Advisor, AWS Compute Optimizer, AWS Billing & Cost Management, and AWS Resource Discovery (or any equivalent successor services).

2.2. **Billing data.** The Client shall provide the most recent twelve (12) months of AWS billing data (Cost & Usage Reports or equivalent exports), in complete and unredacted form.

2.3. **Account tenure.** The AWS account(s) subject to the Audit shall have a minimum tenure of twelve (12) months as of the Audit start date. Greenfield accounts are excluded from the scope of this guarantee.

2.4. **Minimum monthly spend.** The Client's average monthly AWS spend, calculated over the three (3) months immediately preceding signature, shall be at least USD 30,000.00 (thirty thousand United States dollars), or its equivalent in BRL converted at the PTAX selling rate published by the Central Bank of Brazil on the signature date.

2.5. **Exclusivity of the FinOps engagement.** The Client represents that, as of the Audit start date, it is not engaging or receiving concurrent FinOps, AWS cost optimization, or substantively equivalent services from any third party in respect of the same account(s) covered by this Audit.

2.6. **Operational stability.** During the Audit, the Client shall notify Anuvia in writing, within two (2) business days, of any material corporate reorganization, significant workload migration, merger or acquisition transaction, or equivalent event materially affecting the AWS cost baseline. In such case, the parties shall agree on a cutoff date for purposes of freezing the Audit findings, without prejudice to the guarantee.

#### 3. Definition of "Identified Annualized Savings"

3.1. For the purposes of this Clause, "Identified Annualized Savings" means the aggregate value, in USD or BRL, of the remediation opportunities documented by Anuvia in the Final Report, classified under at least one of the following categories:

   (a) compute (EC2, Lambda, ECS, EKS, Fargate, and related services);
   (b) storage (EBS, S3, EFS, FSx, Glacier, and related services);
   (c) network (NAT Gateway, Load Balancers, VPC endpoints, and related services);
   (d) data transfer;
   (e) third-party SaaS billed via AWS Marketplace;
   (f) rebalancing of Reserved Instances and Savings Plans.

3.2. Each opportunity shall be **annualized** by projecting its monthly savings value over a twelve (12) month period, using as baseline the consumption observed during the period agreed upon by the parties at Audit kickoff.

3.3. Identified Annualized Savings are **not contingent** on the Client's actual implementation of the recommendations. Anuvia's obligation under this Clause is one of means with respect to identification and documentation; any implementation obligation, if engaged, shall be governed by a separate instrument.

#### 4. Dispute Window

4.1. The Client shall have thirty (30) calendar days from the date of formal delivery of the Final Report to dispute, in writing, any finding whose Identified Annualized Savings figure it considers incorrect.

4.2. The dispute shall be technically substantiated by objective evidence, including, by way of example: execution logs, configuration screenshots, AWS billing line items, records of existing Reserved Instances or Savings Plans, and other relevant materials.

4.3. The parties shall negotiate in good faith to resolve any disputes raised, having fifteen (15) business days from the timely submission of the dispute to agree on the final value.

4.4. If no timely dispute is raised, the Final Report becomes definitive and its aggregate value shall constitute the "Final Identified Annualized Savings".

#### 5. Refund Trigger

5.1. If, upon conclusion of the dispute procedure described in Clause 4, the Final Identified Annualized Savings are less than 3× the Audit Fee, the Client shall be entitled to a full refund of the Audit Fee actually paid.

5.2. The refund shall be processed by Anuvia within fifteen (15) business days from the closing of the dispute window, in the same form, account, and currency of the original payment, unless the parties agree otherwise in writing.

5.3. The refund set forth in this Clause constitutes the Client's sole and exclusive remedy in connection with the failure to meet the 3× ROI threshold, and any additional claim for indemnification, lost profits, or consequential damages is expressly excluded.

#### 6. Exclusions and Limitations

6.1. **Realized savings.** Anuvia does not guarantee the value of savings actually realized by the Client following the Audit, as such realization depends solely on the Client's execution of the recommendations. This Clause refers exclusively to **Identified** Annualized Savings, as defined in Clause 3.

6.2. **Future cost behavior.** No warranty, express or implied, is given as to the future cost behavior of the audited AWS account(s), including AWS pricing changes, shifts in Client consumption patterns, or architectural changes.

6.3. **Client obstruction.** If the Client, through material action or omission, fails to provide technical access, billing data, or information requested by Anuvia, and such failure compromises the Audit, the guarantee under this Clause shall be **automatically void**, and the Audit Fee shall remain fully payable. Anuvia shall notify the Client in writing of any such obstruction event, granting a cure period of five (5) business days before invoking this subclause.

6.4. **Force majeure.** Force majeure events, including, by way of example, AWS service outages affecting the analysis period, shall suspend the deadlines set forth in this Clause for the corresponding period, without terminating the engagement or the guarantee.

#### 7. Governing Law and Venue

7.1. This Clause shall be governed by the laws of the **[State of Delaware, USA — to be confirmed]**.

7.2. The parties submit to the exclusive jurisdiction of the competent courts of **[State of Delaware, USA — to be confirmed]** for any disputes arising out of this Clause, subject to any different arbitration clause agreed by the parties in the principal instrument.

---

## Notes for Mila's Lawyer

- **Refund cap.** Decide whether the refund is capped at 100% of the Audit Fee actually paid (current draft) or at total contract value including any concurrently signed implementation/retainer schedules. Current draft is the more conservative position for Anuvia.
- **Dispute resolution forum.** Choose between (i) state courts (current placeholder), (ii) institutional arbitration (CAM-CCBC for PT version, AAA/ICC for EN version), or (iii) a tiered escalation (negotiation → mediation → arbitration). Confirm consistency with the rest of the MSA.
- **Client remediation timeline.** Recommendation: do **not** require the Client to commit to a remediation timeline. Tying the guarantee to client execution expands Anuvia's liability surface and dilutes the obligation-of-means framing in Clause 3.3. Keep liability scope narrow.
- **NDA bundling.** Decide whether confidentiality obligations are (i) bundled into this clause as a separate subclause, (ii) handled in a parallel NDA, or (iii) addressed in the umbrella MSA. Given that the Audit involves billing data and architectural detail, an NDA in some form is mandatory; only the placement is open.
- **Partial implementation carve-out.** Some findings (notably Reserved Instance and Savings Plan rebalancing) require AWS Organizations Master account access for execution. Consider adding a subclause clarifying that such findings remain valid "Identified" savings for guarantee purposes even where the Client cannot, or chooses not to, provide Master account access for execution.
- **FX formalization.** Clauses 2.4 and 5.2 reference PTAX and "same currency as paid". Confirm whether refunds should be FX-protected (e.g., if invoiced in USD but paid in BRL, refund the BRL amount or reconvert at refund date).
- **Tax treatment of refund.** Confirm with counsel and Anuvia's accountant whether the refund mechanism interacts with ISS, PIS/COFINS, or invoice cancellation procedures under Brazilian tax law, and whether a credit note or invoice cancellation is the cleaner instrument.
- **Severability.** Confirm that the umbrella MSA contains a standard severability clause that would preserve the rest of this clause if any subclause were judicially struck.
