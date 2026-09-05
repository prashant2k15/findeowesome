# BingLinkFinder

A self-hosted, always-on **backlink opportunity discovery engine**.

It continuously harvests public backlink lists and search footprints, cleans and
de-duplicates everything into one master database, verifies that each URL is
actually alive, works out *what kind of opportunity it is* (directory, profile,
guest post, forum, bookmark…), and exports ready-to-work CSVs.

You never search for link opportunities by hand again — the database keeps
growing 24×7 on a VPS, and every future site you work on reuses it.

```
GitHub list repos ─┐
Search footprints ─┼─► collect ─► normalise ─► de-duplicate ─► live check
Your own imports  ─┘                                              │
                                                                  ▼
                                              classify (what can I submit here?)
                                                                  │
                                                     enrich (is the domain worth it?)
                                                                  │
                                                          master database
                                                           (PostgreSQL)
                                                                  │
                    ┌────────────────┬────────────────┬───────────┴──────┐
                    ▼                ▼                ▼                  ▼
              CSV exports      outreach queue   backlink tracker   Telegram digest
            + web dashboard   (drafts you       (are my links      + lost-link and
                               approve)          still live?)        failure alerts
```

**Discovery, monitoring and assisted outreach.** It finds and qualifies places where links can
legitimately be earned or submitted; it does not auto-post anything, and it
honours `robots.txt` and per-domain rate limits while crawling.

---

## What you get

| | |
|---|---|
| **Zero-touch discovery** | GitHub repo search finds published link lists; 500+ rotating search footprints find the rest |
| **One clean database** | URL normalisation + a unique index means the same site never lands twice, no matter how many lists it appears in |
| **Live verification** | Every URL is fetched, redirects resolved, dead ones retired after a retry budget |
| **Real classification** | Page-level signals ("submit your site", signup forms, URL input fields) decide the type and a 0–1 usefulness score — and the exact submission page is extracted |
| **Runs itself** | APScheduler worker, health rows for every job run, Telegram digest + failure alerts |
| **Dashboard + API** | Live counters, worker health, one-click job triggers, JSON API, CSV downloads |
| **Authority filtering** | Open PageRank (free) or DataForSEO enrichment, so you sort by whether a domain is worth the effort, not just whether a form exists |
| **Link monitoring** | Every link you place is re-verified: still there? still dofollow? Telegram alert the moment one disappears |
| **Outreach with a handbrake** | Finds contacts, drafts personalised mails, then waits for your approval - five independent gates before anything sends |
| **Backups included** | Daily `pg_dump` to `backups/`, 14 kept, one-command restore |

---

## Quick start on a VPS (the intended way to run it)

Tested target: Ubuntu 22.04/24.04, 2 vCPU / 4 GB RAM / 40 GB SSD — enough for
hundreds of thousands of URLs.

```bash
git clone https://github.com/<you>/bing-link-finder.git ~/bing-link-finder
cd ~/bing-link-finder
cp .env.example .env
nano .env                 # set POSTGRES_PASSWORD, GITHUB_TOKEN, Telegram, DASHBOARD_KEY
bash scripts/deploy.sh    # installs Docker if missing, then builds and starts everything
```

That brings up five containers:

| service | what it does |
|---|---|
| `worker` | the 24×7 scheduler running every job |
| `api` | dashboard + JSON API on `:8000` |
| `db` | PostgreSQL (data lives in a named volume, survives rebuilds) |
| `searxng` | self-hosted metasearch so footprint queries cost nothing and need no API key |
| `backup` | daily compressed `pg_dump` into `./backups` |

Then open `http://<server-ip>:8000` (add `?key=…` if you set `DASHBOARD_KEY`).

Seed the database immediately instead of waiting for the first scheduled run:

```bash
docker compose exec worker python -m app.cli discover   # find GitHub link lists
docker compose exec worker python -m app.cli harvest    # pull URLs out of them
docker compose exec worker python -m app.cli footprints # run search footprints
docker compose exec worker python -m app.cli check      # verify + classify
docker compose exec worker python -m app.cli stats
```

A single `discover` + `harvest` pass on a fresh install typically registers
~75 list repos and lands **4,000+ unique URLs** before any search query runs.

