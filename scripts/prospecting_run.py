"""Search Apollo -> enrich -> score -> upsert prospects, optionally feed outbound.

Usage:
    python scripts/prospecting_run.py \\
        --practice cloud_finops \\
        --limit 100 \\
        [--titles "CTO,VP Engineering"] \\
        [--industries "saas,fintech"] \\
        [--country BR] \\
        [--keywords "aws kubernetes"] \\
        [--size-min 20 --size-max 500] \\
        [--auto-feed] \\
        [--feed-min-score 70] \\
        [--feed-batch-size 50]

Defaults are pulled from ``lib/icp_rules.py`` so each practice has reasonable
ICP-aligned defaults when CLI args are omitted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make sibling `lib/` importable when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.icp_rules import canonical_practice, get_rules  # noqa: E402
from lib.prospecting import (  # noqa: E402
    feed_outbound_pipeline,
    search_enrich_score_upsert,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("prospecting_run")


def _split_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _defaults_from_rules(practice: str) -> Dict[str, Any]:
    """Pull title / vertical / size defaults from the practice's ICP rules."""
    try:
        rules = get_rules(practice)
    except KeyError:
        return {}
    th = rules.get("thresholds", {})
    return {
        "titles": list(th.get("titles_in", []))[:8],
        "industries": list(th.get("verticals_in", []))[:6],
        "company_size_min": th.get("company_size_min"),
        "company_size_max": th.get("company_size_max"),
    }


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    practice = canonical_practice(args.practice)
    defaults = _defaults_from_rules(practice)

    titles = _split_csv(args.titles) or defaults.get("titles") or []
    industries = _split_csv(args.industries) or defaults.get("industries") or []
    country = args.country or "BR"
    keywords = args.keywords or ""
    size_min = args.size_min if args.size_min is not None else defaults.get("company_size_min")
    size_max = args.size_max if args.size_max is not None else defaults.get("company_size_max")

    query: Dict[str, Any] = {
        "titles": titles,
        "industries": industries,
        "country": country,
        "keywords": keywords,
    }
    if size_min is not None:
        query["company_size_min"] = int(size_min)
    if size_max is not None:
        query["company_size_max"] = int(size_max)

    log.info(
        "prospecting_run: practice=%s limit=%d query=%s",
        practice, args.limit, json.dumps(query, ensure_ascii=False),
    )

    search_result = await search_enrich_score_upsert(
        query=query, practice=practice, limit=args.limit,
    )
    log.info("prospecting_run: search summary %s", json.dumps({
        k: v for k, v in search_result.items() if k != "sample"
    }))

    feed_result: Optional[Dict[str, Any]] = None
    if args.auto_feed:
        feed_result = await feed_outbound_pipeline(
            practice=practice,
            batch_size=args.feed_batch_size,
            min_icp_score=args.feed_min_score,
        )
        log.info(
            "prospecting_run: feed summary %s",
            json.dumps({k: v for k, v in feed_result.items() if k != "errors"}),
        )

    return {"search": search_result, "feed": feed_result}


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Anuvia prospecting batch: Apollo search + enrich + score + upsert."
    )
    p.add_argument("--practice", required=True,
                   help="Practice key (cloud_finops, ai, devops, growth, industry).")
    p.add_argument("--limit", type=int, default=50,
                   help="Max prospects to fetch from Apollo (default 50).")
    p.add_argument("--titles", default="",
                   help="Comma-separated Apollo title filters.")
    p.add_argument("--industries", default="",
                   help="Comma-separated Apollo industry filters.")
    p.add_argument("--country", default="",
                   help="Apollo country filter (default BR).")
    p.add_argument("--keywords", default="",
                   help="Free-text keyword filter.")
    p.add_argument("--size-min", dest="size_min", type=int, default=None,
                   help="Min company headcount.")
    p.add_argument("--size-max", dest="size_max", type=int, default=None,
                   help="Max company headcount.")
    p.add_argument("--auto-feed", action="store_true",
                   help="After upsert, kick eligible prospects into outbound.")
    p.add_argument("--feed-min-score", dest="feed_min_score", type=int, default=70,
                   help="Min ICP score for outbound feed (default 70).")
    p.add_argument("--feed-batch-size", dest="feed_batch_size", type=int, default=50,
                   help="Max prospects to kick off per --auto-feed run.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    result = asyncio.run(run(args))
    print(json.dumps({
        "search": {k: v for k, v in (result.get("search") or {}).items() if k != "sample"},
        "search_sample": (result.get("search") or {}).get("sample"),
        "feed": result.get("feed"),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
