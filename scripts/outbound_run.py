"""Dispatch an outbound sequence batch from a CSV.

Usage:
    python scripts/outbound_run.py \
        --csv prospects.csv \
        --practice finops \
        --sequence v1 \
        [--dry-run] [--limit 50] [--source manual_csv]

CSV columns (header row required):
    email, first_name, last_name, title, company, vertical, country

What it does:
  1. Reads the CSV row by row.
  2. Inserts each prospect into Supabase `prospects` (skip duplicates by email).
  3. Applies a placeholder ICP score (real Apollo enrichment lives in A5).
  4. Queues touch 1 via `lib.outbound.send_outbound_sequence`.

This is the operator-facing entrypoint. The orchestrator picks up touches 2
and 3 automatically once the prospects table has `next_touch_at` set.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote as _urlquote

import httpx

# Ensure we can import lib.* when invoked from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.outbound import send_outbound_sequence  # noqa: E402
from lib.sessions import SUPA_HEADERS, SUPA_URL  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("outbound_run")


_REQUIRED_CSV_COLUMNS = (
    "email", "first_name", "last_name", "title", "company", "vertical", "country",
)


# ---------------------------------------------------------------------------
# Prospect persistence (subset of A5's prospecting.py — minimal, idempotent)
# ---------------------------------------------------------------------------


async def _fetch_existing_prospect(email: str) -> Optional[dict]:
    """Return the prospect row for `email` if it already exists."""
    enc = _urlquote(email.lower(), safe="@.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{SUPA_URL}/prospects?email=ilike.{enc}&limit=1",
            headers=SUPA_HEADERS,
        )
    if r.status_code != 200:
        log.warning(
            "fetch_existing_prospect non-200 email=%s: %s %s",
            email, r.status_code, r.text[:200],
        )
        return None
    rows = r.json() or []
    return rows[0] if rows else None


def _placeholder_icp_score(row: dict, practice: str) -> int:
    """Tiny ICP heuristic — A5 will replace this with real Apollo signals.

    Scores 0-100. Defaults to 50. Bumps for senior titles + matching vertical.
    """
    score = 50
    title = (row.get("title") or "").lower()
    if any(t in title for t in ("cto", "vp eng", "vp engineering", "head of engineering", "head cloud")):
        score += 25
    elif any(t in title for t in ("director", "principal", "head", "lead")):
        score += 12

    vertical = (row.get("vertical") or "").lower()
    if practice == "finops" and vertical in ("saas", "fintech", "ecommerce", "e-commerce", "marketplace"):
        score += 10
    if practice == "ai" and vertical in ("saas", "fintech", "ecommerce", "e-commerce", "marketplace", "media"):
        score += 10

    country = (row.get("country") or "").lower()
    if country in ("br", "brazil", "brasil"):
        score += 5

    return max(0, min(100, score))


async def _insert_prospect(row: dict, practice: str, source: str) -> Optional[dict]:
    """Insert one prospect. Returns the row (existing or new) or None on error."""
    email = (row.get("email") or "").strip()
    if not email:
        log.warning("skipping row with empty email: %r", row)
        return None

    existing = await _fetch_existing_prospect(email)
    if existing:
        log.info("prospect already exists email=%s id=%s — skipping insert", email, existing.get("id"))
        return existing

    icp = _placeholder_icp_score(row, practice)
    payload = {
        "email": email,
        "first_name": (row.get("first_name") or "").strip() or None,
        "last_name": (row.get("last_name") or "").strip() or None,
        "title": (row.get("title") or "").strip() or None,
        "company": (row.get("company") or "").strip() or None,
        "vertical": (row.get("vertical") or "").strip() or None,
        "country": (row.get("country") or "").strip() or None,
        "icp_score": icp,
        "practice_fit": practice,
        "source": source,
        "status": "new",
        "enriched_data": {
            "icp_score_origin": "placeholder_heuristic",
            "imported_via": "scripts/outbound_run.py",
        },
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{SUPA_URL}/prospects",
            headers=SUPA_HEADERS,
            json=payload,
        )
    if r.status_code in (200, 201):
        body = r.json()
        if isinstance(body, list) and body:
            return body[0]
        if isinstance(body, dict):
            return body
    log.error(
        "_insert_prospect failed email=%s status=%s body=%s",
        email, r.status_code, r.text[:300],
    )
    return None


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> List[Dict[str, str]]:
    """Read CSV into a list of dicts. Validates the required columns."""
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in _REQUIRED_CSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(
                f"CSV {path} is missing required columns: {missing}. "
                f"Required: {list(_REQUIRED_CSV_COLUMNS)}"
            )
        return [dict(r) for r in reader]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(
    csv_path: Path,
    practice: str,
    sequence: str,
    dry_run: bool,
    limit: Optional[int],
    source: str,
) -> Dict[str, Any]:
    rows = _read_csv(csv_path)
    if limit:
        rows = rows[: int(limit)]

    log.info(
        "outbound_run: %d rows from %s, practice=%s, sequence=%s, dry_run=%s",
        len(rows), csv_path, practice, sequence, dry_run,
    )

    inserted = 0
    queued = 0
    skipped = 0
    failed = 0
    results: List[Dict[str, Any]] = []

    for row in rows:
        email = (row.get("email") or "").strip()
        if not email:
            skipped += 1
            continue

        if dry_run:
            log.info(
                "[DRY] would insert + queue: email=%s company=%s practice=%s",
                email, row.get("company"), practice,
            )
            queued += 1
            results.append({"email": email, "status": "dry_run"})
            continue

        prospect = await _insert_prospect(row, practice=practice, source=source)
        if not prospect:
            failed += 1
            results.append({"email": email, "status": "insert_failed"})
            continue

        is_new = (prospect.get("status") or "new") == "new" and not prospect.get("current_touch")
        if is_new:
            inserted += 1

        # Skip if already in a sequence or terminal state.
        status = (prospect.get("status") or "").lower()
        if status not in ("new",):
            log.info(
                "skipping queue: prospect %s status=%s (not 'new')",
                prospect.get("id"), status,
            )
            skipped += 1
            results.append({"email": email, "status": f"skipped_{status}"})
            continue

        try:
            seq_result = await send_outbound_sequence(
                prospect=prospect,
                practice=practice,
                sequence_id=sequence,
            )
            queued += 1
            results.append({
                "email": email,
                "prospect_id": seq_result.get("prospect_id"),
                "message_id": seq_result.get("message_id"),
                "status": "queued",
            })
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.exception("send_outbound_sequence failed email=%s: %s", email, exc)
            results.append({"email": email, "status": "send_failed", "error": str(exc)})

    summary = {
        "total_rows": len(rows),
        "inserted": inserted,
        "queued": queued,
        "skipped": skipped,
        "failed": failed,
        "dry_run": dry_run,
    }
    log.info("outbound_run summary: %s", json.dumps(summary))
    return {"summary": summary, "results": results}


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Dispatch an Anuvia outbound sequence batch from a CSV."
    )
    p.add_argument("--csv", required=True, help="Path to prospects CSV.")
    p.add_argument(
        "--practice", required=True,
        choices=["finops", "ai", "devops", "growth", "industry"],
        help="Practice fit for these prospects.",
    )
    p.add_argument("--sequence", default="v1", help="Sequence id (default: v1).")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Parse + validate CSV but skip Supabase writes and Resend sends.",
    )
    p.add_argument("--limit", type=int, default=None, help="Cap the number of rows processed.")
    p.add_argument(
        "--source", default="manual_csv",
        help="Source label written to prospects.source (default: manual_csv).",
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    csv_path = Path(args.csv).expanduser().resolve()
    result = asyncio.run(
        run(
            csv_path=csv_path,
            practice=args.practice,
            sequence=args.sequence,
            dry_run=args.dry_run,
            limit=args.limit,
            source=args.source,
        )
    )
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
