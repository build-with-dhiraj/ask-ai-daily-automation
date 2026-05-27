# Poster renderer (`scripts/poster_renderer.py`)

Phase **C1.2b** of the poster-format redesign. Takes a snapshot dict + a
surface name (`scoreboard` | `digest`), renders the corresponding Jinja
template under `templates/`, screenshots it via headless Chromium at 2×
device-pixel ratio, and returns PNG bytes.

Caller (the Slack payload assembler in C1.3, future) is expected to wrap
this in `try / except PosterRenderError` and fall back to text-only Block
Kit on failure. The pipeline must never crash because the renderer failed.

---

## Install

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

The Chromium download is ~185 MB and lands in
`~/Library/Caches/ms-playwright/` (macOS) or `~/.cache/ms-playwright/`
(Linux). On the GitHub Actions self-hosted Mac runner this is a one-time
cost — cache it across runs.

`playwright` is pinned to `1.50.0` in `requirements.txt`. If you bump it,
also re-run `python -m playwright install chromium` so the bundled
Chromium build matches.

---

## Local render — the fast feedback loop

```bash
python -m scripts.poster_renderer scoreboard templates/sample_inputs/scoreboard_breach_day.json
# → prints: /tmp/poster_scoreboard_<ts>.png  (<size> KB)
open /tmp/poster_scoreboard_*.png
```

Sample inputs live under `templates/sample_inputs/`:

| Sample | Surface |
|---|---|
| `scoreboard_normal_day.json` | `scoreboard` |
| `scoreboard_breach_day.json` | `scoreboard` |
| `digest_normal_day.json` | `digest` |
| `digest_quiet_day.json` | `digest` |

Observed output sizes (Chromium 1223 on macOS, 2× DPR, 640px wide):

| Sample | PNG size |
|---|---|
| `scoreboard_normal_day` | ~140 KB |
| `scoreboard_breach_day` | ~136 KB |
| `digest_normal_day` | ~244 KB |
| `digest_quiet_day` | ~65 KB |

All well under any Slack image cap.

---

## Public API

```python
from pathlib import Path
from scripts.poster_renderer import render_poster, PosterRenderError

try:
    png_bytes = render_poster(
        surface="scoreboard",         # or "digest"
        snapshot=snapshot_dict,        # matches templates/README.md schema
        output_path=Path("/tmp/x.png") # optional; also writes to disk
    )
except PosterRenderError as e:
    # Fall back to text-only Block Kit.
    # e.template is the Jinja template name; e.reason is a short string.
    log.warning("poster render failed: %s", e)
    png_bytes = None
```

### `PosterRenderError` contract for callers

`PosterRenderError` is the **only** exception `render_poster` is allowed
to raise on a failed render. It carries:

- `e.template` — the Jinja template name that failed (e.g.
  `poster_scoreboard.html.j2`).
- `e.reason` — a short, human-readable reason (e.g. `playwright timeout
  after 30000ms`, `jinja render failed: ...`, `playwright not installed`).

Callers must catch it and degrade to text-only Block Kit. Do **not** let
it bubble up — a failed poster render is a designed failure mode of the
pipeline, not a crash.

Unknown `surface` values, missing Playwright, Jinja errors, Chromium
launch failures, networkidle timeouts, and the 30 s hard budget all
funnel into this one exception type.

---

## Output convention

| Knob | Value | Why |
|---|---|---|
| Viewport width | 640 px | Slack mobile-first; matches `templates/README.md` |
| Initial viewport height | 100 px | Grown by `full_page=True` screenshot |
| Device scale factor | **2** | Retina output — crisp on macOS/iOS Slack |
| Screenshot type | PNG, `omit_background=False` | White background, lossless |
| Chromium launch arg | `--font-render-hinting=none` | Avoids OS-level hinting drift between Mac dev + Linux CI |

---

## Notes on font loading

Templates pull **Inter** + **IBM Plex Mono** from Google Fonts via
`<link rel="stylesheet">`. To avoid a fallback-font flash in the
screenshot:

1. `page.set_content(html, wait_until="networkidle")` waits for the
   stylesheet `@font-face` declarations to be reachable.
2. `page.evaluate("document.fonts.ready")` is the belt-and-braces step
   — it returns the JS promise that resolves only once every declared
   font face has actually loaded into the document.

If the runner has no internet (e.g. an offline CI box), the screenshot
will fall back to the configured `system-ui, sans-serif` stack — still
readable, but visibly off-brand. Make sure outbound HTTPS to
`fonts.googleapis.com` + `fonts.gstatic.com` is open on the runner.

---

## Failure handling summary

| Failure mode | What `render_poster` does | What caller should do |
|---|---|---|
| Unknown `surface` arg | `PosterRenderError("unknown surface...")` | Log + skip poster |
| Playwright not installed | `PosterRenderError(... install hint ...)` | Same |
| Jinja template syntax / missing key | `PosterRenderError("jinja render failed: ...")` | Same |
| Chromium launch crash | `PosterRenderError("playwright error: ...")` | Same |
| `networkidle` / 30 s timeout | `PosterRenderError("playwright timeout ...")` | Same |
| Output isn't a valid PNG | `PosterRenderError("output is not a valid PNG")` | Same |

The 30 s hard budget covers the full `set_content + fonts.ready +
screenshot` envelope. Render budget in practice on a warm Chromium:
~3–6 s; cold Chromium launch adds ~1–2 s.

---

## Tests

`tests/test_poster_renderer.py` renders all four sample inputs and
asserts PNG magic bytes. They auto-skip when Chromium isn't installed
locally — see `tests/conftest.py::_chromium_installed`. To force-run
them in CI, set `CI=true`.

```bash
python -m pytest tests/test_poster_renderer.py -v
```
