# `#ask-ai-evals` — channel onboarding (pin this)

Paste into Slack as the channel description or a pinned message. Tweak names if your webhook posts elsewhere.

---

**Ask AI runs on trust with millions of students. This channel is where we protect that trust — together.**

We don’t use it to point fingers at people. We use it to **see reality early** and **ship fixes**.

---

### Two posts. One mission.

| | **Daily Eval** (~08:30 IST) | **Daily Digest** (~09:30 IST) |
|--|-----------------------------|-------------------------------|
| **Nickname** | The **scorecard** | The **pulse** |
| **Question it answers** | *“Are our answers good enough — factually and as an experience?”* | *“What did students say, and what did the system do?”* |
| **Mental model** | Traffic lights on a **fixed rubric** — stable week over week. | A **stack of signals** — errors first, then voice of the student, then context. |

---

### 1) Daily Eval — the scorecard

- **What it is:** A sample of **real yesterday** conversations, judged the **same way every day** (accuracy + clarity + format + tone + pedagogy).
- **How to read it:** Green is healthy. Yellow/red tells you **where to dig**, not who to blame. A **thumbs-up** can still flag issues — that’s normal; the checklist is stricter than a single tap.
- **What it is not:** A popularity contest or a performance review of individuals.
- **The `?` link:** Definitions, cost, what PASS/NEUTRAL/FAIL mean — **open it when jargon appears.**

---

### 2) Daily Digest — the pulse

**Read top to bottom. The order is deliberate.**

1. **Langfuse errors (24h)** — *Did the machine misbehave?* Spikes here → **engineering / platform first.**
2. **Video co-pilot API health (`stream_logs`, yesterday)** — *Did requests succeed end-to-end?* Complements Langfuse; use **`trace_id`** to connect dots when debugging.
3. **Student comments on downvotes** — *What did they actually say?* Long text = **high intent**. This is **gold** for product and design.
4. **The rest** — Reason mix, yesterday’s downvote snapshot, **silent frustration** proxies (quick follow-ups, “explain again” patterns). Catches pain **without** a downvote.

When **judge + behavior** both flag the same chapter, treat it as a **stronger** signal — not noise.

---

### Who moves first (rough guide)

| Role | Lean in when… |
|------|----------------|
| **Engineering** | Errors, API health, latency, integrations, regressions after release. |
| **Product / Design** | Comments, confusion, friction, wording, flows. |
| **Data science** | Sampling, metric interpretation, deeper slices when something looks off. |
| **QA** | Repro, release correlation, regression checks. |

---

### House rules

- **Thread it.** See something off? Reply in a thread with what you saw — rough notes beat silence.
- **One channel, two beats:** **Eval** = disciplined quality line. **Digest** = reality + system truth.

---

**TL;DR for new joiners:** *Scorecard in the morning. Pulse right after. Errors and API health first, students’ words second, context third. We fix systems — not each other.*
