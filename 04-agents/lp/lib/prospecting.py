"""Prospecting — active lead-finding + enrichment + ICP scoring.

Owned by Agent A5 in the v2 sprint. Per
ARCHITECTURE_AUTONOMOUS_v2_FULL.md §"Module layout" + §"Outbound contract"
(prospects table schema lives there).

Responsibilities:
  * Pull prospects from Apollo.io (search + person enrichment).
  * Enrich via BuiltWith (tech stack) + Apollo (LinkedIn / company size /
    vertical / funding).
  * Score each prospect against per-practice ICP rules (`lib/icp_rules.py`).
  * Upsert into Supabase `prospects` table (idempotent on email).
  * Feed eligible prospects into A1's outbound pipeline
    (`lib.outbound.send_outbound_sequence`).

Module boundaries — we DO NOT modify any of:
  * lib/outbound.py        (A1, live)
  * lib/track_b.py         (A2, live)
  * lib/contract.py        (A3, live)
  * lib/reply_classify.py  (A4, live)
  * lib/sessions.py        (shared)
  * lib/orchestrator.py    (shared)
  * app.py                 (composition root)

Graceful degradation: every external API is optional. Missing keys log a
warning and return safe defaults — Mila can fall back to CSV imports
through `scripts/outbound_run.py` and still drive the funnel.

Quality bar mirrors lib/outbound.py: all public functions are async, httpx
client per call, every Supabase query URL-encodes user inputs (we've shipped
that bug twice already), rate-limited Apollo calls (5 req/sec).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as _urlquote

import httpx
from fastapi import APIRouter, HTTPException, Request

from lib.icp_rules import (
    ALL_PRACTICES,
    ICP_RULES,
    canonical_practice,
    get_rules,
)
from lib.sessions import SUPA_HEADERS, SUPA_URL

log = logging.getLogger("anuvia-lp.prospecting")


# ---------------------------------------------------------------------------
# Environment / constants
# ---------------------------------------------------------------------------

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")
APOLLO_API_URL = os.environ.get(
    "APOLLO_API_URL", "https://api.apollo.io/v1"
).rstrip("/")
APOLLO_RATE_LIMIT_RPS = float(os.environ.get("APOLLO_RATE_LIMIT_RPS", "5"))

BUILTWITH_API_KEY = os.environ.get("BUILTWITH_API_KEY", "")
BUILTWITH_API_URL = os.environ.get(
    "BUILTWITH_API_URL", "https://api.builtwith.com/v21/api.json"
)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
ANTHROPIC_API_URL = os.environ.get(
    "ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages"
)

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

_HTTP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Rate limiter for Apollo (5 req/sec by default).
# ---------------------------------------------------------------------------

_APOLLO_LOCK: Optional[asyncio.Lock] = None
_APOLLO_LAST_TS: Dict[str, float] = {"ts": 0.0}


def _apollo_lock() -> asyncio.Lock:
    """Lazily build the Apollo rate-limit lock (event-loop-safe import)."""
    global _APOLLO_LOCK
    if _APOLLO_LOCK is None:
        _APOLLO_LOCK = asyncio.Lock()
    return _APOLLO_LOCK


async def _apollo_rate_gate() -> None:
    """Block until at least 1/RPS seconds elapsed since last Apollo call."""
    if APOLLO_RATE_LIMIT_RPS <= 0:
        return
    min_gap = 1.0 / APOLLO_RATE_LIMIT_RPS
    async with _apollo_lock():
        elapsed = time.monotonic() - _APOLLO_LAST_TS["ts"]
        wait_for = min_gap - elapsed
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _APOLLO_LAST_TS["ts"] = time.monotonic()


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _safe_lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _coerce_enriched(value: Any) -> Dict[str, Any]:
    """Normalise a possibly-stringified jsonb field to a dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _company_size_to_int(value: Any) -> Optional[int]:
    """Try to turn '20-50' / '50-100' / '500' / 200 into a representative int.

    For bands we return the midpoint (rounded to int).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    m = re.match(r"(\d+)\s*-\s*(\d+)", s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo + hi) // 2
    m = re.match(r"(\d+)\+", s)
    if m:
        return int(m.group(1))
    m = re.match(r"(\d+)", s)
    if m:
        return int(m.group(1))
    return None


def _company_size_band(size: Optional[int]) -> Optional[str]:
    """Bucket a headcount into a coarse band string."""
    if size is None:
        return None
    if size < 10:
        return "1-10"
    if size < 20:
        return "10-20"
    if size < 50:
        return "20-50"
    if size < 100:
        return "50-100"
    if size < 200:
        return "100-200"
    if size < 500:
        return "200-500"
    if size < 1000:
        return "500-1000"
    if size < 5000:
        return "1000-5000"
    return "5000+"


def _estimate_aws_spend_band(tech_stack: List[str], company_size: Optional[int]) -> Optional[str]:
    """Very rough heuristic — kicks the harder estimation downstream.

    Used by the FinOps practice as a coarse pre-qualifier. Logic:
      * No AWS in stack  -> None
      * AWS + size < 50  -> 'low'
      * AWS + 50..500    -> 'mid'
      * AWS + >= 500     -> 'high'
    """
    has_aws = any("aws" in t or t in ("ec2", "rds", "lambda", "eks", "fargate") for t in tech_stack)
    if not has_aws:
        return None
    if company_size is None:
        return "mid"
    if company_size < 50:
        return "low"
    if company_size < 500:
        return "mid"
    return "high"


# ---------------------------------------------------------------------------
# Apollo: search + enrichment
# ---------------------------------------------------------------------------


def _normalise_apollo_person(p: Dict[str, Any]) -> Dict[str, Any]:
    """Map an Apollo `person` (or `contact`) shape to our prospect schema.

    Apollo's `people/search` returns objects with both `person` and
    `organization` nested keys; we accept either flat or nested input.
    """
    org = p.get("organization") or p.get("account") or {}
    if not isinstance(org, dict):
        org = {}

    email = p.get("email") or p.get("primary_email") or ""
    first_name = p.get("first_name") or ""
    last_name = p.get("last_name") or ""
    if not (first_name or last_name) and p.get("name"):
        parts = str(p.get("name")).strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    title = p.get("title") or p.get("headline") or ""
    company = org.get("name") or p.get("organization_name") or p.get("company") or ""
    linkedin_url = p.get("linkedin_url") or p.get("linkedin") or ""

    # Country can live in person.country, person.location, org.country, etc.
    country = (
        p.get("country")
        or (p.get("location") or {}).get("country") if isinstance(p.get("location"), dict) else None
    ) or org.get("country") or ""

    company_size_raw = (
        org.get("estimated_num_employees")
        or org.get("num_employees")
        or org.get("employee_count")
        or p.get("company_size")
    )
    company_size_int = _company_size_to_int(company_size_raw)
    company_size_band = _company_size_band(company_size_int)

    vertical = (
        org.get("industry")
        or org.get("primary_industry")
        or p.get("industry")
        or ""
    )

    funding_stage = (
        org.get("latest_funding_stage")
        or org.get("funding_stage")
        or ""
    )

    website = org.get("website_url") or org.get("website") or ""
    tech_stack = org.get("technologies") or org.get("technology_names") or []
    if isinstance(tech_stack, list):
        tech_stack = [str(t).lower() for t in tech_stack if t]
    else:
        tech_stack = []

    return {
        "email": str(email).strip().lower() if email else "",
        "first_name": str(first_name).strip() or None,
        "last_name": str(last_name).strip() or None,
        "title": str(title).strip() or None,
        "company": str(company).strip() or None,
        "company_size_band": company_size_band,
        "vertical": _safe_lower(vertical) or None,
        "country": str(country).strip() or None,
        "linkedin_url": str(linkedin_url).strip() or None,
        "source": "apollo",
        "enriched_data": {
            "company_size_int": company_size_int,
            "company_website": website or None,
            "funding_stage": _safe_lower(funding_stage) or None,
            "tech_stack": tech_stack,
            "apollo_id": p.get("id") or p.get("person_id"),
        },
    }


async def search_prospects_apollo(query: Dict[str, Any], limit: int = 50) -> List[Dict[str, Any]]:
    """Search prospects via Apollo's `mixed_people/search` endpoint.

    Query fields (all optional):
      titles                list[str]
      industries            list[str]
      company_size_min      int
      company_size_max      int
      country               str ("BR", "US", ...)
      keywords              str  (free-text query)

    Returns up to ``limit`` rows normalised to our prospect schema. If
    ``APOLLO_API_KEY`` is unset we log a warning and return ``[]`` so the
    rest of the pipeline degrades to manual CSV import.
    """
    if not APOLLO_API_KEY:
        log.warning(
            "prospecting.search_prospects_apollo: APOLLO_API_KEY missing — "
            "returning [] (use scripts/outbound_run.py for CSV import)"
        )
        return []

    titles = query.get("titles") or []
    industries = query.get("industries") or []
    keywords = query.get("keywords") or ""
    country = query.get("country") or ""
    size_min = query.get("company_size_min")
    size_max = query.get("company_size_max")

    page = 1
    per_page = max(1, min(int(limit), 100))
    body: Dict[str, Any] = {
        "page": page,
        "per_page": per_page,
    }
    if titles:
        body["person_titles"] = list(titles)
    if industries:
        body["organization_industries"] = list(industries)
    if country:
        body["person_locations"] = [country]
    if size_min is not None and size_max is not None:
        body["organization_num_employees_ranges"] = [f"{int(size_min)},{int(size_max)}"]
    if keywords:
        body["q_keywords"] = str(keywords)

    results: List[Dict[str, Any]] = []
    fetched = 0
    while fetched < limit:
        await _apollo_rate_gate()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                r = await client.post(
                    f"{APOLLO_API_URL}/mixed_people/search",
                    headers={
                        "Cache-Control": "no-cache",
                        "Content-Type": "application/json",
                        "X-Api-Key": APOLLO_API_KEY,
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            log.warning("prospecting.apollo network error: %s", exc)
            break

        if r.status_code >= 400:
            log.warning(
                "prospecting.apollo non-200 status=%s body=%s",
                r.status_code, r.text[:300],
            )
            break

        payload = r.json() if r.text else {}
        people = payload.get("people") or payload.get("contacts") or []
        if not people:
            break
        for p in people:
            if not isinstance(p, dict):
                continue
            results.append(_normalise_apollo_person(p))
            fetched += 1
            if fetched >= limit:
                break

        # Pagination — Apollo returns `pagination.total_pages` when present.
        pagination = payload.get("pagination") or {}
        total_pages = int(pagination.get("total_pages") or 1)
        if page >= total_pages or fetched >= limit:
            break
        page += 1
        body["page"] = page

    log.info("prospecting.search_prospects_apollo: fetched %d rows", len(results))
    return results


async def _apollo_enrich_person(linkedin_url: str) -> Dict[str, Any]:
    """Hit Apollo's `people/match` to enrich one prospect by LinkedIn URL."""
    if not APOLLO_API_KEY or not linkedin_url:
        return {}
    await _apollo_rate_gate()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                f"{APOLLO_API_URL}/people/match",
                headers={
                    "Cache-Control": "no-cache",
                    "Content-Type": "application/json",
                    "X-Api-Key": APOLLO_API_KEY,
                },
                json={"linkedin_url": linkedin_url},
            )
    except httpx.HTTPError as exc:
        log.warning("prospecting.apollo_enrich network error: %s", exc)
        return {}
    if r.status_code >= 400:
        log.warning(
            "prospecting.apollo_enrich non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return {}
    body = r.json() if r.text else {}
    person = body.get("person") or {}
    if not isinstance(person, dict):
        return {}
    return person


async def _builtwith_lookup(domain: str) -> List[str]:
    """Return a lower-cased list of detected technologies for `domain`."""
    if not BUILTWITH_API_KEY or not domain:
        return []
    # Strip scheme + paths so we just send the bare hostname.
    cleaned = re.sub(r"^https?://", "", domain).split("/", 1)[0].strip().lower()
    if not cleaned:
        return []
    params = {"KEY": BUILTWITH_API_KEY, "LOOKUP": cleaned}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(BUILTWITH_API_URL, params=params)
    except httpx.HTTPError as exc:
        log.warning("prospecting.builtwith network error: %s", exc)
        return []
    if r.status_code >= 400:
        log.warning(
            "prospecting.builtwith non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return []
    payload = r.json() if r.text else {}
    techs: List[str] = []
    # BuiltWith v21 layout: Results[0].Result.Paths[*].Technologies[*].Name
    for result in (payload.get("Results") or []):
        res = (result or {}).get("Result") or {}
        for path in (res.get("Paths") or []):
            for tech in (path.get("Technologies") or []):
                name = tech.get("Name")
                if name:
                    techs.append(str(name).lower())
    # de-dupe, preserve order
    seen: set = set()
    out: List[str] = []
    for t in techs:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def enrich_prospect(prospect: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a prospect via Apollo + BuiltWith. Idempotent + degradation-safe.

    Inbound prospect may be sparse (just email + linkedin_url). Returns the
    same dict shape with ``enriched_data`` populated::

        {
          "tech_stack": [...],
          "company_size_band": "20-50",
          "vertical": "saas",
          "country": "BR",
          "linkedin": {...},
          "funding_stage": "seed" | "series_a" | ...,
          "estimated_aws_spend_band": "low" | "mid" | "high",
        }

    Missing API keys log warnings and leave fields blank rather than erroring.
    """
    out = dict(prospect)
    enriched = _coerce_enriched(out.get("enriched_data"))

    linkedin_url = out.get("linkedin_url") or enriched.get("linkedin_url") or ""

    # ------- Apollo person enrichment -------------------------------------
    if linkedin_url and APOLLO_API_KEY:
        try:
            person = await _apollo_enrich_person(linkedin_url)
            if person:
                normalised = _normalise_apollo_person(person)
                # Only fill blanks — don't overwrite operator-curated values.
                for key in ("first_name", "last_name", "title", "company",
                            "company_size_band", "vertical", "country"):
                    if not out.get(key) and normalised.get(key):
                        out[key] = normalised[key]
                merged = _coerce_enriched(normalised.get("enriched_data"))
                for k, v in merged.items():
                    enriched.setdefault(k, v)
                enriched["linkedin"] = {
                    "url": linkedin_url,
                    "headline": person.get("headline") or person.get("title"),
                    "summary": person.get("about") or person.get("summary"),
                }
        except Exception:  # noqa: BLE001
            log.exception("prospecting.enrich_prospect: apollo enrichment failed")
    elif linkedin_url:
        log.info(
            "prospecting.enrich_prospect: APOLLO_API_KEY missing — "
            "skipping LinkedIn enrichment for %s", linkedin_url,
        )

    # ------- BuiltWith tech stack -----------------------------------------
    website = enriched.get("company_website") or out.get("company_website") or ""
    # Heuristic if no website: try `<company>.com`.
    if not website and out.get("company"):
        slug = re.sub(r"[^a-z0-9]", "", _safe_lower(out.get("company")))
        if slug:
            website = f"https://{slug}.com"
            enriched["company_website_guess"] = website

    bw_techs: List[str] = []
    if website:
        try:
            bw_techs = await _builtwith_lookup(website)
        except Exception:  # noqa: BLE001
            log.exception("prospecting.enrich_prospect: builtwith failed")

    tech_stack = list(enriched.get("tech_stack") or [])
    for t in bw_techs:
        if t not in tech_stack:
            tech_stack.append(t)
    enriched["tech_stack"] = tech_stack

    # ------- Derive size band / vertical / country when missing -----------
    if not out.get("company_size_band"):
        size_int = enriched.get("company_size_int")
        band = _company_size_band(_company_size_to_int(size_int))
        if band:
            out["company_size_band"] = band
            enriched["company_size_band"] = band

    if not out.get("country"):
        out["country"] = enriched.get("country") or None

    if not out.get("vertical"):
        out["vertical"] = enriched.get("vertical") or None

    # ------- Derived: AWS spend band --------------------------------------
    size_int = enriched.get("company_size_int") or _company_size_to_int(
        out.get("company_size_band")
    )
    aws_band = _estimate_aws_spend_band(tech_stack, size_int)
    if aws_band:
        enriched["estimated_aws_spend_band"] = aws_band

    enriched.setdefault("funding_stage", enriched.get("funding_stage"))

    # Mirror common fields up so consumers (outbound.py, track_b.py) can read
    # them directly off enriched_data.
    enriched["company_size_band"] = out.get("company_size_band")
    enriched["vertical"] = out.get("vertical")
    enriched["country"] = out.get("country")

    out["enriched_data"] = enriched
    return out


# ---------------------------------------------------------------------------
# ICP scoring
# ---------------------------------------------------------------------------


def _title_matches(title: str, titles_in: List[str]) -> bool:
    t = _safe_lower(title)
    if not t:
        return False
    for cand in titles_in:
        cand_l = _safe_lower(cand)
        if cand_l and cand_l in t:
            return True
    return False


def _stack_hit(tech_stack: List[str], keywords: List[str]) -> bool:
    if not tech_stack or not keywords:
        return False
    stack_lower = [_safe_lower(t) for t in tech_stack]
    kw_lower = [_safe_lower(k) for k in keywords]
    return any(any(kw in t for t in stack_lower) for kw in kw_lower)


def _text_hit(corpus: str, keywords: List[str]) -> bool:
    if not corpus or not keywords:
        return False
    c = _safe_lower(corpus)
    return any(_safe_lower(k) in c for k in keywords)


def _size_in_range(size: Optional[int], rules: Dict[str, Any]) -> bool:
    if size is None:
        return False
    lo = rules.get("company_size_min")
    hi = rules.get("company_size_max")
    if lo is not None and size < int(lo):
        return False
    if hi is not None and size > int(hi):
        return False
    return True


def _vertical_match(vertical: Optional[str], verticals_in: List[str]) -> bool:
    if not vertical:
        return False
    v = _safe_lower(vertical)
    return any(_safe_lower(x) in v for x in verticals_in)


def _score_cloud_finops(prospect: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[int, Dict[str, bool]]:
    weights = rules["weights"]
    th = rules["thresholds"]
    enriched = _coerce_enriched(prospect.get("enriched_data"))
    stack = list(enriched.get("tech_stack") or [])
    title = prospect.get("title") or ""
    vertical = prospect.get("vertical") or enriched.get("vertical")
    size_int = enriched.get("company_size_int") or _company_size_to_int(
        prospect.get("company_size_band")
    )
    funding = _safe_lower(enriched.get("funding_stage"))

    signals = {
        "has_aws": _stack_hit(stack, th.get("stack_keywords", [])),
        "company_size_match": _size_in_range(size_int, th),
        "decision_maker_title": _title_matches(title, th.get("titles_in", [])),
        "vertical_match": _vertical_match(vertical, th.get("verticals_in", [])),
        "multi_account_signal": _stack_hit(stack, th.get("multi_account_keywords", []))
            or _text_hit(
                json.dumps(enriched, ensure_ascii=False),
                th.get("multi_account_keywords", []),
            ),
        "recent_funding": funding in set(_safe_lower(x) for x in th.get("funding_stages_in", [])),
    }
    score = sum(weights.get(k, 0) for k, hit in signals.items() if hit)
    return score, signals


def _score_ai(prospect: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[int, Dict[str, bool]]:
    weights = rules["weights"]
    th = rules["thresholds"]
    enriched = _coerce_enriched(prospect.get("enriched_data"))
    stack = list(enriched.get("tech_stack") or [])
    title = prospect.get("title") or ""
    desc_corpus = " ".join([
        str(title),
        str(prospect.get("company") or ""),
        str(enriched.get("linkedin", {}).get("summary") or "")
            if isinstance(enriched.get("linkedin"), dict) else "",
        str(enriched.get("hiring_signals") or ""),
    ])
    size_int = enriched.get("company_size_int") or _company_size_to_int(
        prospect.get("company_size_band")
    )
    funding = _safe_lower(enriched.get("funding_stage"))

    signals = {
        "has_ai_stack": _stack_hit(stack, th.get("stack_keywords", [])),
        "hiring_ai_signal": _text_hit(desc_corpus, th.get("hiring_keywords", []))
            or bool(enriched.get("hiring_ai_signal")),
        "company_size_match": _size_in_range(size_int, th),
        "scaling_ai_keywords": _text_hit(desc_corpus, th.get("scaling_keywords", [])),
        "decision_maker_title": _title_matches(title, th.get("titles_in", [])),
        "recent_funding": funding in set(_safe_lower(x) for x in th.get("funding_stages_in", [])),
    }
    score = sum(weights.get(k, 0) for k, hit in signals.items() if hit)
    return score, signals


def _score_devops(prospect: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[int, Dict[str, bool]]:
    weights = rules["weights"]
    th = rules["thresholds"]
    enriched = _coerce_enriched(prospect.get("enriched_data"))
    stack = list(enriched.get("tech_stack") or [])
    title = prospect.get("title") or ""
    vertical = prospect.get("vertical") or enriched.get("vertical")
    size_int = enriched.get("company_size_int") or _company_size_to_int(
        prospect.get("company_size_band")
    )
    funding = _safe_lower(enriched.get("funding_stage"))
    pain_corpus = " ".join([
        str(title),
        str(enriched.get("linkedin", {}).get("summary") or "")
            if isinstance(enriched.get("linkedin"), dict) else "",
        str(enriched.get("dora_signals") or ""),
        str(enriched.get("pain_observations") or ""),
    ])

    signals = {
        "has_devops_stack": _stack_hit(stack, th.get("stack_keywords", [])),
        "company_size_match": _size_in_range(size_int, th),
        "decision_maker_title": _title_matches(title, th.get("titles_in", [])),
        "dora_pain_signal": _text_hit(pain_corpus, th.get("dora_pain_keywords", [])),
        "vertical_match": _vertical_match(vertical, th.get("verticals_in", [])),
        "recent_funding": funding in set(_safe_lower(x) for x in th.get("funding_stages_in", [])),
    }
    score = sum(weights.get(k, 0) for k, hit in signals.items() if hit)
    return score, signals


def _score_growth(prospect: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[int, Dict[str, bool]]:
    weights = rules["weights"]
    th = rules["thresholds"]
    enriched = _coerce_enriched(prospect.get("enriched_data"))
    stack = list(enriched.get("tech_stack") or [])
    title = prospect.get("title") or ""
    vertical = prospect.get("vertical") or enriched.get("vertical")
    size_int = enriched.get("company_size_int") or _company_size_to_int(
        prospect.get("company_size_band")
    )
    funding = _safe_lower(enriched.get("funding_stage"))
    corpus = " ".join([
        str(title),
        str(enriched.get("linkedin", {}).get("summary") or "")
            if isinstance(enriched.get("linkedin"), dict) else "",
    ])

    signals = {
        "has_revops_stack": _stack_hit(stack, th.get("stack_keywords", [])),
        "company_size_match": _size_in_range(size_int, th),
        "decision_maker_title": _title_matches(title, th.get("titles_in", [])),
        "vertical_match": _vertical_match(vertical, th.get("verticals_in", [])),
        "growth_keywords": _text_hit(corpus, th.get("growth_keywords", [])),
        "recent_funding": funding in set(_safe_lower(x) for x in th.get("funding_stages_in", [])),
    }
    score = sum(weights.get(k, 0) for k, hit in signals.items() if hit)
    return score, signals


def _score_industry(prospect: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[int, Dict[str, bool]]:
    weights = rules["weights"]
    th = rules["thresholds"]
    enriched = _coerce_enriched(prospect.get("enriched_data"))
    stack = list(enriched.get("tech_stack") or [])
    title = prospect.get("title") or ""
    vertical = prospect.get("vertical") or enriched.get("vertical")
    size_int = enriched.get("company_size_int") or _company_size_to_int(
        prospect.get("company_size_band")
    )
    funding = _safe_lower(enriched.get("funding_stage"))
    corpus = " ".join([
        str(title),
        str(enriched.get("linkedin", {}).get("summary") or "")
            if isinstance(enriched.get("linkedin"), dict) else "",
        str(enriched.get("compliance_signals") or ""),
        str(enriched.get("modernisation_signals") or ""),
    ])

    funding_set = set(_safe_lower(x) for x in th.get("funding_stages_in", []))
    signals = {
        "vertical_match": _vertical_match(vertical, th.get("verticals_in", [])),
        "company_size_match": _size_in_range(size_int, th),
        "decision_maker_title": _title_matches(title, th.get("titles_in", [])),
        "compliance_signal": _text_hit(corpus, th.get("compliance_keywords", []))
            or _stack_hit(stack, th.get("stack_keywords", [])),
        "modernisation_signal": _text_hit(corpus, th.get("modernisation_keywords", [])),
        "recent_funding": bool(funding_set) and funding in funding_set,
    }
    score = sum(weights.get(k, 0) for k, hit in signals.items() if hit)
    return score, signals


_PRACTICE_SCORERS = {
    "cloud_finops": _score_cloud_finops,
    "ai": _score_ai,
    "devops": _score_devops,
    "growth": _score_growth,
    "industry": _score_industry,
}


def _clamp_score(s: int) -> int:
    return max(0, min(100, int(s)))


async def _claude_judge_score(prospect: Dict[str, Any], practice: str) -> Optional[int]:
    """Ask Claude to grade the prospect 0-100. Returns None on any failure.

    Used as a nuance pass when rule-only score is below the
    ``claude_judge_threshold``. We never block on Claude — caller falls back
    to rule-only score when this returns None.
    """
    if not ANTHROPIC_API_KEY:
        return None
    enriched = _coerce_enriched(prospect.get("enriched_data"))
    summary = {
        "title": prospect.get("title"),
        "company": prospect.get("company"),
        "vertical": prospect.get("vertical"),
        "country": prospect.get("country"),
        "company_size_band": prospect.get("company_size_band"),
        "enriched": {
            "tech_stack": enriched.get("tech_stack") or [],
            "funding_stage": enriched.get("funding_stage"),
            "linkedin_headline": (enriched.get("linkedin") or {}).get("headline")
                if isinstance(enriched.get("linkedin"), dict) else None,
            "estimated_aws_spend_band": enriched.get("estimated_aws_spend_band"),
        },
    }
    system = (
        "You are an ICP fit grader for Anuvia (Brazilian boutique consultancy). "
        "Grade the prospect 0-100 for the given practice. 0 = no fit, "
        "100 = perfect fit. Reply with ONLY an integer."
    )
    prompt = (
        f"Practice: {practice}\n"
        f"Prospect summary:\n{json.dumps(summary, ensure_ascii=False)}\n\n"
        f"Score (0-100, integer only):"
    )
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 16,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
    except httpx.HTTPError as exc:
        log.warning("prospecting.claude_judge network error: %s", exc)
        return None
    if r.status_code >= 400:
        log.warning(
            "prospecting.claude_judge non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return None
    body = r.json() if r.text else {}
    blocks = body.get("content") or []
    text = ""
    for blk in blocks:
        if isinstance(blk, dict) and blk.get("type") == "text":
            text += blk.get("text") or ""
    m = re.search(r"-?\d+", text)
    if not m:
        return None
    try:
        return _clamp_score(int(m.group(0)))
    except ValueError:
        return None


async def score_icp(prospect: Dict[str, Any], practice: str) -> int:
    """Return a 0-100 ICP score for ``prospect`` against ``practice``.

    Practice keys: cloud_finops, ai, devops, growth, industry. Aliases
    (``finops`` -> ``cloud_finops``) are resolved via ``canonical_practice``.

    Logic: run the rule scorer, clamp, then if the result is below the
    practice's ``claude_judge_threshold`` ask Claude for a second opinion
    and average. Claude failures are non-blocking — we return the rules-only
    score in that case.
    """
    pkey = canonical_practice(practice)
    rules = ICP_RULES.get(pkey)
    if not rules:
        log.warning("prospecting.score_icp: unknown practice %r", practice)
        return 0
    scorer = _PRACTICE_SCORERS.get(pkey)
    if scorer is None:
        log.warning("prospecting.score_icp: no scorer registered for %r", pkey)
        return 0

    raw, signals = scorer(prospect, rules)
    base = _clamp_score(raw)

    threshold = int(rules.get("claude_judge_threshold", 0))
    if base < threshold and base > 0:
        # Borderline — let Claude nuance, but only if some signals fired.
        signal_count = sum(1 for hit in signals.values() if hit)
        if signal_count >= 1:
            judged = await _claude_judge_score(prospect, pkey)
            if judged is not None:
                base = _clamp_score(int((base + judged) / 2))

    return base


async def auto_classify_practice_fit(prospect: Dict[str, Any]) -> Tuple[str, int]:
    """Score the prospect across all 5 practices and return the best fit.

    Returns ``(best_practice, score)``. Ties are broken by ``ALL_PRACTICES``
    order (so cloud_finops wins ties against ai etc — that matches Mila's
    revenue priorities). If nothing scores > 0 we still return the highest.
    """
    best_practice = ALL_PRACTICES[0]
    best_score = -1
    for pkey in ALL_PRACTICES:
        try:
            s = await score_icp(prospect, pkey)
        except Exception:  # noqa: BLE001
            log.exception(
                "prospecting.auto_classify: scorer failed for practice=%s", pkey,
            )
            s = 0
        if s > best_score:
            best_score = s
            best_practice = pkey
    return best_practice, max(0, best_score)


# ---------------------------------------------------------------------------
# Supabase upsert + queries
# ---------------------------------------------------------------------------


_PROSPECT_COLUMNS = {
    "email",
    "first_name",
    "last_name",
    "title",
    "company",
    "company_size_band",
    "vertical",
    "country",
    "enriched_data",
    "icp_score",
    "practice_fit",
    "source",
    "status",
}


def _shape_for_db(prospect: Dict[str, Any]) -> Dict[str, Any]:
    """Project a prospect dict down to the columns the prospects table holds."""
    out: Dict[str, Any] = {}
    for col in _PROSPECT_COLUMNS:
        if col in prospect:
            out[col] = prospect[col]
    # email must be lower-cased + stripped — Supabase will enforce unique on it.
    if out.get("email"):
        out["email"] = str(out["email"]).strip().lower()
    enriched = _coerce_enriched(out.get("enriched_data") or prospect.get("enriched_data"))
    out["enriched_data"] = enriched
    out.setdefault("status", "new")
    out.setdefault("source", prospect.get("source") or "apollo")
    return out


async def _fetch_prospect_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Return the row for `email` or None. URL-encodes the email correctly."""
    enc = _urlquote(email.lower(), safe="")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(
            f"{SUPA_URL}/prospects?email=eq.{enc}&limit=1",
            headers=SUPA_HEADERS,
        )
    if r.status_code != 200:
        log.warning(
            "prospecting._fetch_prospect_by_email non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return None
    rows = r.json() or []
    return rows[0] if rows else None


async def upsert_prospect(prospect: Dict[str, Any]) -> Dict[str, Any]:
    """Insert or update one prospect, keyed on email.

    Uses PostgREST's ``Prefer: resolution=merge-duplicates`` + ``on_conflict=email``
    so the call is a single round-trip. Returns
    ``{ok, prospect_id, action: 'inserted' | 'updated'}``.

    Idempotent: re-running with the same email never duplicates the row.
    """
    payload = _shape_for_db(prospect)
    email = payload.get("email")
    if not email:
        return {"ok": False, "error": "missing email", "prospect_id": None, "action": "noop"}

    existing = await _fetch_prospect_by_email(email)

    headers = {
        **SUPA_HEADERS,
        "Prefer": "return=representation,resolution=merge-duplicates",
    }
    url = f"{SUPA_URL}/prospects?on_conflict=email"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.post(url, headers=headers, json=payload)

    if r.status_code not in (200, 201):
        log.warning(
            "prospecting.upsert_prospect non-200 email=%s status=%s body=%s",
            email, r.status_code, r.text[:300],
        )
        return {
            "ok": False,
            "error": f"{r.status_code}: {r.text[:200]}",
            "prospect_id": existing.get("id") if existing else None,
            "action": "noop",
        }

    body = r.json() if r.text else []
    if isinstance(body, list) and body:
        row = body[0]
    elif isinstance(body, dict):
        row = body
    else:
        row = {}

    action = "updated" if existing else "inserted"
    return {
        "ok": True,
        "prospect_id": row.get("id") or (existing.get("id") if existing else None),
        "action": action,
    }


async def _list_eligible_prospects(
    practice: str,
    min_icp_score: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Pull `status=new` rows for `practice` with `icp_score >= min`."""
    pkey = canonical_practice(practice)
    qs = (
        f"practice_fit=eq.{_urlquote(pkey, safe='')}"
        f"&status=eq.new"
        f"&icp_score=gte.{int(min_icp_score)}"
        f"&order=icp_score.desc"
        f"&limit={int(limit)}"
    )
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(f"{SUPA_URL}/prospects?{qs}", headers=SUPA_HEADERS)
    if r.status_code != 200:
        log.warning(
            "prospecting._list_eligible_prospects non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return []
    return r.json() or []


async def _count_eligible(practice: str, min_icp_score: int) -> int:
    pkey = canonical_practice(practice)
    qs = (
        f"practice_fit=eq.{_urlquote(pkey, safe='')}"
        f"&status=eq.new"
        f"&icp_score=gte.{int(min_icp_score)}"
        f"&select=id"
    )
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(
            f"{SUPA_URL}/prospects?{qs}",
            headers={**SUPA_HEADERS, "Prefer": "count=exact"},
        )
    if r.status_code not in (200, 206):
        return 0
    cr = r.headers.get("content-range") or r.headers.get("Content-Range") or ""
    if "/" in cr:
        try:
            return int(cr.split("/")[-1])
        except ValueError:
            pass
    rows = r.json() or []
    return len(rows)


async def _list_unconverted_for_rescore(limit: int = 500) -> List[Dict[str, Any]]:
    """Pull prospects we may want to re-score after ICP rules change."""
    qs = (
        f"status=in.(new,sequence_running)"
        f"&order=updated_at.desc"
        f"&limit={int(limit)}"
    )
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.get(f"{SUPA_URL}/prospects?{qs}", headers=SUPA_HEADERS)
    if r.status_code != 200:
        log.warning(
            "prospecting._list_unconverted_for_rescore non-200 status=%s body=%s",
            r.status_code, r.text[:200],
        )
        return []
    return r.json() or []


# ---------------------------------------------------------------------------
# Outbound pipeline feed
# ---------------------------------------------------------------------------


async def feed_outbound_pipeline(
    practice: str,
    batch_size: int = 50,
    min_icp_score: int = 70,
) -> Dict[str, Any]:
    """Kick eligible prospects into A1's outbound sequence.

    Query: ``status='new' AND practice_fit=practice AND icp_score >= min``.
    Calls ``lib.outbound.send_outbound_sequence`` for up to ``batch_size``
    rows. Returns::

        {"kicked_off": int, "total_eligible": int, "errors": list, "practice": str}

    Outbound import is local to keep the module loadable when outbound's
    template directory is missing in lightweight test envs.
    """
    # Local import: outbound.py also imports prospects-table helpers from
    # sessions; keeping this lazy avoids any future circular import.
    from lib.outbound import send_outbound_sequence  # noqa: E402

    pkey = canonical_practice(practice)
    total = await _count_eligible(pkey, min_icp_score)
    eligible = await _list_eligible_prospects(pkey, min_icp_score, batch_size)

    kicked = 0
    errors: List[Dict[str, Any]] = []
    for prospect in eligible:
        # Skip prospects mid-sequence (defensive — query already filters but
        # status can race during a batch).
        if (prospect.get("status") or "").lower() != "new":
            continue
        if int(prospect.get("current_touch") or 0) >= 1:
            continue
        try:
            await send_outbound_sequence(
                prospect=prospect,
                practice=pkey,
                sequence_id="v1",
            )
            kicked += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "prospecting.feed_outbound_pipeline: send_outbound_sequence "
                "failed prospect=%s: %s", prospect.get("id"), exc,
            )
            errors.append({"prospect_id": prospect.get("id"), "error": str(exc)})

    return {
        "practice": pkey,
        "kicked_off": kicked,
        "total_eligible": total,
        "batch_size": batch_size,
        "min_icp_score": min_icp_score,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Higher-level orchestration: search -> enrich -> score -> upsert
# ---------------------------------------------------------------------------


async def search_enrich_score_upsert(
    query: Dict[str, Any],
    practice: str,
    limit: int = 50,
) -> Dict[str, Any]:
    """End-to-end: Apollo search, enrich, score, upsert into prospects.

    Returns ``{searched, upserted, inserted, updated, sample[]}``.
    """
    pkey = canonical_practice(practice)
    raw = await search_prospects_apollo(query, limit=limit)
    inserted = 0
    updated = 0
    upserted = 0
    sample: List[Dict[str, Any]] = []

    for row in raw:
        if not row.get("email"):
            continue
        try:
            enriched = await enrich_prospect(row)
        except Exception:  # noqa: BLE001
            log.exception("prospecting.search_enrich_score_upsert: enrich failed")
            enriched = row
        try:
            score = await score_icp(enriched, pkey)
        except Exception:  # noqa: BLE001
            log.exception("prospecting.search_enrich_score_upsert: score failed")
            score = 0
        enriched["icp_score"] = score
        enriched["practice_fit"] = pkey
        try:
            res = await upsert_prospect(enriched)
        except Exception as exc:  # noqa: BLE001
            log.warning("prospecting.search_enrich_score_upsert: upsert failed: %s", exc)
            continue
        if res.get("ok"):
            upserted += 1
            if res.get("action") == "inserted":
                inserted += 1
            else:
                updated += 1
            if len(sample) < 10:
                sample.append({
                    "email": enriched.get("email"),
                    "company": enriched.get("company"),
                    "title": enriched.get("title"),
                    "icp_score": score,
                    "practice_fit": pkey,
                })

    return {
        "searched": len(raw),
        "upserted": upserted,
        "inserted": inserted,
        "updated": updated,
        "practice": pkey,
        "sample": sample,
    }


# ---------------------------------------------------------------------------
# FastAPI router (admin)
# ---------------------------------------------------------------------------


def _admin_auth(request: Request) -> None:
    """Mirror of app.py's `_admin_auth`. Accept ``?key=`` or ``Authorization: Bearer``."""
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API not configured")
    key = request.query_params.get("key") or ""
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:]
    if key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


router = APIRouter(prefix="/api/prospecting", tags=["prospecting"])


@router.post("/search")
async def http_search(request: Request) -> Dict[str, Any]:
    """Body: ``{query: {...}, practice: str, limit: int}``.

    Runs Apollo search -> enrich -> score -> upsert. Returns counts + sample.
    """
    _admin_auth(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    query = body.get("query") or {}
    if not isinstance(query, dict):
        raise HTTPException(status_code=400, detail="query must be an object")
    practice = body.get("practice") or ""
    if not practice:
        raise HTTPException(status_code=400, detail="practice is required")
    limit = int(body.get("limit") or 50)

    try:
        result = await search_enrich_score_upsert(query, practice, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.exception("prospecting.http_search failed")
        raise HTTPException(status_code=500, detail=f"search failed: {exc}")
    return {"ok": True, **result}


@router.post("/score-batch")
async def http_score_batch(request: Request) -> Dict[str, Any]:
    """Re-score all unconverted prospects against current ICP rules.

    Useful after Mila edits ``lib/icp_rules.py``. Body (optional):
    ``{limit: int, practice: str | null}``. If `practice` omitted we
    re-score against each row's current `practice_fit` (or auto-classify
    when missing).
    """
    _admin_auth(request)
    try:
        body = await request.json() if (await request.body()) else {}
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    limit = int(body.get("limit") or 500)
    forced_practice = body.get("practice") or None
    forced_pkey = canonical_practice(forced_practice) if forced_practice else None

    rows = await _list_unconverted_for_rescore(limit=limit)
    rescored = 0
    classified = 0
    errors: List[Dict[str, Any]] = []
    for row in rows:
        try:
            if forced_pkey:
                score = await score_icp(row, forced_pkey)
                pkey = forced_pkey
            else:
                if row.get("practice_fit"):
                    pkey = canonical_practice(row.get("practice_fit"))
                    score = await score_icp(row, pkey)
                else:
                    pkey, score = await auto_classify_practice_fit(row)
                    classified += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"prospect_id": row.get("id"), "error": str(exc)})
            continue

        try:
            await _patch_prospect_score(row.get("id"), score, pkey)
            rescored += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"prospect_id": row.get("id"), "error": f"patch: {exc}"})

    return {
        "ok": True,
        "rescored": rescored,
        "newly_classified": classified,
        "errors": errors,
        "total_considered": len(rows),
    }


async def _patch_prospect_score(prospect_id: Optional[str], score: int, practice: str) -> None:
    if not prospect_id:
        return
    enc = _urlquote(str(prospect_id), safe="")
    payload = {
        "icp_score": int(score),
        "practice_fit": canonical_practice(practice),
        "updated_at": _now_iso(),
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.patch(
            f"{SUPA_URL}/prospects?id=eq.{enc}",
            headers=SUPA_HEADERS,
            json=payload,
        )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"patch failed: {r.status_code} {r.text[:200]}")


@router.post("/feed-outbound")
async def http_feed_outbound(request: Request) -> Dict[str, Any]:
    """Body: ``{practice: str, batch_size: int, min_icp_score: int}``."""
    _admin_auth(request)
    try:
        body = await request.json() if (await request.body()) else {}
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    practice = body.get("practice") or ""
    if not practice:
        raise HTTPException(status_code=400, detail="practice is required")
    batch_size = int(body.get("batch_size") or 50)
    min_score = int(body.get("min_icp_score") or 70)
    try:
        result = await feed_outbound_pipeline(
            practice=practice,
            batch_size=batch_size,
            min_icp_score=min_score,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("prospecting.http_feed_outbound failed")
        raise HTTPException(status_code=500, detail=f"feed failed: {exc}")
    return {"ok": True, **result}


@router.get("/stats")
async def http_stats(request: Request) -> Dict[str, Any]:
    """Counts by practice + status + crude score-bucket histogram."""
    _admin_auth(request)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        # Pull a wide window of recent rows. For large tables Mila can
        # paginate; for v1 we cap at 5k which is plenty for a boutique funnel.
        r = await client.get(
            f"{SUPA_URL}/prospects?select=id,status,practice_fit,icp_score&limit=5000",
            headers=SUPA_HEADERS,
        )
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"supabase {r.status_code}: {r.text[:200]}",
        )
    rows = r.json() or []

    by_practice: Counter = Counter()
    by_status: Counter = Counter()
    score_buckets: Counter = Counter()
    for row in rows:
        practice = row.get("practice_fit") or "_unset_"
        by_practice[practice] += 1
        status = row.get("status") or "_unset_"
        by_status[status] += 1
        score = row.get("icp_score")
        if score is None:
            score_buckets["unscored"] += 1
        else:
            try:
                s = int(score)
            except (TypeError, ValueError):
                score_buckets["unscored"] += 1
                continue
            if s < 25:
                score_buckets["0-24"] += 1
            elif s < 50:
                score_buckets["25-49"] += 1
            elif s < 70:
                score_buckets["50-69"] += 1
            elif s < 85:
                score_buckets["70-84"] += 1
            else:
                score_buckets["85-100"] += 1

    return {
        "ok": True,
        "total": len(rows),
        "by_practice": dict(by_practice),
        "by_status": dict(by_status),
        "score_distribution": dict(score_buckets),
    }


# ---------------------------------------------------------------------------
# Module exports
# ---------------------------------------------------------------------------


__all__ = [
    "search_prospects_apollo",
    "enrich_prospect",
    "score_icp",
    "auto_classify_practice_fit",
    "upsert_prospect",
    "feed_outbound_pipeline",
    "search_enrich_score_upsert",
    "router",
]
