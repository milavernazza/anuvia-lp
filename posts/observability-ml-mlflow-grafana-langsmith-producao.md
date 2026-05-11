---
title: "Observability em ML: como sair do zero sem parar produção"
slug: observability-ml-mlflow-grafana-langsmith-producao
date: 2026-05-11
author: Mila Vernazza
audience: engineering
language: pt
subtitle: "MLflow, Grafana e LangSmith na prática — menos latência, menos custo"
excerpt: "4 modelos em produção, zero observability, drift indetectado. Veja como implementamos o stack certo e caímos 40% na latência."
tags:
  - MLOps
  - observability
  - MLflow
  - LangSmith
  - drift detection
  - produção
  - engineering
  - pt
---

Quando chegamos nesse time, o cenário era familiar: quatro modelos rodando em produção, todos entregando predições, todos potencialmente errados — e ninguém sabia.

Não havia alerta de drift. Não havia rastreamento de latência por endpoint. Não havia log estruturado de inferência. O time sabia que os modelos *existiam* em produção. Não sabia se ainda *funcionavam* como deveriam.

Esse é o estado de muitos times de dados em empresas médias: chegaram em produção, mas pararam no meio do caminho do MLOps.

## O problema real do "modelo em produção sem observability"

Colocar um modelo em produção sem observability é equivalente a subir uma API sem APM. Você só descobre o problema quando o cliente reclama — ou pior, quando ele para de reclamar porque já foi embora.

No caso desse time, os riscos concretos eram:

- **Data drift silencioso**: distribuição dos dados de entrada mudando sem trigger de retreinamento
- **Latência crescendo gradualmente**: sem baseline, ninguém percebia a degradação
- **Custo de inferência opaco**: nenhuma visibilidade sobre quais modelos consumiam mais recurso por request
- **Ausência de rastreabilidade**: sem run ID ligando predição → versão do modelo → features utilizadas

Nenhum desses problemas é exótico. São os problemas padrão de qualquer operação de ML que escala sem estrutura de monitoramento.

## O stack que implementamos

A decisão de stack levou em conta três restrições reais do time: familiaridade com Python, infraestrutura já parcialmente na AWS, e sem headcount para manter ferramenta proprietária complexa.

### MLflow para rastreabilidade de experimentos e versões

O primeiro passo foi instrumentar todos os modelos existentes com MLflow Tracking. Cada inferência em produção passou a registrar: versão do modelo, parâmetros de entrada (anonimizados), score de confiança e latência do request.

Isso criou a linha de base que o time nunca teve. Com histórico de runs, ficou possível correlacionar degradações com deploys específicos.

### Grafana para visibilidade operacional

Com as métricas saindo do MLflow e de um Prometheus simples, montamos dashboards no Grafana separados por modelo. Cada painel exibe:

- Latência p50 / p95 / p99
- Volume de requisições por hora
- Taxa de erro e fallback
- Custo estimado por inferência (calculado via tokens ou compute unit, dependendo do modelo)

A adoção foi rápida justamente porque o time já conhecia Grafana para infra — não precisou aprender nova ferramenta para ganhar visibilidade em ML.

### LangSmith para os modelos com componente LLM

Um dos quatro modelos em produção usava um LLM no pipeline. Para esse caso específico, o MLflow sozinho não era suficiente: faltava rastreabilidade de prompt, tokens consumidos por chamada e avaliação de qualidade de output.

O LangSmith resolveu exatamente esse gap. Cada chain passou a ter trace completo — input, output, tokens, latência por step e metadados de avaliação. Isso permitiu identificar dois prompts que estavam consumindo tokens desnecessários por falta de instrução de concisão.

## Os resultados

Após a instrumentação completa e primeiro ciclo de ajustes baseados nos dados coletados:

- **Latência média caiu 40%** — principalmente por identificar e remover chamadas redundantes no pipeline do modelo LLM
- **Custo por inferência baixou 25%** — via otimização de prompts e detecção de requests duplicados que chegavam sem cache

Mais importante que os números: o time passou a ter conversas diferentes. Em vez de "o modelo parece estar ok", a discussão virou "o p95 subiu 200ms na última semana, o que mudou nos dados de entrada?".

## O que isso exige do time

Observability em ML não é só ferramenta — é disciplina de instrumentação. Algumas práticas que consolidamos junto ao time:

- **Nunca sobe modelo sem run ID rastreável** — qualquer deploy precisa ter referência no MLflow
- **Alerta de drift é pré-requisito de go-live** — não é feature opcional
- **Dashboard de custo por modelo é revisado semanalmente** — custo de inferência cresce silenciosamente em produção

O esforço inicial de instrumentar os quatro modelos existentes foi de aproximadamente duas semanas de engineering. O retorno em visibilidade e redução de custo justificou em menos de 30 dias.

---

Se o seu time tem modelos em produção mas ainda não tem clareza sobre latência, drift e custo por inferência, o próximo passo é estruturar esse diagnóstico antes de escalar qualquer novo modelo.

**Faça o diagnóstico de AI Readiness do seu time:** [roadmap.anuvia.com.br](https://roadmap.anuvia.com.br)
