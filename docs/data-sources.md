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

- Results page in tens, and `start=1000` returns HTTP 400 - a real ceiling, so a single query
  reaches at most 1,000 postings. Reaching older jobs needs a narrower `f_TPR` recency window,
  which the incremental sync produces naturally after a first backfill.
- An empty body is **not** proof that a search is exhausted, which is the more expensive lesson.
  During a real run LinkedIn answered `start=250` with an empty page and two walks stopped there
  reporting "no more results"; asked again minutes later, that same offset served a full page, as
  did 260 and 400. Under load LinkedIn says "nothing here" rather than 429, so an empty page is
  slept on and asked again twice before the walk accepts it.
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

Each target role is walked as its own search rather than as one OR'd query. The result ceiling and
the relevance ordering both apply per query, so five roles joined with OR share a single
1,000-result budget and the roles at the end of the string are the ones that lose; walked
separately, each gets its own budget and its own best matches. Every role is walked, too - the
five-role cap that keeps the other providers' single query readable does not apply when a role is
a search of its own, and profile order decides which roles a stopped run reached.

A source whose keywords were written by hand stays one search, because splitting an expression such
as `java AND (kafka OR pulsar)` on OR would produce two broken queries. One posting often answers
several of the role searches, so a run remembers the IDs it has read and does not pay a second
paced request for the same posting.

A walk is unbounded and resumable. It runs through every role search until each is exhausted or
reaches the 1,000-result ceiling, or until the user stops it; the role and the offset reached are
stored in the batch cursor alongside a fingerprint of the searches they belong to, so the next run
continues inside the role it stopped in and starts over whenever the target roles or location have
changed. Postings keep the canonical
`https://www.linkedin.com/jobs/view/<id>/` URL, derived from the job ID rather than the card's
country-specific tracking link, so the same posting deduplicates across sources.

### The signed-in walk

Guest throttling makes a long walk slow, so each generated location also gets a `linkedin_browser`
row, disabled until chosen. The Sources page presents the two as one choice - public search or the
user's own session - and switches every row of a method together, because "which way should
LinkedIn be read" is the real decision and a per-location row is only how that answer is stored.
The signed-in card carries the warning it deserves: automated access to signed-in pages is against
LinkedIn's User Agreement, and the account at risk is the user's own, the same one they apply with.

The browser is used for one thing: the posting description. Titles, employers, locations, and
posting dates keep coming from the guest search cards even in a signed-in run. LinkedIn renders its
own chrome in the account's display language, so a session set to Turkish reports a London job as
"Birleşik Krallık" and a relative date as "4 gün önce"; Playwright's `locale` does not override it,
and an eligibility gate cannot read it. The account's language is the user's setting to make, not
something a collector should work around. Taking metadata from the English guest cards and only the
body from the window sidesteps the problem entirely, and the body is the part the guest endpoints
throttle hardest, so it is also where the session earns its keep.

It opens a visible Chrome on a dedicated profile directory inside the application-data directory and
waits for the user to sign in themselves if LinkedIn asks. RoleBeacon never types credentials and
never stores them; the session lives in that profile directory and can be dropped on its own without
touching the profile used for application autofill.

Being signed in is read from the presence of the session cookie, never from the URL. LinkedIn serves
a guest the same `/jobs/search/` and `/jobs/view/` addresses a member sees, so a first run walked
seventeen postings signed out without ever asking for a sign-in - which collects nothing the public
collector could not already reach and looks, from the desk, like a window refreshing itself. The
cookie is checked by name only; its value is never read. When it is missing the walk opens the
sign-in page, waits up to five minutes for the person to finish, and then asks again for the posting
the sign-in interrupted.

LinkedIn serves two different job pages, which is worth knowing before guessing at a selector. The
signed-out pages and the signed-in *search* page are the older server-rendered markup, with
`#job-details` and `job-card-container__link` class names. A signed-in `/jobs/view/<id>` page is the
newer rewrite: content-hashed class names, no JSON-LD at all, and semantic ids such as
`JobDetails_AboutTheJob_<jobId>`. The collector queries a small union of both shapes and logs once
per run which one answered, so a redesign shows up in the log rather than as blank descriptions.

Waiting for that container to exist is not enough - LinkedIn renders the section and its heading
first and fills the posting in a moment later, so a walk that read on the element's arrival got a
heading and nothing else. The wait is on rendered text length instead, and a posting that still
yields no description is skipped with a warning rather than saved empty.

The limits do not move: job search results and job postings only, never a profile, connection list,
message, or the feed, and never an application submission. Because it opens a window and can wait
on a person, `linkedin_browser` is an interactive kind - a scheduled sync always skips it as
`interactive_source`, and it runs only for a manual refresh or `rolebeacon sync --interactive`.
The web Refresh button still honours each source's minimum interval, so `--force` is what retries a
signed-in walk immediately. Stopping is closing the window or Ctrl-C; either checkpoints the cursor,
and the browser is shut down on a deadline so a driver that outlives its window cannot hold the run
open.

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
