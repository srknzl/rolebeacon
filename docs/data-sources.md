# Job data source assessment

Research date: 2026-08-11.

RoleBeacon favors first-party career pages and documented provider APIs. A source must preserve
the original posting URL, publication time, location restrictions, and provider attribution. A
large feed is not useful if its employment geography cannot be assessed conservatively.

## Company board workflow

The Sources page accepts a public careers URL and supports two classes of company source:

- documented public ATS boards: Greenhouse, Lever, Ashby, SmartRecruiters, and Workday;
- isolated first-party connectors: Google Careers server-rendered job pages and the Amazon Jobs
  public-site JSON response.

Detection is deterministic and restricted to known HTTPS hostnames. RoleBeacon derives the provider
endpoint, fetches a small preview, and saves the source only after confirmation. A connector is reusable
code; each company board or filtered first-party search is a separate source instance. Saving preferences
preserves user-added instances.

Google Careers and Amazon Jobs do not advertise these read surfaces as supported public APIs. Their
connectors therefore use four-hour polling by default, content hashes, provider-specific contract tests,
and independent health reporting. A response change disables only that source run. Microsoft, Meta,
Apple, and Netflix remain explicit coverage gaps until equivalent first-party adapters are implemented.

Amazon's free-text `loc_query` is display state rather than an enforced JSON API filter. When a pasted
URL does not include coordinates, RoleBeacon derives a deterministic country or city post-filter and
applies it to every collected page. The preview reports matches from the newest provider page instead
of presenting Amazon's unfiltered global count as local coverage.

## LinkedIn boundary

LinkedIn does not expose a general personal job-search API. Its documented Job Posting and Apply
Connect APIs are restricted partner integrations for ATS vendors, job distributors, and employer
customers. They publish employer jobs to LinkedIn or connect applications; they do not provide a
self-service API for a job seeker to search and download LinkedIn's corpus.

RoleBeacon therefore does not log into or scrape LinkedIn. The supported path is:

1. Create daily LinkedIn Job Alerts from searches derived from your RoleBeacon preferences.
2. Deliver the alerts by email and apply the private Gmail label `Job Alerts`.
3. Enable the read-only Gmail collector.
4. Ingest alert URLs and summaries, deduplicate them, and score them with reduced confidence when
   the email does not contain the full description.
5. Open the original posting for final review and application.

LinkedIn currently allows up to 20 job alerts. It officially supports uppercase `AND`, `OR`, and
`NOT`, exact phrases in quotes, and parentheses. Company-page alerts are useful for high-priority
employers.

Official references:

- [LinkedIn Job Posting API overview](https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview)
- [LinkedIn Boolean search](https://www.linkedin.com/help/linkedin/answer/a524335/)
- [LinkedIn Job Alerts](https://www.linkedin.com/help/linkedin/answer/a511279/)
- [LinkedIn job filters](https://www.linkedin.com/help/linkedin/answer/a511259/)

## Recommended additions

| Priority | Source | Access | Value | Constraints | Recommendation |
| --- | --- | --- | --- | --- | --- |
| 1 | Arbeitnow | Free, no key | Europe-focused ATS aggregation, remote field, and explicit `visa_sponsorship` filter | Aggregated data still needs canonical deduplication | Built in; user enables it during setup |
| 2 | Jobicy | Free REST and RSS | Structured remote geography, full description, salary fields, Europe and Türkiye taxonomies | Recent-job cap, delayed publication, attribution, and fair-use rules | Built in; user enables it during setup |
| 3 | Remotive | Free REST and RSS | Full descriptions and explicit candidate location restrictions | Delayed public data, attribution, and conservative polling requirements | Built in; user enables it during setup |
| 4 | Adzuna | API key | Broad country-scoped search, salary data, pagination, and established developer API | Aggregator duplicates and redirects require provenance checks; credentials and a positive local budget required | Optional and disabled by default |
| 5 | Jooble | Requested API key | Broad international coverage and keyword/location search | Aggregated provenance and duplicate quality need a benchmark | Optional and disabled by default |
| 6 | SerpApi Google Jobs | Paid API key | Fills coverage gaps through structured Google Jobs results and geolocation | Commercial dependency, cost, duplicates, and upstream layout changes | Optional and disabled by default |

Official provider references:

- [Arbeitnow Job Board API](https://www.arbeitnow.com/blog/job-board-api)
- [Jobicy Remote Jobs API](https://github.com/Jobicy/remote-jobs-api)
- [Remotive public API](https://remotive.com/remote-jobs/api)
- [Adzuna Job Search API](https://developer.adzuna.com/docs/search)
- [Jooble REST API](https://jooble.org/api/about)
- [SerpApi Google Jobs API](https://serpapi.com/google-jobs-api)

## Sources that do not solve the gap directly

- EURES has a valuable official portal and millions of European vacancies, including employers
  interested in cross-border recruitment, but no documented general-purpose public vacancy API
  was found. Prefer alerts or a future approved integration over reverse-engineering the portal.
- Google Cloud Talent Solution is search infrastructure for a customer's own uploaded job corpus;
  it is not an API for the public Google Jobs index.
- Unofficial LinkedIn, Indeed, or Glassdoor scraper APIs are excluded from the default design.
  They create account, terms-of-service, provenance, and breakage risk disproportionate to a
  personal job-search system.

EURES references:

- [EURES services](https://eures.europa.eu/eures-services_en)
- [How EURES vacancies are sourced](https://eures.europa.eu/employers/advertise-job_en)
