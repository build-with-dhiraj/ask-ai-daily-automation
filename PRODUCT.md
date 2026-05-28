# Product

## Register

product

## Users

Internal stakeholders at PhysicsWallah who read the `#ask-ai-evals` Slack channel each morning to assess Ask AI quality and operational health. Most engagement happens on mobile, on the phone, in the first few minutes after the daily message lands. Time budget per reader ranges from 3 seconds (leadership glance) to 30 seconds (engineering deep-read).

Named roster (captured via Slack MCP, 2026-05-28):

| Slack tag | User ID | Email | Team | Title |
|---|---|---|---|---|
| @Naresh Saini | `U03P01CHELQ` | naresh.saini@pw.live | Data Science | Data Scientist |
| @Deepesh kumawat | `U091F0LPG7Q` | deepesh.kumawat@pw.live | Data Science | (not set) |
| @Ankita Agarwal | `U05D4FS3HB2` | ankita.agarwal@pw.live | Backend | Backend, Internal Tools & Automation |
| @Pankaj Bohra | `U085FBH4Q8Y` | pankaj_consultant@pw.live | Frontend Android | (not set) |
| @Tarun Raghav | `U03NCBHSUAZ` | tarun.1@pw.live | Frontend Android | SDE-2, Android, ITA |
| @Vishal Mudgal | `U039CQ75QGY` | vishal.mudgal@pw.live | Frontend Web | SDE2, Frontend, Internal tools and automation |
| @Prince Suman | `U05G8P8CGTH` | prince.suman@pw.live | QA | SDET1, QA/Automation, Internal tools and automation |

Plus the orchestrator (Dhiraj) as the human-in-loop owner / fallback recipient for any unrouted breach.

### Job to be done

When a stakeholder opens the daily message on mobile in the morning, they need one thing: **what is the top risk on the eval pipeline right now?**

Not "what changed?", not "what's the score?" — the top risk to act on. Even on quiet days, the message answers "no urgent risks today, here is what we are watching." The poster and the first sentence of the text companion both serve this question.

### Axis-to-owner mapping (for breach @-mentions)

When a metric crosses a breach threshold, the message prepends targeted Slack `@<owner>` tokens. The mapping:

| Breach signal | Owners pinged |
|---|---|
| Academic FAIL rate | Naresh + Deepesh (DS) |
| Experience FAIL (Intent / Presentation / Pedagogy / Tone-Feel) | Naresh + Deepesh (DS) |
| Downvote rate spike | Naresh + Deepesh (DS) |
| Multi-turn burst / Rephrase rate spike | Naresh + Deepesh (DS) |
| Video Co-Pilot (VCP) API success rate | Ankita (Backend) |
| Cost / Latency / TTFT spike | Ankita (Backend) |
| Langfuse error rate spike | Ankita (Backend) |
| Free-text "UI bugs" / "App bugs" spike | Pankaj + Tarun (Android) AND Vishal (Web), triage internally |
| Test / regression failure (CI red on daily-automation workflow) | Prince (QA) |
| Wildcard / unrouted | Dhiraj (orchestrator) |

All doubt sources (lecture OTT, Pi Lens, etc.) are available on both web and Android, so platform-specific routing keys off the free-text feedback signal, not off the rubric axis itself.

## Product Purpose

The Ask AI Daily Automation pipeline posts two messages to `#ask-ai-evals` each working day: a Rubric Scoreboard (from `daily_eval.py`) and a Daily Digest (from `daily_digest.py`). Each is one Slack Block Kit message containing a rendered PNG poster (the visual at-a-glance), a searchable text companion below the image (numbers, narrative, links), and an organic-thread surface (humans reply to start threads, the bot does not post threads itself).

Success looks like: stakeholders open the channel, scan the poster in under 5 seconds, walk away correctly informed on the top risk, and on the days that matter, click into the deep-dive archive for context. Engagement is the lagging signal: emoji reactions from at least two distinct personas per day, recall test passing at 24 hours, no notification-fatigue complaints.

