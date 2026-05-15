"""ICP scoring rules — externalised config so Mila can tweak weights/thresholds
without editing scorer code.

Consumed by ``lib/prospecting.py`` (Agent A5). Keep this file pure data + tiny
helpers — no I/O, no network calls.

Practice keys mirror Track B handler keys (`cloud_finops`, `ai`, `devops`,
`growth`, `industry`). The Architecture doc uses both ``finops`` and
``cloud_finops`` interchangeably; we treat ``cloud_finops`` as canonical and
expose ``finops`` as an alias resolver below so callers can pass either.

Each practice block:

    weights              -> per-signal point contribution (sums clamped to 100)
    thresholds           -> per-signal trigger thresholds (size bands, titles,
                            verticals, stack keywords)
    claude_judge_threshold -> if rule-only score < this, prospecting.py may
                              call Claude for a nuance pass
"""

from __future__ import annotations

from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Canonical rule set
# ---------------------------------------------------------------------------


ICP_RULES: Dict[str, Dict[str, Any]] = {
    # ---------------------- Cloud FinOps -----------------------------------
    "cloud_finops": {
        "weights": {
            "has_aws": 25,
            "company_size_match": 20,
            "decision_maker_title": 20,
            "vertical_match": 15,
            "multi_account_signal": 10,
            "recent_funding": 10,
        },
        "thresholds": {
            "company_size_min": 20,
            "company_size_max": 500,
            "verticals_in": ["saas", "fintech", "ecommerce", "marketplace"],
            "titles_in": [
                "cto",
                "vp engineering",
                "vp eng",
                "head cloud",
                "head infra",
                "head of cloud",
                "head of infrastructure",
                "director engineering",
                "director of engineering",
                "engineering manager",
                "platform lead",
            ],
            "stack_keywords": ["aws", "ec2", "rds", "lambda", "eks", "fargate"],
            "multi_account_keywords": [
                "multi_account",
                "control_tower",
                "organizations",
                "landing_zone",
                "multiple_aws_accounts",
            ],
            "funding_stages_in": ["seed", "series_a", "series_b"],
        },
        "claude_judge_threshold": 50,
    },
    # ---------------------- AI Readiness -----------------------------------
    "ai": {
        "weights": {
            "has_ai_stack": 25,
            "hiring_ai_signal": 20,
            "company_size_match": 20,
            "scaling_ai_keywords": 15,
            "decision_maker_title": 10,
            "recent_funding": 10,
        },
        "thresholds": {
            "company_size_min": 50,
            "company_size_max": 1000,
            "verticals_in": [
                "saas",
                "fintech",
                "ecommerce",
                "marketplace",
                "media",
                "healthtech",
                "edtech",
            ],
            "titles_in": [
                "cto",
                "vp engineering",
                "vp ai",
                "head of ai",
                "head of ml",
                "head of data",
                "director ai",
                "director of data",
                "principal engineer",
                "ai lead",
                "ml lead",
            ],
            "stack_keywords": [
                "openai",
                "anthropic",
                "claude",
                "pytorch",
                "tensorflow",
                "langchain",
                "llamaindex",
                "vector",
                "pinecone",
                "weaviate",
                "huggingface",
                "vllm",
            ],
            "scaling_keywords": [
                "scaling ai",
                "ai in production",
                "pov",
                "proof of value",
                "llm in production",
                "agent",
                "rag",
                "eval",
                "guardrails",
            ],
            "hiring_keywords": [
                "hiring ai",
                "ai engineer",
                "ml engineer",
                "ai job",
                "ml job",
                "applied ai",
            ],
            "funding_stages_in": ["seed", "series_a", "series_b", "series_c"],
        },
        "claude_judge_threshold": 55,
    },
    # ---------------------- DevOps Maturity --------------------------------
    "devops": {
        "weights": {
            "has_devops_stack": 25,
            "company_size_match": 20,
            "decision_maker_title": 20,
            "dora_pain_signal": 15,
            "vertical_match": 10,
            "recent_funding": 10,
        },
        "thresholds": {
            "company_size_min": 30,
            "company_size_max": 800,
            "verticals_in": ["saas", "fintech", "ecommerce", "marketplace", "logistics"],
            "titles_in": [
                "cto",
                "vp engineering",
                "vp platform",
                "head of devops",
                "head of platform",
                "head of sre",
                "director engineering",
                "director of platform",
                "platform engineer",
                "sre lead",
                "devops lead",
            ],
            "stack_keywords": [
                "kubernetes",
                "k8s",
                "terraform",
                "argo",
                "argocd",
                "argo cd",
                "github actions",
                "circleci",
                "jenkins",
                "helm",
                "prometheus",
                "grafana",
                "istio",
            ],
            "dora_pain_keywords": [
                "lead time",
                "deploy frequency",
                "incident",
                "mttr",
                "outage",
                "rollback",
                "change failure",
                "release pain",
            ],
            "funding_stages_in": ["series_a", "series_b", "series_c"],
        },
        "claude_judge_threshold": 50,
    },
    # ---------------------- Growth / RevOps --------------------------------
    "growth": {
        "weights": {
            "has_revops_stack": 25,
            "company_size_match": 20,
            "decision_maker_title": 20,
            "vertical_match": 15,
            "growth_keywords": 10,
            "recent_funding": 10,
        },
        "thresholds": {
            "company_size_min": 10,
            "company_size_max": 300,
            "verticals_in": [
                "saas",
                "fintech",
                "ecommerce",
                "marketplace",
                "agency",
                "b2b services",
            ],
            "titles_in": [
                "ceo",
                "founder",
                "co-founder",
                "head of growth",
                "head of marketing",
                "head of sales",
                "head of revops",
                "vp marketing",
                "vp sales",
                "director growth",
                "director of marketing",
                "cmo",
            ],
            "stack_keywords": [
                "hubspot",
                "salesforce",
                "pipedrive",
                "rd station",
                "rdstation",
                "intercom",
                "segment",
                "mixpanel",
                "amplitude",
                "marketo",
            ],
            "growth_keywords": [
                "scaling sales",
                "go-to-market",
                "gtm",
                "pipeline",
                "revops",
                "lead funnel",
                "outbound",
            ],
            "funding_stages_in": ["pre_seed", "seed", "series_a"],
        },
        "claude_judge_threshold": 45,
    },
    # ---------------------- Industry Assessment ----------------------------
    "industry": {
        "weights": {
            "vertical_match": 25,
            "company_size_match": 20,
            "decision_maker_title": 20,
            "compliance_signal": 15,
            "modernisation_signal": 10,
            "recent_funding": 10,
        },
        "thresholds": {
            "company_size_min": 100,
            "company_size_max": 5000,
            "verticals_in": [
                "financial services",
                "banking",
                "insurance",
                "healthcare",
                "manufacturing",
                "retail",
                "logistics",
                "energy",
                "telco",
                "public sector",
            ],
            "titles_in": [
                "cio",
                "cto",
                "vp technology",
                "head of digital",
                "head of transformation",
                "director technology",
                "director of digital",
                "head of compliance",
            ],
            "stack_keywords": [
                "sap",
                "oracle",
                "mainframe",
                "as400",
                "ibm",
                "cobol",
                "totvs",
                "sap hana",
                "siebel",
            ],
            "compliance_keywords": [
                "lgpd",
                "gdpr",
                "sox",
                "pci",
                "hipaa",
                "compliance",
                "audit",
                "iso 27001",
                "soc 2",
            ],
            "modernisation_keywords": [
                "legacy",
                "migration",
                "cloud migration",
                "modernisation",
                "modernization",
                "monolith to microservices",
                "digital transformation",
            ],
            "funding_stages_in": [],
        },
        "claude_judge_threshold": 45,
    },
}


# Aliases — accept legacy / shorthand keys.
_PRACTICE_ALIASES: Dict[str, str] = {
    "finops": "cloud_finops",
    "cloud-finops": "cloud_finops",
    "cloudfinops": "cloud_finops",
    "ai_readiness": "ai",
    "ai-readiness": "ai",
    "devops_maturity": "devops",
    "devops-maturity": "devops",
    "growth_salesops": "growth",
    "salesops": "growth",
    "industry_assessment": "industry",
}


# All practice keys we ever score against.
ALL_PRACTICES: List[str] = list(ICP_RULES.keys())


def canonical_practice(practice: str) -> str:
    """Resolve aliases. Returns the canonical key, or the input lowered."""
    key = (practice or "").strip().lower().replace(" ", "_")
    return _PRACTICE_ALIASES.get(key, key)


def get_rules(practice: str) -> Dict[str, Any]:
    """Return the rules block for `practice`. Raises KeyError if unknown."""
    key = canonical_practice(practice)
    if key not in ICP_RULES:
        raise KeyError(f"unknown practice: {practice!r} (resolved to {key!r})")
    return ICP_RULES[key]


__all__ = [
    "ICP_RULES",
    "ALL_PRACTICES",
    "canonical_practice",
    "get_rules",
]
