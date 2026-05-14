"""Thin Metabase /api/dataset client for ad-hoc native (SQL) queries.

Why a separate module: the existing `fetch_metabase_card_*` helpers in
`daily_digest.py` hit `/api/card/{id}/query/json` for *saved* questions. The
cost & latency section needs ad-hoc native queries against `cdp.central.silver_stream_logs`
parametrised by a UTC date window — saved-card route can't carry SQL + params,
so we go through the `/api/dataset` endpoint with X-API-KEY auth.

Public surface:
    run_native_query(sql, params, database_id=895) -> list[dict]

`sql` may contain Metabase `{{template_tag}}` placeholders; values come from
`params` (dict[str, str]) as date/single parameters. Response rows are zipped
back into dicts using `data.cols[*].name` for column order.

Retry policy: 1 retry on 5xx / connection error with 10s backoff. No retry on
4xx (a bad query won't get better next attempt). Read timeout = 60s.

Env vars (read per-call, not at import time):
    METABASE_HOST     — base URL, e.g. https://metabase-prod.penpencil.co
                        (falls back to METABASE_URL for parity with the rest
                        of the codebase, which only has METABASE_URL today).
    METABASE_API_KEY  — header value for X-API-KEY. Empty/unset raises
                        MetabaseQueryError before any HTTP call so the
                        misconfiguration shows up clearly in logs (rather
                        than as a downstream 401 from Metabase).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_TIMEOUT_SEC = 60
DEFAULT_DATABASE_ID = 895  # Trino-Prod (validated by Data Engineer)
DEFAULT_METABASE_HOST = "https://metabase-prod.penpencil.co"


class MetabaseQueryError(RuntimeError):
    """Raised when /api/dataset returns a non-recoverable error."""


def _read_host() -> str:
    return (
        os.environ.get("METABASE_HOST")
        or os.environ.get("METABASE_URL")
        or DEFAULT_METABASE_HOST
    ).rstrip("/")


def _read_api_key() -> str:
    return os.environ.get("METABASE_API_KEY", "") or ""


def _build_body(sql: str, params: Dict[str, str], database_id: int) -> Dict[str, Any]:
    """Build the JSON body for POST /api/dataset.

    Each key in `params` becomes both a `template-tag` declaration AND a
    `parameters[]` entry. We always send them as `date/single` since the only
    caller (cost/latency fetcher) parametrises on UTC midnight ISO timestamps.
    """
    template_tags = {
        name: {
            "id": name,
            "name": name,
            "display-name": name,
            "type": "date",
            "required": True,
        }
        for name in params
    }
    parameters = [
        {
            "type": "date/single",
            "target": ["variable", ["template-tag", name]],
            "value": value,
        }
        for name, value in params.items()
    ]
    return {
        "database": database_id,
        "type": "native",
        "native": {"query": sql, "template-tags": template_tags},
        "parameters": parameters,
    }


def _post_once(url: str, body: Dict[str, Any], api_key: str, timeout: int) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_native_query(
    sql: str,
    params: Dict[str, str],
    database_id: int = DEFAULT_DATABASE_ID,
    *,
    timeout: int = DEFAULT_TIMEOUT_SEC,
) -> List[Dict[str, Any]]:
    """Execute a native (SQL) query and return rows as list[dict].

    On 5xx or connection error: 1 retry with 10s backoff.
    On 4xx: raise immediately (won't get better with a retry).
    On missing METABASE_API_KEY: raise immediately (distinguishes
    misconfiguration from a Metabase-side 401).
    """
    api_key = _read_api_key()
    if not api_key.strip():
        raise MetabaseQueryError("METABASE_API_KEY not set")

    host = _read_host()
    url = f"{host}/api/dataset"
    body = _build_body(sql, params, database_id)

    last_exc: Optional[BaseException] = None
    for attempt in range(2):  # 1 initial + 1 retry
        try:
            payload = _post_once(url, body, api_key, timeout)
            break
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                # Bad query / auth — won't recover with a retry.
                raise MetabaseQueryError(
                    f"Metabase /api/dataset {exc.code}: {exc.reason}"
                ) from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
        if attempt == 0:
            print(
                f"[warn] metabase_client retry after: {last_exc!r}",
                file=sys.stderr,
            )
            time.sleep(10)
    else:
        raise MetabaseQueryError(
            f"Metabase /api/dataset failed after retry: {last_exc!r}"
        )

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MetabaseQueryError(
            f"Metabase /api/dataset returned malformed payload: {payload!r}"
        )
    cols = data.get("cols") or []
    rows = data.get("rows") or []
    col_names = [c.get("name") for c in cols]
    return [dict(zip(col_names, row)) for row in rows]