The pipeline is not just a notification surface; it is an asynchronous review ritual. Habit formation is the lever for engagement, and ritual depends on a stable wrapper with earned variation in the content.

## Brand Personality

**Simple, concise, conversational, precise.**

Voice anchor: write like a senior engineer briefing peers over Slack. Numbers wrapped in prose, not the other way around. Plain English. Acronyms expanded on first use in any given message. No marketing hype. No editorializing. Honest about quiet days. Direct about breach days.

Voice references (used as few-shot anchors in the LLM follow-up generator prompt):

- FiveThirtyEight style: "The judge agreement rate climbed to 84% this week, up from 79% a week ago and the highest reading since the panel was re-calibrated in March."
- Stratechery style: "The interesting thing about today's run is not the headline accuracy number, which is fine, but the shape of the failures."
- Construction Physics style: "Throughput on the eval pipeline this week was about 1,420 traces per day, roughly 3.2x what we managed in the first week of April."

Pulled from the deep-research synthesis (Tufte / Few / Cairo / NN/G / Duhigg / Fogg / Eyal / Slack accessibility / Stratechery / FiveThirtyEight / Construction Physics / Notion + Staffbase internal-comms research).

## Anti-references

Per the impeccable absolute bans and the slop list, the surface must NOT look like:

1. Side-stripe borders (3px colored rail on rows or cards). impeccable: "most recognizable tell of AI-generated UIs."
2. Identical card grids (4 to 6 same-shape cards repeated).
3. Hero-metric template (big number + small label + supporting stat). impeccable: "SaaS cliché."
4. Sparklines as decoration (tiny charts with no informational weight).
5. Inter as the primary face. impeccable: "overused fonts."
6. Geist as the primary face. Now AI-default-adjacent.
7. Generic drop shadows on rounded rectangles. impeccable: "safest, most forgettable combination."
8. Centered text everywhere. impeccable: F-pattern reading requires left-alignment.
9. Glassmorphism, purple/cyan gradients, dark-mode-with-glowing-accents, gradient text.
10. Jargon: "axial", "pp" without expansion, "p50 / p90 / p95" without "percentile", "WoW" raw, "TTFT / VCP / CSAT" raw on first occurrence.
11. Em-dashes anywhere, including inside the rendered PNG. Universal rule.

## Design Principles

1. **Compression preserves strategic value.** Every word and every visual element earns its place. If a stakeholder needs to ask "what does that mean?", we failed.
2. **Risk-first verdict above all else.** The poster headline and the text companion's first sentence answer "what is the top risk on the pipeline right now?" Numbers are evidence for that answer, not the answer itself.
3. **Plain English, day-to-day talking language.** Humanizer-passed. Acronyms expanded on first use. Numbers wrapped in prose, not the other way around. No marketing hype.
4. **Habit formation through stable skeleton.** Same poster layout every day. Same opening sentence shape. Variation only in the content. Reader's eye learns the grid in week one.
5. **Mobile-first, phone-on-lock-screen.** 640px native poster renders ~320pt on Slack mobile. Body type at least 14pt, headline at least 18pt. The first 60 characters of the text companion are the lock-screen preview and must carry the verdict.
6. **Separate exploration from explanation** (per Knaflic). The poster ships the conclusion. The deep-dive archive (GitHub Pages) is for exploration. Threads happen when humans need to ask follow-up questions.

## Accessibility & Inclusion

- WCAG AA minimum. The kill-switch breach band must hit AA at mobile half-size (Söhne Semibold at 12px on cream background, brick-red text, contrast verified ≥4.5:1).
- Every numeric value in the poster must also be reachable via Slack image alt-text, the text companion below the image, and the deep-dive archive page on GitHub Pages.
- Screen reader path: Slack image alt-text contains the headline plus the key numbers as a single sentence so VoiceOver / TalkBack reads a coherent summary in under 10 seconds.
- No content that conveys information through color alone. Breach state is always carried by typography, label, AND color, not just color.
- No content that relies on hover or interactive states. Slack posts are static.