### Local development (no Docker)

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows
echo "DATABASE_URL=sqlite:///./data/blf.db" > .env
python -m app.cli init
python -m app.cli discover && python -m app.cli harvest
python -m app.cli check --limit 50
uvicorn app.api.main:app --reload      # dashboard at http://localhost:8000
pytest -q
```

---

## The schedule

The worker is one process; jobs never overlap and a missed run is coalesced, so
a restart or a slow batch can never pile work up.

| job | default cadence | what it does |
|---|---|---|
| `github_discover` | 24 h | GitHub repo search → register new seed lists |
| `github_harvest` | 6 h | fetch each seed list, extract URLs (skips unchanged lists by hash) |
| `footprints` | 6 h | run the next 25 least-recently-used search footprints |
| `import` | 15 min | ingest anything dropped into `imports/` |
| `check` | 1 h | verify + classify the next batch (500 URLs by default) |
| `export` | 6 h | regenerate CSVs in `exports/` |
| `cleanup` | daily 04:30 | purge URLs that failed repeatedly |
| `metrics` | 6 h | fetch domain authority for live domains |
| `verify_backlinks` | 12 h | re-check the links you placed |
| `build_prospects` | 12 h | promote the best opportunities into outreach |
| `find_contacts` | 3 h | look up an email for new prospects |
| `draft_outreach` | 6 h | write first mails and due follow-ups |
| `send_outreach` | 2 h | send approved drafts (no-op while outreach is disabled) |
| `report` | daily 09:00 | Telegram digest |

Every cadence is an env var (`JOB_*` in `.env`) — no code change needed.

---

## Configuration

Everything lives in `.env` (see `.env.example`). The ones that matter:

```ini
SEARCH_PROVIDER=searxng      # searxng (free, self-hosted) | serper | brave | none
GITHUB_TOKEN=               # optional; lifts GitHub API limit 60/h -> 5000/h
TELEGRAM_BOT_TOKEN=         # optional; digest + failure alerts
DASHBOARD_KEY=              # optional; require ?key=... on the dashboard
PER_DOMAIN_DELAY=2.0        # politeness: seconds between hits on one host
RESPECT_ROBOTS_TXT=true
CHECKER_CONCURRENCY=16      # raise on a bigger box; it is I/O bound
```

**Search backends.** SearXNG ships in the compose file and costs nothing, but it
is a metasearch layer — engines throttle it, so expect modest result counts.
For serious footprint volume, set `SEARCH_PROVIDER=serper` and add a
`SERPER_API_KEY` (real Google SERPs), or `brave` with a `BRAVE_API_KEY`
(2k free queries/month). The provider is one interface —
[`app/search/base.py`](app/search/base.py) — so adding another is ~30 lines.

**Footprints.** [`config/footprints.json`](config/footprints.json) holds query
templates expanded across modifier lists — the shipped set produces **501
queries** across directories, profiles, guest posting, forums, Q&A, bookmarking
and web 2.0, including country-specific directory hunts. Edit it and run
`blf sync-config`; new queries join the rotation, existing stats are preserved.

**Seed lists.** [`config/seed_sources.yaml`](config/seed_sources.yaml) is the
hand-curated starting set — any raw text/markdown/CSV URL works. `github_discover`
appends to it automatically, and a source that 404s is disabled after one try.

**Your own lists.** Drop `.txt` / `.csv` / `.md` files into `imports/`; within
15 minutes every URL is normalised, de-duplicated against the database and
queued for checking. The file is renamed `*.done` so it is never read twice.

---

## Using the database

**Dashboard** (`/`) — counters, live-by-type with per-type CSV downloads, worker
health, top-scoring opportunities with their submission pages, and a button per
job to run it now.

**CLI**

```bash
python -m app.cli stats                       # database + worker health
python -m app.cli list --kind directory       # top opportunities
python -m app.cli check --limit 200           # one checker batch now
python -m app.cli import-url <raw-list-url>   # pull in someone else's list
python -m app.cli add https://site.com/submit # add manually
python -m app.cli export                      # regenerate CSVs
python -m app.cli run-all                     # full cycle (handy from cron)
```

**JSON API**

```
GET  /healthz
GET  /api/stats
GET  /api/opportunities?kind=directory&status=live&min_score=0.5&q=seo&limit=100
POST /api/opportunities/{id}/used?used=true
POST /api/run/{job}
GET  /api/export/all_live.csv
```

**CSV exports** — `exports/all_live.csv` plus one file per type
(`directory.csv`, `profile.csv`, `article.csv`, …), sorted by score:

```
url, domain, root_domain, kind, submission_url, status, http_status,
score, title, source, source_detail, first_seen, last_checked, used
```

`submission_url` is the actual page to work with — the register/submit link
extracted from the page, not just the homepage.

---

## Domain authority: is this link worth having?

Discovery answers *can I get a link here*. Authority answers *is it worth it* —
and they are stored as two separate numbers on purpose, because collapsing them
into one hides which half is missing.

| field | meaning | source |
|---|---|---|
| `score` | 0–1, can I actually place a link (forms, phrases, submit page) | the page itself |
| `page_rank` | 0–10 domain authority | Open PageRank / DataForSEO |

```bash
blf metrics                 # enrich the next batch of live domains
```

Open PageRank is the default: free, 100 domains per request, and it only needs
an email to register at [domcop.com/openpagerank](https://www.domcop.com/openpagerank/).
Set `OPENPAGERANK_API_KEY` and the worker enriches every 6 hours, cheapest-first
(live URLs only — quota is never spent on URLs that might be dead).

For referring domains, backlink counts and a real domain rating, set
`METRICS_PROVIDER=dataforseo` with your login/password instead.

Once metrics exist, `MIN_PAGE_RANK=3` (say) filters the CSV exports and the
dashboard sorts by authority first. Leave it at `0` until enrichment has run,
or you will filter out a database that has no metrics yet.

---

## Backlink tracker: are the links you built still there?

Directories delete listings, editors rewrite posts, sites migrate and drop the
footer. Without monitoring you keep believing in links that vanished months ago.

```bash
blf link add https://blog.com/post https://mysite.com/page --anchor "my anchor" --project q1
blf link import links.csv          # source_url,target_url[,anchor,project]
blf link check                     # verify now
blf link list --status missing
blf link lost                      # links that were live and disappeared
```

Every check records the real state of the link:

| | |
|---|---|
| `live` | the link is on the page |
| `missing` | the page loads fine, your link is gone |
| `unreachable` | the page itself is down |
| `is_dofollow` | parsed from `rel` — `nofollow`, `ugc` and `sponsored` all count as nofollow |
| `anchor_found` | the anchor text actually used, which is often not the one you asked for |

Matching is exact-URL first, then same-domain fallback, so a link that moved to
another page on your site still counts. **A link that goes from live to missing
triggers an immediate Telegram alert** — that is the whole point of the module.

Runs every 12 hours, exports to `tracked_backlinks.csv`.

---

## Outreach: for links that need a human, not a form

Directory submissions scale; guest posts and resource-page links do not — they
need an email a real person wants to answer. This module does the tedious 90%
and stops exactly where judgement starts.

```
verified opportunity ──► prospect ──► find contact ──► draft ──► YOU APPROVE ──► send ──► follow up
```

```bash
blf outreach prospects --min-pr 3 --project q1   # promote the best opportunities
blf outreach contacts                            # find an email for each
blf outreach draft                               # write first mails + due follow-ups
blf outreach review --show-body                  # read what it wrote
blf outreach approve 12 14                       # or: --all-clean
blf outreach send
blf outreach status
```

**Contact discovery** reads the homepage, then follows up to two contact-ish
pages (`write for us`, `contribute`, `contact`, `about`). It decodes obfuscated
addresses (`info [at] site [dot] com`), drops platform noise (`wixpress`,
`noreply@`, image filenames) and ranks `editor@` above `info@` above an
off-domain gmail. Expect a real-world hit rate of roughly 20–40% — plenty of
sites publish only a contact form, and those are marked `no_contact` rather than
guessed at.

**Templates** live in [`config/outreach_templates.yaml`](config/outreach_templates.yaml):
`guest_post`, `resource_page`, `broken_link`, `listing`, plus a two-step
follow-up chain. Each one deliberately contains `[bracketed instructions]` where
a specific detail belongs — an angle, a category, the actual broken links.

### The five gates before anything is sent

Nothing leaves the machine unless *all* of these pass:

1. `OUTREACH_ENABLED=true` — master switch, **off by default**
2. the draft is approved — `OUTREACH_REQUIRE_APPROVAL=true` by default
3. **a draft still holding `[placeholders]` can never be approved**, not even with `--all-clean`
4. the address is not suppressed — `blf outreach suppress <email|domain>`
5. the daily limit is not spent — `OUTREACH_DAILY_LIMIT`

With no SMTP configured the mailer runs in **dry-run**: it logs the message and
marks the draft, so you can rehearse a whole campaign before wiring up a mailbox.
Follow-ups stop the moment you run `blf outreach replied <email>`, cap at two,
and every template carries an opt-out line.

Send from a mailbox you are willing to have flagged as spam, keep the daily
limit low, and personalise the bracketed sections. Automated mail that reads as
automated gets your domain blacklisted — the approval gate exists so that the
volume knob is not the only thing standing between you and that.

---

## How quality is enforced

The failure mode of every "backlink list" is junk. Four filters run before
anything reaches your CSVs:

1. **Normalisation** — scheme/host lowercased, `www.` and tracking params
   stripped, query params sorted, fragments dropped. `http://WWW.Site.com/x/?utm_source=a`
   and `https://site.com/x` are one row, not two.
