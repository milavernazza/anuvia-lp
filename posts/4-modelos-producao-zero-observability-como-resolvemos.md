---
title: "4 modelos em produção, zero observability: como resolvemos"
slug: 4-modelos-producao-zero-observability-como-resolvemos
date: 2026-05-11
author: Mila Vernazza
audience: engineering
language: pt
subtitle: "MLflow + Grafana + LangSmith numa stack de data team que operava no escuro"
excerpt: "Time com 4 modelos em produção só descobria problema quando cliente reclamava. Veja a stack que implementamos e os resultados em latência e custo."
tags:
  - MLOps
  - observability
  - MLflow
  - LangSmith
  - drift detection
  - AI em produção
  - engineering
  - pt
---

Quando a gente fala de "IA em produção", a maioria dos times pensa no deploy. Modelo treinado, endpoint no ar, feature liberada — pronto. Mas existe um estado que é talvez o mais perigoso de todos: **modelos rodando, gerando predições, e ninguém sabe se estão funcionando direito.**

Foi exatamente isso que encontramos num cliente recente.

## O cenário: 4 modelos vivos, nenhum monitorado

O time de dados tinha entregado quatro modelos em produção nos últimos 18 meses. Esforço real, trabalho sólido. O problema estava no que veio depois do deploy:

- **Drift detection**: inexistente. Nenhuma checagem de distribuição de features ao longo do tempo.
- **Alertas**: só disparavam quando o cliente — usuário final ou área de negócio — reclamava de alguma coisa estranha.
- **Rollback strategy**: não havia. Um modelo degradado ficava em produção até alguém perceber manualmente.
- **Eval suite contínuo**: sem baseline atualizado, sem comparação com versão anterior.
- **Custo por inferência**: desconhecido. O billing chegava agregado, sem granularidade por modelo ou por endpoint.

Isso não é descuido do time. É o padrão quando MLOps não foi tratado como parte do projeto desde o início — e quando a pressão por entregar novas features consome o espaço que deveria ir para instrumentação.

## O que implementamos

A stack escolhida levou em conta o que o time já usava parcialmente e o que podia ser integrado sem reescrever pipelines inteiros.

### MLflow para tracking e model registry

O time já tinha experimentos registrados no MLflow de forma inconsistente — alguns runs com métricas, outros sem. Padronizamos o logging de métricas de eval em todos os modelos, adicionamos tags de versão e criamos um model registry com stages explícitos (`Staging`, `Production`, `Archived`).

Isso sozinho já viabilizou rollback: com versões rastreadas e promovidas formalmente, reverter para uma versão anterior virou uma operação de dois cliques — não um exercício de arqueologia no repositório.

### Grafana para observability de runtime

Conectamos as métricas de inferência (latência p50/p95, volume de requests, taxa de erro) ao Grafana via Prometheus. Adicionamos dashboards por modelo, não só por serviço.

O resultado mais imediato: **latência caiu 40%** — não porque otimizamos o modelo, mas porque identificamos um gargalo de serialização que ninguém havia notado antes. Ele estava escondido nas médias agregadas.

### LangSmith para os modelos baseados em LLM

Dois dos quatro modelos usavam LLMs no pipeline. Para esses, LangSmith entrou como camada de tracing — permitindo ver input/output por chamada, identificar onde false-positives em classificações estavam ocorrendo e auditar o comportamento sem precisar reproduzir manualmente cada caso.

**False-positives em features críticas passaram de "suspeita difusa" para rastreável e quantificável.** Isso mudou a conversa com o time de produto: de "acho que o modelo está errando mais" para "nas últimas 72h, 14% das chamadas com esse padrão de input retornaram label incorreto."

### Custo por inferência

Com tags por modelo no billing da cloud e métricas de volume no Grafana, conseguimos finalmente atribuir custo a cada modelo. **Custo por inferência caiu 25%** depois que identificamos um modelo sendo chamado redundantemente em dois pontos do pipeline — duplicação que ninguém havia percebido porque não havia visibilidade.

## O ponto que times de dados subestimam

Observability não é feature extra. É o que separa um modelo em produção de um modelo **gerenciável** em produção.

Sem ela, o ciclo é sempre reativo: cliente reclama → engenheiro investiga → solução paliativa → próximo incidente. Com ela, o ciclo pode ser proativo: métrica sai do threshold → alerta dispara → investigação antes do impacto.

A stack não precisa ser complexa. MLflow + Grafana cobre a maioria dos casos de times até 10 pessoas. LangSmith entra quando LLMs fazem parte do pipeline. O importante é instrumentar **antes** de o problema aparecer — não depois.

---

Se o seu time tem modelos em produção sem monitoramento estruturado, o primeiro passo é entender onde estão os gaps maiores. Fazemos esse diagnóstico técnico com times de dados de empresas médias regularmente.

👉 Acesse **[roadmap.anuvia.com.br](https://roadmap.anuvia.com.br)** e veja como estruturamos o AI Readiness Roadmap para o seu contexto.
