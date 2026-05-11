---
title: "MLOps sem infraestrutura gigante: 5 modelos, resultados reais"
slug: mlops-5-modelos-producao-mlflow-grafana-langsmith
date: 2026-05-11
author: Mila Vernazza
audience: engineering
language: pt
subtitle: "Como um time enxuto foi de zero observability a drift detection automático com MLflow, Grafana e LangSmith"
excerpt: "5 modelos em produção, zero MLOps — até a latência cair 35% e o custo por inferência baixar 20%. Veja como montamos o pipeline."
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

## O problema que ninguém documenta direito

O time tinha cinco modelos rodando em produção. Nenhum dashboard. Nenhum alerta de drift. Nenhum rastreamento de latência por endpoint. Os modelos "funcionavam" — até que deixavam de funcionar, e ninguém sabia exatamente por quê ou quando o problema tinha começado.

Esse cenário é mais comum do que parece. Equipes de dados que cresceram rápido costumam priorizar a entrega do modelo e adiar a camada de operações. O débito técnico acumula silenciosamente até virar incêndio.

Quando chegamos, a pergunta não era *se* eles precisavam de MLOps — era como montar uma estrutura funcional sem travar o time por meses de reengenharia.

---

## Stack escolhida: pragmatismo antes de pureza

Antes de qualquer ferramenta, definimos três requisitos não-negociáveis:

- **Visibilidade em tempo real** de latência, throughput e erros por modelo
- **Detecção automática de drift** para dados de entrada e saída
- **Rastreamento de prompts e respostas** nos modelos com componente LLM

Com isso em mãos, o stack ficou assim:

### MLflow para tracking e registro de modelos

O MLflow já estava parcialmente adotado pelo time, mas sendo usado só para logar experimentos offline. Expandimos o uso para **model registry com stages** (`Staging → Production → Archived`), versionamento de artefatos e integração com o pipeline de deploy.

O ganho imediato: qualquer rollback passou a levar minutos, não horas de arqueologia em repositório.

### Grafana para observability de infraestrutura e negócio

Montamos dashboards separados por camada:

- **Infra**: CPU/GPU por pod, tempo de resposta do endpoint, taxa de erro HTTP
- **Modelo**: distribuição de scores, volume de predições por janela de tempo
- **Negócio**: métricas downstream atreladas às predições (conversão, churn score vs. churn real)

Alertas configurados com thresholds conservadores no início — melhor ter falso positivo do que ignorar degradação real.

### LangSmith para os modelos com LLM

Dois dos cinco modelos tinham componentes de linguagem. O LangSmith entrou para **rastrear cada chain de prompt**, registrar inputs/outputs e medir latência por etapa da chain. Sem isso, depurar comportamento inesperado num fluxo RAG é essencialmente adivinhar.

---

## Drift detection: o que configuramos e por quê

Para drift em dados tabulares, usamos **Population Stability Index (PSI)** rodando em job diário. Simples, interpretável, fácil de explicar para o time de produto quando dispara.

Para os modelos de linguagem, o sinal de drift é diferente — monitoramos **distribuição de embeddings de entrada** via cosseno médio contra uma janela de referência. Quando a distância sobe além do threshold, o alerta vai para o canal do time antes de qualquer degradação chegar ao usuário final.

---

## Resultados após a implementação

Os números vieram em combinação — não existe uma única alavanca:

- **Latência média caiu 35%**: identificamos dois modelos com inferência síncrona desnecessária que foram migrados para processamento assíncrono após o Grafana expor o gargalo com clareza
- **Custo por inferência caiu 20%**: o rastreamento do LangSmith revelou chamadas redundantes à API do modelo — o mesmo contexto sendo re-enviado a cada turno sem cache

Sem observability, esses dois problemas poderiam passar meses invisíveis.

---

## O que esse projeto ensina

MLOps não precisa começar com Kubernetes customizado, feature store proprietária e plataforma interna de ML. Para a maioria dos times de dados em empresas médias, o maior salto de maturidade vem de **tornar o que já está em produção visível e controlável**.

O stack MLflow + Grafana + LangSmith não é o único caminho. É um caminho que funciona, custa pouco para começar e escala bem até o time precisar de algo mais sofisticado.

Se você tem modelos em produção sem instrumentação equivalente, o risco não está no futuro — está acontecendo agora, só que silenciosamente.

---

Quer mapear os gaps de MLOps do seu time antes do próximo incidente? Acesse **[roadmap.anuvia.com.br](https://roadmap.anuvia.com.br)** e faça o diagnóstico técnico gratuito.
