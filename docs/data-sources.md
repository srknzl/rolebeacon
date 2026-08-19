# Job data source assessment

Research date: 2026-08-16.

RoleBeacon favors first-party career pages and documented provider APIs. A source must preserve
the original posting URL, publication time, location restrictions, and provider attribution. A
large feed is not useful if its employment geography cannot be assessed conservatively.

## Company board workflow

The Sources page accepts a public careers URL and supports two classes of company source:

- documented public ATS boards: Greenhouse, Lever, Ashby, SmartRecruiters, Workday, and
  Personio's public XML feed;
- isolated first-party connectors: Google Careers server-rendered job pages and the Amazon Jobs
  public-site JSON response.

Detection is deterministic and restricted to known HTTPS hostnames. RoleBeacon derives the provider
endpoint, fetches a small preview, and saves the source only after confirmation. A connector is reusable
code; each company board or filtered first-party search is a separate source instance. Saving preferences
preserves user-added instances.

## Curated source catalog and packs

`source-packs.json` is RoleBeacon's versioned archive of known official company boards. Each entry has a
stable catalog ID, company, public board URL, detected connector, schema version, and verification date.
Packs are named, overlapping selections over that registry; they do not duplicate connector code or job
records. Users can install a pack, browse and search every verified entry, or install one company board.

Pack installation is idempotent and atomic. Installing an updated pack refreshes catalog metadata without
disabling a source the user already enabled. The default Add action makes no external request; Add & enable
opts the source into the next refresh. This separation matters because the complete tech-company catalog
can produce thousands of postings and substantial local-model work.

The registry includes unsupported first-party career sites as explicit coverage gaps rather than
installable sources. Entries are reviewed against public provider endpoints, but the verification date is
not a promise of permanent availability. Contract tests and source-local health reporting handle changes.

Personio boards are read from the employer's documented, credential-free
`https://<account>.jobs.personio.com/xml` feed. The connector uses the same four-hour default polling
interval as other company boards, preserves the public Personio job URL, and never uses the
authenticated Recruiting API.

Google Careers and Amazon Jobs do not advertise these read surfaces as supported public APIs. Their
connectors therefore use four-hour polling by default, content hashes, provider-specific contract tests,
and independent health reporting. A response change disables only that source run. Microsoft, Meta,
Apple, and Netflix remain explicit coverage gaps until equivalent first-party adapters are implemented.

Amazon's free-text `loc_query` is display state rather than an enforced JSON API filter. When a pasted
URL does not include coordinates, RoleBeacon derives a deterministic country or city post-filter and
applies it to every collected page. The preview reports matches from the newest provider page instead
of presenting Amazon's unfiltered global count as local coverage.

## LinkedIn

LinkedIn does not expose a general personal job-search API. Its documented Job Posting and Apply
Connect APIs are restricted partner integrations for ATS vendors, job distributors, and employer
customers. They publish employer jobs to LinkedIn or connect applications; they do not provide a
self-service API for a job seeker to search and download LinkedIn's corpus.

RoleBeacon therefore reads LinkedIn the only way it can without an account: the credential-free
guest endpoints that serve a signed-out visitor. `seeMoreJobPostings/search` returns result cards
carrying the job ID, title, employer, location, and posting date; `jobPosting/<id>` returns that
posting's public description fragment. RoleBeacon never signs in, never sends a cookie, and never
requests an authenticated page, a profile, a connection, or a message. An earlier revision read
LinkedIn Job Alert emails through a user-owned Gmail label; that collector was removed because a
digest email carries no recoverable description or employer name.

Verified behavior, measured against the live endpoints rather than assumed:

- Results page in tens. `start=975` returns an empty body and `start=1000` returns HTTP 400, so a
  single query reaches at most 1,000 postings. Reaching older jobs needs a narrower `f_TPR`
  recency window, which the incremental sync produces naturally after a first backfill.
- Roughly 1s spacing draws HTTP 429 after about ten postings; 3s spacing completed an 18-posting
  run untouched. The collector paces at 2.5-4.5s per posting and treats 429 as a wait, not a
  failure, retrying with escalating backoff before checkpointing.
- The rate budget is bursty rather than fixed. A four-source run held ~16 postings a minute for
  four and a half minutes, then spent five minutes paying a 60s penalty for every three or four
  postings, then recovered to its old rate unprompted. The pace is therefore not a constant: every
  429 widens the spacing shared by all LinkedIn sources and every served request eases it back, so
  a walk tracks what LinkedIn is allowing at the time. One clock covers every source, because they
  sync concurrently and LinkedIn counts the host, not the row.
- Search answers HTTP 500 for a query it serves fine seconds later, so server errors are retried
  on a short backoff and only checkpoint the walk once the retries are exhausted.
- `Europe`, `North America`, and `Türkiye` all resolve as locations, so a continent is one source
  row rather than dozens of per-country rows over the same postings.
- The postings carry no JSON-LD, so descriptions are parsed from the `show-more-less-html__markup`
  subtree only, keeping the repeated top-card title and employer out of the description text.

A walk is unbounded and resumable. It runs until the search is exhausted, the 1,000-result ceiling
is reached, or the user stops it; the offset reached is stored in the batch cursor alongside a
fingerprint of the query it belongs to, so the next run continues where it stopped and starts over
whenever the target roles or location have changed. Postings keep the canonical
`https://www.linkedin.com/jobs/view/<id>/` URL, derived from the job ID rather than the card's
country-specific tracking link, so the same posting deduplicates across sources.