2. **Junk rejection** — assets, shorteners, IPs, and infrastructure domains
   (github, google, w3.org, wikipedia…) never enter the database.
3. **Per-domain cap** — max 60 URLs per root domain, so one big site cannot
   flood the database and skew every export.
4. **Page-level scoring** — the checker reads the page: matched phrases,
   `<form>` count, password fields, website-URL input fields, and parked/for-sale
   detection that zeroes the score. Every signal is stored as JSON on the row, so
   any score can be explained after the fact.

Statuses are deliberately separate: `live`, `redirect` (final URL stored),
`dead` (404/410 or exhausted retries), and `blocked` (401/403/429/503 or
robots-disallowed — alive but shielded, kept and left alone rather than deleted).

---

## Operations

```bash
make logs        # tail worker logs
make stats       # database + worker health
make backup      # dump the database right now
make up / down   # start / stop the stack
bash scripts/restore.sh                 # restore newest backup
bash scripts/restore.sh backups/x.sql.gz
```

Data lives in the `pgdata` volume plus `./backups`, so `docker compose down`,
rebuilds and code updates never touch it. `.github/workflows/deploy.yml` redeploys
on every push to `main` once you add the `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY`
secrets; CI runs the test suite and a Docker build on every PR.

**Put it behind TLS before exposing it publicly.** The API has only an optional
shared-key gate — terminate with Caddy or nginx and either bind `API_PORT` to
`127.0.0.1` or firewall it.

