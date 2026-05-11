---
title: "Shadow deployment com Evidently AI: drift detectado em 2h"
slug: shadow-deployment-evidently-ai-drift-deteccao-2h
date: 2026-05-11
author: Mila Vernazza
audience: engineering
language: pt
subtitle: "Como evitar dias de produção perdidos por model drift sazonal com observability real"
excerpt: "Um cliente perdeu 3 dias de produção por drift sazonal não detectado. Implementamos shadow deployment com Evidently AI e reduzimos o tempo de detecção para 2 horas."
tags:
  - MLOps
  - model drift
  - Evidently AI
  - shadow deployment
  - observability
  - produção
  - engineering
  - pt
---

## O problema que ninguém quer ter numa segunda-feira

O time de dados recebe um alerta: o modelo de scoring está retornando valores estranhos. Alguém puxa os logs. Outro abre o dashboard de negócio. O número bate — mas está errado. Três dias depois, com prejuízo acumulado e a confiança da área comercial no chão, o diagnóstico chega: **model drift sazonal**.

Esse foi o cenário real de um cliente nosso. Mudança de comportamento no dado de entrada — típica de virada de período, campanha ou sazonalidade — e o modelo de scoring continuou inferindo como se nada tivesse mudado. Sem alertas. Sem fallback. Sem visibilidade.

Três dias de produção degradada.

---

## Por que drift sazonal é traiçoeiro

Drift de dados tem duas formas principais: **data drift** (a distribuição do input muda) e **concept drift** (a relação entre input e output muda). O drift sazonal costuma combinar os dois.

O modelo foi treinado com dados de um período. Chega uma campanha, uma virada de mês, um comportamento novo de usuário — e a distribuição real se afasta silenciosamente da distribuição de treino. O modelo não quebra. Ele **continua respondendo**. Só que errado.

Esse é o pior tipo de falha em produção: a silenciosa.

---

## A solução: shadow deployment com Evidently AI

A estratégia que implementamos tem três componentes principais:

### 1. Shadow deployment paralelo

O modelo novo — ou o modelo candidato a substituir o atual em períodos de alta variação — roda **em paralelo**, sem servir as predições reais. Ele recebe o mesmo tráfego de entrada, processa, mas o output vai apenas para observação.

Isso elimina o risco de colocar um modelo não validado em produção enquanto ainda permite comparar comportamento em tempo real.

### 2. Monitoramento contínuo com Evidently AI

O [Evidently AI](https://www.evidentlyai.com/) foi configurado para calcular métricas de drift em janelas deslizantes sobre o stream de dados de entrada. Os testes de distribuição (PSI, KS, Jensen-Shannon dependendo da feature) rodam automaticamente e alimentam um painel centralizado.

- **Alertas configurados** para thresholds de PSI > 0.2 em features críticas do scoring
- **Reports periódicos** gerados automaticamente com breakdown por feature
- **Integração com o pipeline** via chamadas ao SDK do Evidently embutidas no job de inferência

### 3. Janela de decisão clara

O ponto mais subestimado: definir **o que acontece quando o alerta dispara**. Criamos um runbook simples:

- Drift detectado → alerta no canal de dados no Slack
- On-call valida em até 30 minutos
- Se confirmado → fallback automático para modelo anterior ou regra heurística
- Abertura de ticket para retraining com dados do novo período

---

## O resultado

Com a arquitetura rodando, o mesmo tipo de drift sazonal que antes ficou invisível por 72 horas foi **detectado em menos de 2 horas**. O modelo em shadow começou a mostrar divergência de distribuição nas features de recência e frequência logo nas primeiras inferências pós-virada de período.

O time tomou a decisão de rollback antes do modelo impactar qualquer decisão comercial crítica.

---

## O que isso exige de stack

Não há necessidade de trocar tudo. O Evidently AI funciona bem integrado a ambientes que já usam:

- **Airflow** para orquestração dos jobs de monitoramento
- **MLflow** para versionamento e comparação dos modelos em shadow vs. produção
- **Snowflake / BigQuery** como fonte dos dados de referência (baseline de treino)
- Qualquer infraestrutura de serving — desde FastAPI simples até Vertex AI ou SageMaker

O custo de inferência do shadow aumenta, mas é gerenciável: você está rodando dois modelos, mas apenas um serve. Para a maioria dos casos de scoring em PMEs, o overhead é aceitável diante do risco evitado.

---

## Conclusão

Model drift em produção não é questão de **se** vai acontecer — é de **quando**. A diferença entre 3 dias de degradação e 2 horas de detecção está na arquitetura de observability que você coloca antes do problema aparecer.

Shadow deployment com Evidently AI não é rocket science. É engenharia de produto aplicada a modelos de ML.

Se você quer mapear os gaps de MLOps do seu ambiente antes do próximo drift silencioso, o próximo passo está abaixo.

**→ Faça o diagnóstico de AI Readiness em [roadmap.anuvia.com.br](https://roadmap.anuvia.com.br)**
