# Ubongo Funder Monitor — Implementation Plan

**Goal:** Scrape all 780 funders from the Segal directory, filter to those tagged **"Quality
Education"** (Ubongo's sector, UN SDG 4) who also work in Africa, then crawl those weekly to
detect new funding opportunities and deliver email digests to the Ubongo development team via
alerts@ubongo.org.

**Stack:** Python · Playwright · PostgreSQL · Claude API · AWS SES · AWS EC2

---

## Progress Tracker

| Phase | Description | Status |
|---|---|---|
| 0 | Project setup (repo, env, deps, DB models) | ✅ Done |
| 1 | Segal directory scraper | ✅ Done — 780 scraped, filtering verified |
| 2 | Database schema | ✅ Done — 4 tables defined in SQLAlchemy |
| 3 | Weekly site crawler | ✅ Done — Playwright async crawler, link discovery, path probing |
| 4 | Change detection + Claude AI filter | ✅ Done — SHA-256 diff + haiku/sonnet escalation |
| 5 | Email digest (AWS SES) | ✅ Done — HTML digest + all-clear email via boto3 |
| 6 | AWS EC2 deployment + cron | ✅ Done — EC2 live, DB seeded (116 active funders), cron installed, SES verified |
| 7 | Monitoring & maintenance | 🔲 Pending |

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Phase 0 — Project Setup](#phase-0--project-setup)
3. [Phase 1 — Scrape the Funder Directory](#phase-1--scrape-the-funder-directory)
4. [Phase 2 — Database Design](#phase-2--database-design)
5. [Phase 3 — Weekly Crawler](#phase-3--weekly-crawler)
6. [Phase 4 — Opportunity Detection (AI Layer)](#phase-4--opportunity-detection-ai-layer)
7. [Phase 5 — Email Notifications](#phase-5--email-notifications)
8. [Phase 6 — AWS Deployment](#phase-6--aws-deployment)
9. [Phase 7 — Monitoring & Maintenance](#phase-7--monitoring--maintenance)
10. [File Structure](#file-structure)
11. [Cost Estimate](#cost-estimate)
12. [Decisions Log](#decisions-log)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    AWS EC2 (t3.small)                    │
│                                                          │
│  ┌──────────────┐    ┌───────────────┐    ┌──────────┐  │
│  │  Scheduler   │───▶│    Crawler    │───▶│    DB    │  │
│  │  (cron,      │    │  (Playwright) │    │(Postgres)│  │
│  │  weekly)     │    │               │    │          │  │
│  └──────────────┘    └───────┬───────┘    └──────────┘  │
│                              │                           │
│                              ▼                           │
│                    ┌─────────────────┐                   │
│                    │  Change Detector│                   │
│                    │  (diff engine)  │                   │
│                    └────────┬────────┘                   │
│                             │ changed content            │
│                             ▼                            │
│                    ┌─────────────────┐                   │
│                    │  Claude API     │                   │
│                    │  (opportunity   │                   │
│                    │   filter + sum) │                   │
│                    └────────┬────────┘                   │
│                             │ confirmed opportunities    │
│                             ▼                            │
│                    ┌─────────────────┐                   │
│                    │  Email Digest   │──▶ AWS SES        │
│                    │  (weekly)       │    (→ team inbox) │
│                    └─────────────────┘                   │
└─────────────────────────────────────────────────────────┘

                  Phase 1 (one-time)
┌──────────────────────────────────────────┐
│  segalfamilyfoundation.org/funder-       │
│  directory → Playwright scraper →        │
│  filter education funders → Postgres     │
└──────────────────────────────────────────┘
```

**Key design decisions:**
- **EC2 over Lambda** — crawling 350 sites can take 1–2 hrs; Lambda's 15-min limit rules it out
- **Playwright over requests** — the funder directory and many funder sites are JS-rendered
- **Claude API for filtering** — keyword matching alone produces too many false positives; AI
  reads the page and decides if it's a real, actionable opportunity
- **PostgreSQL** — we need to store snapshots of page content to diff against next week
- **AWS SES** — team is already on AWS; cheap and reliable for transactional email

---

## Phase 0 — Project Setup ✅

### 0.1 Repository structure

```
ubongo-funder-monitor/
├── scraper/
│   ├── directory_scraper.py   # One-time: scrape the Segal funder directory
│   └── site_crawler.py        # Weekly: crawl each funder's website
├── detector/
│   ├── change_detector.py     # Compare new snapshot vs stored snapshot
│   └── opportunity_filter.py  # Claude API call to classify + summarize
├── notifier/
│   └── email_notifier.py      # Build and send HTML email digest via SES
├── db/
│   ├── models.py              # SQLAlchemy ORM models
│   └── init_db.py             # Create tables on first run
├── scheduler/
│   └── weekly_run.py          # Entry point for the weekly job
├── config.py                  # Loads env vars
├── requirements.txt
├── .env.example
├── Dockerfile
└── deploy/
    ├── setup.sh               # EC2 bootstrap script
    └── crontab.txt            # Cron schedule
```

### 0.2 Python dependencies

```
playwright==1.44.*
beautifulsoup4==4.12.*
sqlalchemy==2.0.*
psycopg2-binary==2.9.*
anthropic==0.28.*         # Claude API
boto3==1.34.*             # AWS SES
python-dotenv==1.0.*
tenacity==8.3.*           # retry logic for flaky sites
```

### 0.3 Environment variables (`.env`)

```
DATABASE_URL=postgresql://user:pass@localhost:5432/funder_monitor
ANTHROPIC_API_KEY=sk-ant-...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
SES_SENDER_EMAIL=alerts@ubongo.org
# During testing: use your personal email. In production: switch to ubongo.org addresses.
TEAM_EMAILS=your-personal@email.com
```

---

## Phase 1 — Scrape the Funder Directory ✅

**Goal:** Scrape all 780 funders from `segalfamilyfoundation.org/tools/funder-directory/`,
filter down to education + Africa-focused ones, and store them in the database.

**Why Playwright here:** The directory uses Ninja Table, a JS-rendered plugin. Data is not
in the raw HTML — it loads after the page renders.

### 1.1 Strategy

1. Launch Playwright (headless Chromium)
2. Navigate to the directory page
3. Wait for the table to fully render
4. Scrape **all 780 rows** across all pages (pagination handled by clicking through)
5. For each row, extract:
   - Funder name
   - Website URL
   - Focus area(s)
   - Geographic focus
6. Apply filtering logic (see 1.2) in-memory
7. Write ~350 filtered results to the `funders` table

> **Note:** The Segal directory is not updated regularly. Re-run this script manually
> only when you want to refresh the funder list (e.g. once or twice a year).

### 1.2 Filtering logic

Keep a funder if **both** conditions are met:
- Sector/focus area exactly matches **"Quality Education"** (Ubongo's focus area, UN SDG 4)
- Geography contains Africa or any African sub-region / country name

Funders that pass only one condition are stored with `is_active = false` so they're
preserved but not crawled — easy to re-enable manually.

> Both filter lists live in `config.py` (`EDUCATION_SECTORS`, `AFRICA_KEYWORDS`) so they
> can be tuned without touching the scraper code.

### 1.3 Output

**Verified on first dry run (2026-03-02):**
- Total scraped: 780 funders across 39 pages (20 rows/page)
- Sector label format confirmed: `04-Quality Education` (SDG number prefix)
- Substring match `"quality education"` correctly catches this format
- Column mapping confirmed: `name:0, website_url:1, geography:2, focus_areas:3`
- Exact active funder count determined at runtime

---

## Phase 2 — Database Design ✅

### Tables

#### `funders`
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `name` | TEXT | Funder organization name |
| `website_url` | TEXT | The URL to crawl weekly |
| `focus_areas` | TEXT[] | e.g. `["Education", "Health"]` |
| `geography` | TEXT[] | e.g. `["East Africa", "West Africa"]` |
| `is_active` | BOOLEAN | False = skip this funder (e.g. site is down) |
| `created_at` | TIMESTAMP | |
| `last_checked_at` | TIMESTAMP | Last time crawler visited |

#### `page_snapshots`
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `funder_id` | INT FK | → funders.id |
| `crawled_at` | TIMESTAMP | When this snapshot was taken |
| `url` | TEXT | Exact URL crawled (may differ from root) |
| `content_hash` | TEXT | SHA-256 of extracted text (fast change check) |
| `content_text` | TEXT | Extracted plain text (for AI analysis) |
| `status` | TEXT | `ok`, `error`, `blocked` |
| `error_message` | TEXT | If status = error |

#### `opportunities`
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `funder_id` | INT FK | → funders.id |
| `snapshot_id` | INT FK | → page_snapshots.id |
| `detected_at` | TIMESTAMP | |
| `title` | TEXT | AI-generated title |
| `summary` | TEXT | AI-generated 2–3 sentence summary |
| `deadline` | DATE | If mentioned; else NULL |
| `source_url` | TEXT | Direct URL to the opportunity |
| `notified` | BOOLEAN | Has this been included in an email yet |

#### `notification_log`
| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `sent_at` | TIMESTAMP | |
| `recipient_emails` | TEXT[] | |
| `opportunity_ids` | INT[] | Which opportunities were in this email |

---

## Phase 3 — Weekly Crawler

**Goal:** Visit every active funder URL, extract text content, detect changes.

### 3.1 Crawl strategy

For each funder in the `funders` table where `is_active = true`:

1. Launch Playwright (headless Chromium)
2. Navigate to the funder's website
3. Wait for full page load (`networkidle`)
4. **Smart page targeting:** Don't just crawl the homepage. Also check common paths:
   - `/grants`, `/funding`, `/opportunities`, `/apply`, `/open-calls`, `/rfp`
   - If any of these return 200, crawl them instead of / in addition to homepage
5. Extract visible text (strip nav, footer, cookie banners)
6. Compute SHA-256 hash of extracted text
7. Compare hash against the most recent snapshot in the DB
8. If hash changed → store new snapshot and flag for AI analysis
9. If unchanged → update `last_checked_at`, skip AI call (saves cost)
10. Handle errors gracefully: timeout, 403, DNS failure → log to `page_snapshots.status`

### 3.2 Parallelism

- Run crawls in **batches of 10** concurrently (respects most sites' rate limits)
- Add a 1–3 second random delay between requests to a single domain
- Use `tenacity` for retries (max 3 attempts, exponential backoff)

### 3.3 Estimated timing

- 350 sites × avg 5 seconds per site = ~30 minutes single-threaded
- With 10 parallel workers → ~5–7 minutes per weekly run

---

## Phase 4 — Opportunity Detection (AI Layer)

**Goal:** For each changed snapshot, ask Claude to determine if there's a real funding
opportunity and summarize it.

### 4.1 Claude prompt design

```
System:
You are an expert at identifying funding opportunities for African education nonprofits.
You will be given the text content of a funder's website page.

User:
Funder: {funder_name}
URL: {url}

Page content:
---
{content_text}
---

Answer ONLY with valid JSON:
{
  "is_opportunity": true | false,
  "confidence": "high" | "medium" | "low",
  "title": "short title of the opportunity or null",
  "summary": "2-3 sentence summary of what the opportunity is, who can apply, and the deadline if known, or null",
  "deadline": "YYYY-MM-DD or null",
  "direct_url": "URL to the opportunity page if different from the page provided, or null"
}

Return is_opportunity: true ONLY if there is a specific, actionable grant, RFP, or
funding call that is currently open or opening soon. General descriptions of what a
funder funds do NOT count.
```

### 4.2 Model choice

Use `claude-haiku-4-5-20251001` for cost efficiency. It handles this classification task
well, and at ~$0.001 per page analysis, 350 sites/week costs < $0.40/week in AI fees.

Escalate to `claude-sonnet-4-6` only if haiku confidence is "low" (re-analyze ambiguous
cases).

### 4.3 Guardrails

- Only call Claude if the content hash changed (skip unchanged pages)
- Truncate content to 6000 tokens before sending (most pages don't need more)
- If Claude returns `is_opportunity: false` → do nothing
- If `is_opportunity: true` and `confidence: high/medium` → insert into `opportunities`
  table and flag `notified = false`

---

## Phase 5 — Email Notifications

**Goal:** Once per week, send an HTML digest of all new unnotified opportunities.

### 5.1 Email format

```
Subject: [Ubongo] X New Funding Opportunities Found — Week of {date}

Hi team,

The funder monitor found X new opportunities this week:

────────────────────────────────────────
1. USAID Education Innovation Fund
   Funder: USAID
   Deadline: March 31, 2026
   Summary: Open call for organizations delivering foundational literacy programs
            in Sub-Saharan Africa. Grants range from $100K–$500K.
   → View opportunity: https://...

2. ...
────────────────────────────────────────

{N} funders checked. {M} sites had content changes. {X} opportunities identified.

— Ubongo Funder Monitor
```

### 5.2 Implementation

- Build HTML email (with plain text fallback) using Python's `email` library
- Send via **AWS SES** using `boto3`
- After successful send: mark all included opportunities as `notified = true` and
  insert a row in `notification_log`
- If **zero** opportunities found that week: send a brief "all clear" email so the
  team knows the system is running

### 5.3 Send schedule

Every **Monday at 8:00 AM EAT** (East Africa Time = UTC+3), so the team starts the
week with fresh intel.

---

## Phase 6 — AWS Deployment

### 6.1 Infrastructure

| Resource | Spec | Why |
|---|---|---|
| EC2 | t3.small (2 vCPU, 2GB RAM) | Cheapest instance that can run Playwright |
| OS | Ubuntu **24.04** LTS (actual; 22.04 planned) | Stable, well-supported |
| Storage | 20GB gp3 EBS | Plenty for Postgres + app |
| PostgreSQL | Self-hosted on same EC2 (v16 installed) | Simplest; can move to RDS later |
| SES | Standard | Email sending |
| Elastic IP | 1× | Stable IP for SES sending reputation |
| Security Group | Inbound: SSH (22) only | Outbound: all (for crawling) |

### 6.1a EC2 Deployment Status (2026-03-03)

| Step | Status | Notes |
|---|---|---|
| EC2 instance provisioned | ✅ Done | t3.small, Ubuntu 24.04, eu-north-1 |
| System packages installed | ✅ Done | Python 3.12, PostgreSQL 16, git, curl, build-essential |
| PostgreSQL configured | ✅ Done | `funder_user` + `funder_monitor` DB created |
| Repo cloned | ✅ Done | `/opt/funder-monitor` |
| Python venv + pip deps | ✅ Done | All packages installed incl. playwright 1.58, anthropic 0.84 |
| Playwright Chromium | ✅ Done | Browser + system deps installed |
| `.env` configured | ✅ Done | DB URL, API keys, SES credentials set |
| DB schema initialised | ✅ Done | All 4 tables created via `python -m db.init_db` |
| Funder directory scrape | ✅ Done | 780 scraped → 116 active, 318 inactive, 346 skipped |
| Cron job installed | ✅ Done | Sun 23:00 UTC crawl + Mon 05:00 UTC email + Mon 07:00 UTC watchdog |
| SES verified + production | ✅ Done | Gmail sender verified for testing |

### 6.2 EC2 setup (`deploy/setup.sh`)

```bash
# Install system dependencies
apt update && apt install -y python3-pip python3-venv postgresql postgresql-contrib

# Install Playwright system deps
pip install playwright && playwright install chromium && playwright install-deps

# Set up app
git clone <repo> /opt/funder-monitor
cd /opt/funder-monitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Init database
python db/init_db.py

# Run Phase 1 once (scrape funder directory)
python scraper/directory_scraper.py
```

### 6.3 Cron schedule (`deploy/crontab.txt`)

```cron
# Weekly crawl: every Sunday at 11 PM UTC (Monday 2 AM EAT)
0 23 * * 0 cd /opt/funder-monitor && venv/bin/python scheduler/weekly_run.py >> /var/log/funder-monitor.log 2>&1
```

(Crawl Sunday night → process overnight → email ready by Monday morning)

### 6.4 SES setup checklist

- [ ] Verify sender email domain in SES
- [ ] Request production access (move out of sandbox) so you can email any address
- [ ] Add team email addresses
- [ ] Set up a bounce/complaint handling (optional but good practice)

---

## Phase 7 — Monitoring & Maintenance

### 7.1 Logging

- All crawler runs logged to `/var/log/funder-monitor.log`
- Include: sites checked, sites changed, Claude calls made, opportunities found, email sent
- Rotate logs weekly

### 7.2 Health checks

- If the weekly cron job fails silently, you won't know. Add a "watchdog" email:
  if no run completes within 8 days, alert the team.
- Simple approach: a second cron job checks the `notification_log` table; if no entry
  in the past 8 days, send a warning email.

### 7.3 Ongoing maintenance

| Task | Frequency |
|---|---|
| Review funders list (add/remove) | Monthly or when directed |
| Check `is_active = false` funders (sites that keep erroring) | Monthly |
| Review Claude prompt quality (too many false positives/negatives?) | After first 4 weeks |
| Rotate AWS keys | Quarterly |
| OS security patches | Monthly (`apt upgrade`) |

---

## File Structure

```
ubongo-funder-monitor/
├── scraper/
│   ├── directory_scraper.py      # Phase 1: one-time Segal directory scrape
│   └── site_crawler.py           # Phase 3: weekly per-funder crawl
├── detector/
│   ├── change_detector.py        # Hash comparison logic
│   └── opportunity_filter.py     # Claude API integration
├── notifier/
│   └── email_notifier.py         # AWS SES email builder + sender
├── db/
│   ├── models.py                 # SQLAlchemy models (funders, snapshots, etc.)
│   └── init_db.py                # Create tables
├── scheduler/
│   └── weekly_run.py             # Orchestrates the full weekly pipeline
├── config.py                     # Env var loading
├── requirements.txt
├── .env.example
├── Dockerfile                    # Optional: containerize the app
└── deploy/
    ├── setup.sh                  # EC2 bootstrap
    └── crontab.txt               # Cron schedule
```

---

## Cost Estimate

| Item | Monthly Cost |
|---|---|
| EC2 t3.small | ~$17 |
| EBS 20GB gp3 | ~$1.60 |
| Elastic IP | ~$3.60 |
| AWS SES (< 1000 emails/mo) | ~$0.10 |
| Claude API (350 sites/wk, ~50% changed) | ~$1.50 |
| **Total** | **~$24/month** |

---

## Decisions Log

| Decision | Choice | Notes |
|---|---|---|
| Funder source | Segal directory (780 total → ~350 filtered) | Scrape all, filter in-memory |
| From email domain | `ubongo.org` (alerts@ubongo.org) | Verify domain in AWS SES |
| Team emails | Start with personal email for testing, then switch to ubongo.org team | Single env var change |
| Funder list refresh | Manual only | Segal directory rarely updated; re-run script when needed |
| Opportunity history | Email-only, no dashboard | Keep it simple |
| Priority funders | Change-detection only | No always-flag list; tune later if needed |

## Remaining Open Questions

None. All decisions finalised. Ready to build.

---

*Last updated: 2026-03-03 — Phases 0–6 complete. System live on EC2 (eu-north-1). 116 funders being monitored. First automated run: Sunday 2026-03-08 at 23:00 UTC. Phase 7 (monitoring) remaining.*
*Author: Ubongo Development Team + Claude Code*