---

## Layout

```
app/
  collectors/   github_collector.py  footprint_collector.py  list_importer.py
  processors/   url_cleaner.py  classifier.py
  checkers/     live_checker.py        # robots, throttling, concurrency, scoring
  search/       searxng.py  serper.py  brave.py       # pluggable SERP backends
  metrics/      openpagerank.py  dataforseo.py  enrich.py   # domain authority
  trackers/     backlink_tracker.py    # are my placed links still live?
  outreach/     contact_finder.py  templates.py  mailer.py  campaign.py
  exporters/    csv_exporter.py
  notify/       telegram.py
  db/           models.py  session.py  repo.py  migrate.py  # writes go through repo.py
  api/          main.py  templates/dashboard.html
  jobs.py  worker.py  cli.py  config.py
config/         footprints.json  seed_sources.yaml
                outreach_templates.yaml  searxng/settings.yml
scripts/        deploy.sh  restore.sh
tests/          test_pipeline.py  test_modules.py
```

## Roadmap

- Language/geo detection per opportunity
- Duplicate-network detection (same footer, same owner, PBN-ish clusters)
- IMAP reply detection, so follow-ups stop themselves without `blf outreach replied`
- Submission-assist: pre-filled form data and a queue you approve manually,
  rather than blind auto-posting
- Indexation checks on placed links (is Google actually seeing them?)

## Licence

MIT.