### The signed-in walk

Guest throttling makes a long walk slow, so each generated location also gets a `linkedin_browser`
row, disabled until chosen. It opens a visible Chrome on a dedicated profile directory inside the
application-data directory, waits for the user to sign in themselves if LinkedIn asks, and walks the
same searches through the same cursor, break, and progress machinery. RoleBeacon never types
credentials and never stores them; the session lives in that profile directory and can be dropped
on its own without touching the profile used for application autofill.

The limits do not move: job search results and job postings only, never a profile, connection list,
message, or the feed, and never an application submission. Because it opens a window and can wait
on a person, `linkedin_browser` is an interactive kind — a scheduled sync always skips it as
`interactive_source`, and it runs only for a manual refresh or `rolebeacon sync --interactive`.

Postings are read from a `JobPosting` JSON-LD block when the page carries one and from the
description markup otherwise; which path answered is logged once per run, and a posting that yields
no description is skipped with a warning rather than saved empty, so a LinkedIn redesign is visible
in the log instead of silently producing blank jobs.

Official references:

- [LinkedIn Job Posting API overview](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview)
- [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement)

## Recommended additions

| Priority | Source | Access | Value | Constraints | Recommendation |
| --- | --- | --- | --- | --- | --- |
| 1 | Arbeitnow | Free, no key | Europe-focused ATS aggregation, remote field, and explicit `visa_sponsorship` filter | Aggregated data still needs canonical deduplication | Built in; user enables it during setup |
| 2 | Jobicy | Free REST and RSS | Structured remote geography, full description, salary fields, Europe and Türkiye taxonomies | Recent-job cap, delayed publication, attribution, and fair-use rules | Built in; user enables it during setup |
| 3 | Remotive | Free REST and RSS | Full descriptions and explicit candidate location restrictions | Delayed public data, attribution, and conservative polling requirements | Built in; user enables it during setup |
| 4 | Adzuna | API key | Broad country-scoped search, salary data, pagination, and established developer API | Aggregator duplicates and redirects require provenance checks; credentials and a positive local budget required | Optional and disabled by default |
| 5 | Jooble | Requested API key | Broad international coverage and keyword/location search | Aggregated provenance and duplicate quality need a benchmark | Optional and disabled by default |
| 6 | SerpApi Google Jobs | Paid API key | Fills coverage gaps through structured Google Jobs results and geolocation | Commercial dependency, cost, duplicates, and upstream layout changes | Optional and disabled by default |

Collector completeness is explicit. Only a non-truncated, plausible complete snapshot participates in
absence-based closure; partial, date-bounded, failed, or page-limited responses never deactivate a
previously active posting. Complete JSON and XML collectors validate their expected top-level payload
shape before declaring a snapshot. When a complete snapshot falls below 50% of an accepted baseline of
at least 20 jobs, or becomes empty while jobs remain active, RoleBeacon preserves missing jobs and stores
the observed source-job ID fingerprint. Reconciliation occurs only if the next complete snapshot has the
same fingerprint. A declared `provider_total` larger than the raw returned record count is explicitly
incomplete and can never be confirmed. Provider totals are compared before
source-ID deduplication; accepted baselines and confirmation fingerprints use unique source-job IDs.
The Sources page shows the pending baseline and count warning.

Advanced source definitions may override `snapshot_drop_ratio` and
`snapshot_drop_minimum_baseline`; invalid values fall back to the conservative defaults. The accepted
baseline, pending confirmation, warning, and any eventual job deactivations are persisted
transactionally. A job remains active while any active source still reports it, and provenance is
preserved during reconciliation.

Official provider references:

- [Arbeitnow Job Board API](https://www.arbeitnow.com/blog/job-board-api)
- [Jobicy Remote Jobs API](https://github.com/Jobicy/remote-jobs-api)
- [Remotive public API](https://remotive.com/remote-jobs/api)
- [Adzuna Job Search API](https://developer.adzuna.com/docs/search)
- [Jooble REST API](https://jooble.org/api/about)
- [SerpApi Google Jobs API](https://serpapi.com/google-jobs-api)
- [Personio XML job integration](https://support.personio.de/hc/en-us/articles/207576365-Integrate-jobs-from-Personio-into-your-company-website-via-XML)

## Sources that do not solve the gap directly

- EURES has a valuable official portal and millions of European vacancies, including employers
  interested in cross-border recruitment, but no documented general-purpose public vacancy API
  was found. Prefer alerts or a future approved integration over reverse-engineering the portal.
- Google Cloud Talent Solution is search infrastructure for a customer's own uploaded job corpus;
  it is not an API for the public Google Jobs index.
- Unofficial LinkedIn, Indeed, or Glassdoor scraper APIs are excluded from the default design.
- LinkedIn coverage is job search results and job postings only, whether read through the guest
  endpoints or through the user's own signed-in browser session. Profiles, connections, messages,
  and the feed stay out of scope.
  They create account, terms-of-service, provenance, and breakage risk disproportionate to a
  personal job-search system.

EURES references:

- [EURES services](https://eures.europa.eu/eures-services_en)
- [How EURES vacancies are sourced](https://eures.europa.eu/employers/advertise-job_en)
