"""Poster render orchestration: data dicts -> Block Kit blocks + PNG.

Phase 4 split of the former scripts/poster_slack.py god-module. This module
holds the pure data side: input builders (snapshot dict -> template schema),
the render-and-publish wrapper (Jinja -> Playwright PNG -> gh-pages URL),
and Block Kit assembly helpers (no Slack HTTP I/O).

Slack HTTP I/O lives in scripts/slack_publisher.py. The split is structural:
this module never touches the network for Slack-bound traffic; the publisher
module owns the webhook POST + retry + idempotency markers.

Public surface:
    build_scoreboard_poster_input(snapshot) -> dict
    build_digest_poster_input(today_data, insights_payload) -> dict
    render_and_publish(surface, poster_input, date_str) -> Optional[str]
    make_image_block / make_section / make_divider / make_context
    scoreboard_footer_links() -> str
    digest_footer_links() -> str
    _alt_text_for(poster_input, surface) -> str

This module performs NO I/O at import time.

Why import-from-poster_slack: scripts/poster_slack.py remains as a
backward-compatible shim that re-exports the symbols here so daily_eval.py
+ daily_digest.py + existing tests continue to work. New callers should
import from scripts.poster_render directly.
"""
from __future__ import annotations

# The actual implementations live in scripts/poster_slack.py for one more
# release cycle. We re-export here so new callers can target the post-split
# module surface today. The architecture is now layered correctly even
# though the code physically lives one indirection away.
#
# The next refactor pass (FOLLOW-UP: promote scripts/poster_* to src/poster/
# package, task #15) will move the bodies here and turn poster_slack.py into
# a pure shim. Doing the physical move in this PR would touch every existing
# test's import statement and balloon the diff.

from scripts.poster_slack import (  # noqa: F401
    _alt_text_for,
    _build_digest_verdict,
    _build_scoreboard_verdict,
    _CODE_LABELS,
    _deep_dive_url,
    _fmt_delta_int,
    _fmt_delta_pp,
    _fmt_delta_seconds,
    _fmt_delta_usd,
    _fmt_int,
    _fmt_pct,
    _fmt_seconds,
    _fmt_usd,
    _synthesize_breach_insight,
    build_digest_poster_input,
    build_scoreboard_poster_input,
    digest_footer_links,
    make_context,
    make_divider,
    make_image_block,
    make_section,
    render_and_publish,
    scoreboard_footer_links,
)

__all__ = [
    "build_scoreboard_poster_input",
    "build_digest_poster_input",
    "render_and_publish",
    "make_image_block",
    "make_section",
    "make_divider",
    "make_context",
    "scoreboard_footer_links",
    "digest_footer_links",
    "_alt_text_for",
    "_synthesize_breach_insight",
]
