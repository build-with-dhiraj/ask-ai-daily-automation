# Self-hosted fonts for poster render

Self-hosted as WOFF2 so the headless Chromium render in `scripts/poster_renderer.py` does NOT depend on Google Fonts CDN at render time. The CI runner firewall and the dogfood C1.2b ops note both treat any outbound dependency at screenshot time as a render-failure risk.

## Faces

| File | Family | Weight | License | Source |
|---|---|---|---|---|
| `hanken-grotesk-variable.woff2` | Hanken Grotesk | 100 to 900 variable | SIL Open Font License 1.1 | https://fonts.google.com/specimen/Hanken+Grotesk |
| `plex-mono-400.woff2` | IBM Plex Mono | 400 | SIL OFL 1.1 | https://fonts.google.com/specimen/IBM+Plex+Mono |
| `plex-mono-500.woff2` | IBM Plex Mono | 500 | SIL OFL 1.1 | same |
| `plex-mono-600.woff2` | IBM Plex Mono | 600 | SIL OFL 1.1 | same |

Hanken Grotesk is a single variable-axis WOFF2 covering 100 to 900. The template `@font-face` rules declare weights 400, 500, 600, 700 all pointing at this single file via `font-weight: 400 700` range syntax; the browser interpolates.

IBM Plex Mono ships per-weight statics (not variable), so we keep the three weights we actually use.

## Why these two

Locked in DESIGN.md after Phase 1 Stitch rendered six variants and the user picked Variant D in Phase 3.

- Hanken Grotesk: open source, distinct Grotesque proportions, NOT Inter, NOT Geist. Both Inter and Geist are banned as primary faces per the impeccable anti-pattern list (overused + AI-default-adjacent).
- IBM Plex Mono: open source, full tnum support, distinct from the Berkeley Mono / JetBrains Mono pair that would push the surface into a generic dev-tool aesthetic.

Both pairings validated by the Phase 1 Stitch render.

## License compliance

Both faces are SIL Open Font License 1.1, which permits redistribution including in commercial projects, provided the OFL terms are preserved. The full OFL text lives at `OFL.txt` in this directory.

## How to refresh

If a future version of either face is released:

```bash
# Hanken Grotesk variable
curl -fsSL -o static/fonts/hanken-grotesk-variable.woff2 \
  "$(curl -fsSL -H 'User-Agent: Mozilla/5.0' \
    'https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400..700&display=swap' \
    | grep -oE 'https://[^)]+\.woff2' | head -1)"

# IBM Plex Mono (per-weight, do all three)
for w in 400 500 600; do
  url="$(curl -fsSL -H 'User-Agent: Mozilla/5.0' \
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@${w}&display=swap" \
    | grep -oE 'https://[^)]+\.woff2' | head -1)"
  curl -fsSL -o "static/fonts/plex-mono-${w}.woff2" "$url"
done
```

Then re-render a sample and visually diff against `docs/design/variant-d-reference/`.
