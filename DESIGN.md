# Design

## Theme

Editorial-precision print register. The poster reads closer to a printed standings sheet or an FT-pink-paper morning brief than a SaaS dashboard or an observability tool. Warm paper neutrals, restrained committed palette, distinctive non-Inter typography, generous whitespace, no decorative chrome.

Physical scene sentence (per impeccable's theme law): a PhysicsWallah engineer or DS lead glancing at the channel on their phone at 9am IST, on Wi-Fi over coffee, looking for one signal: is yesterday's eval pipeline normal or do I need to act. The surface answers that in under 5 seconds and gets out of the way.

Light theme only. Dark mode is not supported. Slack mobile and desktop both render light backgrounds against the channel's dark theme as paper-on-table, which is the intended effect.

## Color Palette

Strategy: **Restrained** (per impeccable's commitment axis), drifting **Committed** only on breach days. Tinted neutrals plus a single saturated accent (brick-red) reserved exclusively for safety-floor breach states. The "one accent at or below 10% of surface" rule holds on quiet days. On breach days the brick-red can climb to about 12% via the brick-red row stripe and brick-red delta cell inside the standings table. There is no longer a dedicated breach band on top of that.

### Tokens (OKLCH, ASCII hyphen only)

```
bg-paper:        oklch(0.97 0.005 85)    /* cream, NOT #fafafa SaaS default */
bg-paper-deep:   oklch(0.94 0.008 85)    /* slightly deeper cream for inset blocks */
ink-body:        oklch(0.22 0.01 240)    /* off-black, slight cool bias, NOT #000 */
ink-muted:       oklch(0.50 0.01 240)    /* secondary type, eyebrows */
ink-faint:       oklch(0.65 0.01 240)    /* tertiary, footers */
ok-green:        oklch(0.62 0.13 145)    /* muted dataviz green for "normal" */
warn-amber:      oklch(0.72 0.15 75)     /* near-breach indicator (reserved, may not appear in v1) */
breach-brick:    oklch(0.55 0.20 28)     /* saturated brick-red, breach ONLY */
breach-brick-bg: oklch(0.95 0.05 28)     /* tinted band background, breach ONLY */
rule:            oklch(0.88 0.006 85)    /* hairline rule on cream */
rule-strong:     oklch(0.22 0.01 240)    /* the 2px black rule under masthead */
```

Category-reflex check passes: cream paper plus brick breach does NOT look like Datadog (dark electric blue), does NOT look like an AI tool (cream + Inter + purple), does NOT look like a generic SaaS dashboard (white + blue gradient).

Never use `#000` or `#fff`. Never use generic Tailwind palette names like `bg-slate-50` or `text-red-500` in templates. Go through the OKLCH tokens.

## Typography

### Faces

Body: **Hanken Grotesk** (Alfredo Marco Pradil, SIL OFL). Open source, distinctive Grotesque proportions, not Inter or Geist.

Tabular numerals: **IBM Plex Mono** (IBM, SIL OFL). Open source, wide tnum support, distinct from Berkeley/JetBrains so the surface does not read as a generic dev tool.

Both must be self-hosted as WOFF2 in `static/fonts/`. No Google Fonts CDN dependency at headless-Chromium render time, per the C1.2b ops note about runner firewalls. No Tailwind CDN at render time either: poster `<style>` blocks are inline.

This typography pairing was validated by Stitch in Phase 1 of the redesign cycle and locked in Phase 3 by the user.

### CSS fallback stacks

```css
font-family-body: "Hanken Grotesk", ui-sans-serif, "Helvetica Neue", Arial, sans-serif;
font-family-mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
```

Inter is **banned** as the primary face (impeccable/slop: "overused fonts"). Geist is **banned** as the primary face (AI-default-adjacent per Design Specialist). Söhne and Berkeley Mono are not in repo (licensed commercial faces) and would require a separate licensing path before adoption.

### Scale

Hierarchy through scale + weight contrast (≥1.25 ratio between steps, per impeccable):

| Role | Size at 640px native | Weight | Face |
|---|---|---|---|
| Display headline (verdict sentence) | 26px | Hanken 700 | Body |
| Section label / eyebrow | 11px, all caps, letter-spacing 0.12em | IBM Plex Mono 500 | Mono |
| Body | 14px | Hanken 400 | Body |
| Body emphasis | 14px | Hanken 600 | Body |
| Standings table value | 14px | IBM Plex Mono 500 | Mono |
| Standings table delta | 14px | IBM Plex Mono 500 | Mono |
| Footer | 11px | IBM Plex Mono 500 uppercase | Mono |
| Date / sample chip | 11px | IBM Plex Mono 500 uppercase | Mono |

`font-feature-settings: "tnum", "zero"` on all mono blocks for tabular-num alignment. Critical for the standings table: yesterday and 14d median columns must align to the decimal point.

Body line length capped at 65 to 75 characters. Inside a 640px poster column, body wraps before reaching the right margin to enforce this.

## Spacing

Base unit: 4px. Scale: 4 / 8 / 12 / 16 / 20 / 28 / 40 / 56.

Vary spacing for rhythm per impeccable. Section gaps are wider than row gaps which are wider than line gaps. Same padding on every container is monotony.

Horizontal poster padding: 28px (px-7 in Tailwind terms).

## Components

The poster is composed from these named components. No others. New components require an update to this file before they appear in templates.

### `Masthead`
Date chip (left), sample size chip (right), 2px black rule below. Roles: pure identity, no metrics. Sits at the top of every poster.

### Breach signal (no dedicated band)
Removed in Iteration 3 (Commit 11). Previously a `KillSwitchBand` carried an all-caps label. The band was insider jargon for a Slack channel that includes leadership readers, so it has been dropped. Breach state is now carried by exactly two channels: (1) the verdict sentence at the top of the poster (e.g. "Top risk: safety floor breached..."), and (2) the brick-red row stripe + brick-red delta cell on the breached metric inside the standings table. The reader sees the breach in plain English first, then sees the offending row.

### `Verdict`
Display headline. 26px Hanken Grotesk 700. One English sentence answering "what is the top risk?". Always left-aligned, never centered. Maximum 3 lines on a 640px poster. Sentence ends with a period.

### `StandingsTable`
The primary content block for Variant D. 5 rows × 4 columns: metric name (Hanken regular, uppercase, letter-spaced 0.06em) | yesterday (IBM Plex Mono 500, right-aligned, tnum) | 14d median (IBM Plex Mono 400 muted, right-aligned, tnum) | delta (IBM Plex Mono 500, right-aligned, tnum, color-coded only on breach). Hairline rule between rows. NO side-stripe borders. NO row rails. Header row in IBM Plex Mono 11px uppercase, letter-spaced 0.12em.

### `TopDriverList`
Used on the scoreboard surface (Variant D) directly below the standings table. List of top-3 driver codes by fire count: mono code prefix (e.g. `A5`) | Hanken 400 label | 1px black bar on 1px rule track | IBM Plex Mono 500 count. Bar width is proportional to the count of the top row, not to a percentage of total. Empty list renders nothing. Extracted to `templates/_top_drivers.html.j2` so the partial can be reused if a future surface needs the same affordance.

### `Callout`
Used on the scoreboard surface. Two-line block: mono uppercase eyebrow label (IBM Plex Mono 11px letter-spaced 0.12em, e.g. "WHY IT MATTERS" or "WORTH WATCHING") on line one, Hanken 400 13-14px body sentence on line two. 2-line maximum on the body. No card container, no rounded corners, no drop shadow. Hairline rule between callouts. Extracted to `templates/_callout.html.j2`.

### `InsightCard`
Used on the digest surface as the body content. Schema: `{topic_label, icon, claim, evidence, context}`. Topic label is one of CLARITY, LATENCY, FEEDBACK, COST, ACCURACY, USAGE (mono uppercase, letter-spaced 0.12em). Icon is a single semantic glyph from the locked set or empty. Claim in Hanken 600 14px. Evidence in Hanken 400 14px ink-muted. Optional context in Hanken 400 12-14px ink-faint. Hairline rule between cards. NO card container, NO rounded corners; the stack of hairlines does the visual separation work. Extracted to `templates/_insight_card.html.j2`.

### `Sparkline`
Not rendered in Variant D, but the data layer is wired (`spark_series` populated from 14-day rolling history) so a future variant can use it without a data migration. If rendered: inline SVG. 1.25px stroke `ink-body`, 6%-alpha area fill `ink-body`, 2px terminal dot at the latest point. Adequate space mandatory (not a tiny 96×20 decoration). Caption in eyebrow style.

### `Footer`
Two-part footer: brand mark on the left ("ASK AI, DAILY EVAL" or "ASK AI, DAILY DIGEST") in IBM Plex Mono 11px uppercase letter-spaced 0.12em. ISO date on the right, IBM Plex Mono 10px in `ink-faint`. No link chrome inside the rendered PNG: links live in the Slack Block Kit message body, not the image.

## Layout

640px native poster width. Height grows organically per content. Mobile renders at ~320pt (Slack mobile constraint). All components must remain legible at half size; that is the binding constraint, not the native size.

Vertical flow per surface (top to bottom):

- Scoreboard: Masthead, 2px rule, Verdict, 1px rule, Standings table (5 rows), 1px rule, Top Driver Codes (3 codes), 1px rule, 2 Callout blocks, Footer.
- Digest: Masthead, 2px rule, Verdict, 1px rule, 4 Insight cards (vertical stack with hairline rules between), Footer.

There is no longer a conditional band between the masthead and the verdict; the verdict sentence itself carries breach state.

Horizontal alignment: left for all body content. Right for metadata chips and ISO dates. Never center body type.

Cards: avoided unless genuinely the best affordance. Nested cards are always banned. Most regions are separated by spacing and rules, not card containers.

## Motion

The poster is a static PNG. No motion at render time. No CSS transitions. No animation properties. The Playwright screenshot is taken at `wait_until="networkidle"` to ensure all fonts are loaded and laid out before capture.

If a future surface needs motion (e.g. an HTML deep-dive archive page on GitHub Pages), use exponential easing only (ease-out-quart / quint / expo). No bounce. No elastic. No animating layout properties; transform and opacity only.

## Forbidden patterns (cite impeccable/slop)

Codified as CI lints in `tests/test_no_anti_patterns.py`:

- `border-left: [3-9]px` or `border-right: [3-9]px` with a colored value → side-stripe border ban
- `background-clip: text` combined with gradient → gradient text ban
- More than 3 cards with same shape in a single section → identical card grid ban
- `font-family:` containing only "Inter" or only "Geist" → primary-face ban
- `box-shadow: 0 1px` or `box-shadow: 0 2px` on a rounded rectangle → generic drop shadow ban
- Any em-dash (U+2014) or en-dash (U+2013) character in templates or message text. Universal rule.

## File integration

This file lives at `/Users/pw/ask-ai-daily-automation/DESIGN.md`. Loaded automatically by `node .claude/skills/impeccable/scripts/load-context.mjs` at the start of every impeccable command. Templates in `templates/` consume its tokens via Tailwind's `theme.extend.colors` or via inline CSS variable definitions in the poster's `<style>` block. Both are valid.

When Phase 1 of the redesign plan generates Stitch variants, this DESIGN.md uploads to Stitch as the design-system input (via `mcp__stitch__upload_design_md`). The Stitch variants are then constrained by these tokens.
