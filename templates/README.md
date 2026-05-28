# Poster templates: Rubric Scoreboard & Daily Digest

Jinja2 + Tailwind (CDN) HTML templates that the Playwright renderer screenshots to PNG for posting in `#ask-ai-evals`. Companion to plan §"The Poster Design" in `/Users/pw/.claude/plans/here-is-the-sixth-buzzing-sundae.md`.

Width is fixed at **640px**; height grows with content (`full_page` screenshot).

## Files

| File | Purpose |
|---|---|
| `poster_scoreboard.html.j2` | Rubric Scoreboard poster (daily eval, ~12:00 IST) |
| `poster_digest.html.j2` | Daily Digest poster (~12:08 IST) |
| `_sparkline.html.j2` | Shared Jinja macro for inline SVG sparkline (~120×24px), accepts `spark_series: list[float]` of length ≤14. Not rendered by Variant D, reserved for future variants. |
| `sample_inputs/*.json` | Fixture data for local render review |

## Design constraints (locked)

- **Typography**: Hanken Grotesk (body, weights 400/500/600/700) + IBM Plex Mono (tabular figures), self-hosted as WOFF2 in `static/fonts/`. No Google Fonts CDN at render time. `font-feature-settings: "tnum","zero"` on numeric columns.
- **Brand**: PhysicsWallah Ask AI. FT/Stripe craft level. Anti-slop emoji discipline: only semantic state (🟢🟡🔴 ⚠️ ▲▼ ·) and section anchors (📰 💡 ⚙️ 🛟, one per section).
- **PII / sensitivity** (locked decision #8): NO cost figures, NO raw quoted student feedback, NO per-chapter downvote rates inside the image. Templates have no fields for those. Insights may describe trends narratively only.
- **Kill-switch**: when `kill_switch_breach: true` the poster renders a slim red-tinted band above the headline. The Academic FAIL value and delta text turn brick-red; no side-stripe border (banned per impeccable). The Digest mirrors the band for downvote-rate / VCP breaches.
- **Accessibility**: every numeric value has a text-equivalent inside the semantic DOM so the alt-text extractor (the Playwright step) can lift a high-fidelity description without OCR.

## JSON schemas

### Scoreboard (`poster_scoreboard.html.j2`)

```jsonc
{
  "date_human": "Tue · 27 May",          // display string, free
  "date_iso": "2026-05-27",              // YYYY-MM-DD, used in alt-text
  "n_judged": 989,                       // int, sample size for the run
  "kill_switch_breach": true,            // bool, forces red treatment
  "headline": "string ≤180 chars, ends with period.",
  "scoreboard": [                        // 3 rows, fixed order: academic, experience, overall
    {
      "label": "Academic FAIL",
      "value_text": "8.2%",              // pre-formatted (renderer is dumb on units)
      "delta_text": "+2.1pp vs WoW",     // free, may be "n/a"
      "delta_dir": "up" | "down" | "flat",
      "state": "red" | "yellow" | "green" | "neutral",
      "note": "above 6% floor"           // ≤32 chars contextual hint
    }
  ],
  "top_drivers": [                       // 3 items, ranked
    { "code": "A5", "label": "answer incomplete", "count": 137, "bar_pct": 100 }
  ],
  "trend": {
    "label": "14-day Academic FAIL trend",
    "spark_series": [3.1, /* up to 14 floats */ 8.2]
  },
  "brand_mark": "Ask AI · daily eval"
}
```

### Digest (`poster_digest.html.j2`)

```jsonc
{
  "date_human": "Mon · 26 May",
  "date_iso": "2026-05-26",
  "kill_switch_breach": false,
  "headline": "string ≤180 chars, narrative claim, ends with period.",
  "subhead": "optional ≤200 chars second sentence, or null",
  "insights": [                          // 0..5 items; 0 → quiet-day render
    {
      "topic_label": "CLARITY",          // CLARITY | LATENCY | FEEDBACK | COST | ACCURACY | USAGE
      "icon": "📈",                      // one of: 📈 ⚠️ 💬 💸 🎯 🔥
      "claim": "≤90 char delta-anchored sentence",
      "evidence": "≤90 char supporting metric",
      "context": "≤90 char cross-signal join, or null",
      "spark_series": [/* 0..14 floats */] // or null
    }
  ],
  "brand_mark": "Ask AI · daily digest"
}
```

## Local render: review command

These are plain Jinja templates with CDN Tailwind, so the easiest preview path is a one-shot Python render → HTML file → open in a browser.

```bash
# from repo root
python -c "
import json, pathlib
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
for name, tmpl in [
    ('scoreboard_breach',  ('poster_scoreboard.html.j2', 'sample_inputs/scoreboard_breach_day.json')),
    ('scoreboard_normal',  ('poster_scoreboard.html.j2', 'sample_inputs/scoreboard_normal_day.json')),
    ('digest_normal',      ('poster_digest.html.j2',     'sample_inputs/digest_normal_day.json')),
    ('digest_quiet',       ('poster_digest.html.j2',     'sample_inputs/digest_quiet_day.json')),
]:
    tmpl_path, data_path = tmpl
    data = json.loads(pathlib.Path('templates', data_path).read_text())
    html = env.get_template(tmpl_path).render(**data)
    out = pathlib.Path(f'/tmp/poster_{name}.html')
    out.write_text(html)
    print(out)
"

# then
open /tmp/poster_scoreboard_breach.html \
     /tmp/poster_scoreboard_normal.html \
     /tmp/poster_digest_normal.html \
     /tmp/poster_digest_quiet.html
```

To screenshot at the production width:

```bash
# requires playwright; one-off install: pip install playwright && playwright install chromium
python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page(viewport={'width': 640, 'height': 900}, device_scale_factor=2)
    page.goto('file:///tmp/poster_scoreboard_breach.html')
    page.wait_for_load_state('networkidle')
    page.screenshot(path='/tmp/poster_scoreboard_breach.png', full_page=True)
    b.close()
"
open /tmp/poster_scoreboard_breach.png
```

## What this dir does NOT contain

- The renderer module (lives in `posters/render.py` per C1.2b, not shipped here).
- Slack payload assembly (C1.3, separate ticket).
- The insights-generator LLM prompt (C1.1, separate ticket).
