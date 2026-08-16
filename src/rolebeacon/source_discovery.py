from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
import pycountry

from .domain import SourceConfig
from .profile import country_catalog


class SourceDiscoveryError(ValueError):
    """Raised when a careers URL cannot be converted into a supported source."""


@dataclass(slots=True)
class SourcePreview:
    source: SourceConfig
    jobs_found: int
    sample_jobs: list[dict[str, str]]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "jobs_found": self.jobs_found,
            "sample_jobs": self.sample_jobs,
            "message": self.message,
        }


def detect_source(careers_url: str, company: str = "") -> SourceConfig:
    """Convert a public ATS board URL into a validated source configuration."""
    raw_url = careers_url.strip()
    if not raw_url:
        raise SourceDiscoveryError("Enter a company careers or ATS board URL")
    if len(raw_url) > 2048:
        raise SourceDiscoveryError("The careers URL is too long")
    parts = urlsplit(raw_url)
    if parts.scheme != "https" or not parts.hostname:
        raise SourceDiscoveryError("Use a public HTTPS careers URL")

    host = parts.hostname.casefold().rstrip(".")
    segments = [segment for segment in parts.path.split("/") if segment]
    company_name = _company_name(company, segments)
    common = {
        "name": company_name,
        "company": company_name,
        "enabled": True,
        "min_sync_interval_seconds": 14400,
        "trust_priority": 100,
        "ingestion_filter": True,
        "careers_url": raw_url,
        "managed_by": "user",
    }

    if host in {"www.google.com", "careers.google.com"} and "/jobs/results" in parts.path:
        query = parse_qs(parts.query)
        identifier = "-".join(filter(None, (query.get("q", [""])[0], query.get("location", [""])[0]))) or "all"
        return SourceConfig.from_dict({
            "id": _source_id("google-careers", company_name, identifier), "kind": "google_careers",
            "url": raw_url, "max_pages": 2, **common,
        })

    if host in {"www.amazon.jobs", "amazon.jobs"} and "/search" in parts.path:
        query = parse_qs(parts.query)
        identifier = "-".join(filter(None, (query.get("base_query", [""])[0], query.get("loc_query", [""])[0]))) or "all"
        return SourceConfig.from_dict({
            "id": _source_id("amazon-jobs", company_name, identifier), "kind": "amazon_jobs",
            "url": raw_url, "max_pages": 5, **_amazon_location_filter(query), **common,
        })

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}:
        slug = _required_segment(segments, "Greenhouse board token")
        return SourceConfig.from_dict({"id": _source_id("greenhouse", company_name, slug), "kind": "greenhouse", "slug": slug, **common})

    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        slug = _required_segment(segments, "Lever site name")
        api_host = "https://api.eu.lever.co" if host == "jobs.eu.lever.co" else "https://api.lever.co"
        return SourceConfig.from_dict({
            "id": _source_id("lever", company_name, slug), "kind": "lever", "slug": slug,
            "host": api_host, **common,
        })

    if host == "jobs.ashbyhq.com":
        slug = _required_segment(segments, "Ashby board name")
        return SourceConfig.from_dict({"id": _source_id("ashby", company_name, slug), "kind": "ashby", "slug": slug, **common})

    if host == "jobs.smartrecruiters.com":
        slug = _required_segment(segments, "SmartRecruiters company identifier")
        return SourceConfig.from_dict({
            "id": _source_id("smartrecruiters", company_name, slug), "kind": "smartrecruiters", "slug": slug, **common,
        })

    if host.endswith(".myworkdayjobs.com") or host.endswith(".myworkdaysite.com"):
        tenant = host.split(".", 1)[0]
        site_segments = [segment for segment in segments if not re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", segment)]
        site = _required_segment(site_segments, "Workday career site")
        return SourceConfig.from_dict({
            "id": _source_id("workday", company_name, f"{tenant}-{site}"), "kind": "workday",
            "host": f"https://{host}", "tenant": tenant, "site": site, **common,
        })

    if host.endswith(".jobs.personio.de") or host.endswith(".jobs.personio.com"):
        board = host.split(".", 1)[0]
        return SourceConfig.from_dict({
            "id": _source_id("personio", company_name, board), "kind": "personio", "slug": board,
            "host": f"https://{host}", **common,
        })

    custom_sites = {
        "apply.careers.microsoft.com": "Microsoft Careers",
        "jobs.careers.microsoft.com": "Microsoft Careers",
        "www.metacareers.com": "Meta Careers",
        "metacareers.com": "Meta Careers",
        "jobs.apple.com": "Apple Jobs",
        "jobs.netflix.com": "Netflix Jobs",
    }
    if host in custom_sites:
        raise SourceDiscoveryError(
            f"{custom_sites[host]} uses a company-specific careers system. "
            "RoleBeacon needs a dedicated first-party connector for this URL; LinkedIn alerts can cover it meanwhile."
        )
    raise SourceDiscoveryError(
        "This careers URL is not a supported public ATS board. Use a Greenhouse, Lever, Ashby, "
        "SmartRecruiters, Workday, or Personio board URL."
    )


class SourceDiscoveryService:
    def __init__(self, client_factory: Any | None = None):
        self.client_factory = client_factory or _http_client

    async def preview(self, careers_url: str, company: str = "") -> SourcePreview:
        source = detect_source(careers_url, company)
        async with self.client_factory() as client:
            jobs_found, sample_jobs = await self._preview_source(client, source)
        if not jobs_found:
            raise SourceDiscoveryError(
                "The ATS board was recognized but returned no public jobs. Check the URL before saving it."
            )
        connector_name = source.kind.replace("_", " ").title()
        message = f"Connected to {connector_name} and found {jobs_found} public jobs."
        if source.kind == "amazon_jobs" and source.options.get("location_filter_text"):
            message = (
                f"Connected to {connector_name} and found {jobs_found} location-matched jobs "
                "in the newest provider page."
            )
        return SourcePreview(
            source=source,
            jobs_found=jobs_found,
            sample_jobs=sample_jobs,
            message=message,
        )

    async def _preview_source(
        self, client: httpx.AsyncClient, source: SourceConfig
    ) -> tuple[int, list[dict[str, str]]]:
        if source.kind == "greenhouse":
            response = await client.get(f"https://boards-api.greenhouse.io/v1/boards/{source.slug}/jobs")
            response.raise_for_status()
            items = response.json().get("jobs", [])
            return len(items), [_greenhouse_summary(item) for item in items[:3]]
        if source.kind == "lever":
            response = await client.get(
                f"{source.host.rstrip('/')}/v0/postings/{source.slug}", params={"mode": "json", "limit": 5}
            )
            response.raise_for_status()
            items = response.json()
            return len(items), [_lever_summary(item) for item in items[:3]]
        if source.kind == "ashby":
            response = await client.get(f"https://api.ashbyhq.com/posting-api/job-board/{source.slug}")
            response.raise_for_status()
            payload = response.json()
            items = payload.get("jobs", payload.get("jobPostings", []))
            return len(items), [_ashby_summary(item) for item in items[:3]]
        if source.kind == "smartrecruiters":
            response = await client.get(
                f"https://api.smartrecruiters.com/v1/companies/{source.slug}/postings",
                params={"limit": 5, "offset": 0},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("content", [])
            return int(payload.get("totalFound", len(items))), [_smartrecruiters_summary(item) for item in items[:3]]
        if source.kind == "workday":
            base = f"{source.host.rstrip('/')}/wday/cxs/{source.tenant}/{source.site}"
            response = await client.post(
                f"{base}/jobs", json={"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": ""}
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("jobPostings", [])
            return int(payload.get("total", len(items))), [_workday_summary(item) for item in items[:3]]
        if source.kind == "personio":
            response = await client.get(f"{source.host.rstrip('/')}/xml")
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            items = root.findall("./position")
            return len(items), [_personio_summary(item, source.host) for item in items[:3]]
        if source.kind == "google_careers":
            response = await client.get(source.url)
            response.raise_for_status()
            links = google_result_links(response.text, str(response.url))
            total_match = re.search(r"([\d,]+)\s+jobs matched", plain_html_text(response.text), re.IGNORECASE)
            total = int(total_match.group(1).replace(",", "")) if total_match else len(links)
            return total, [{"title": title, "location": "See job details", "url": url} for url, title in links[:3]]
        if source.kind == "amazon_jobs":
            response = await client.get(
                "https://www.amazon.jobs/en/search.json",
                params=amazon_search_params(source.url, 0, 100, str(source.options.get("location_filter_code", ""))),
            )
            response.raise_for_status()
            payload = response.json()
            provider_items = payload.get("jobs", [])
            items = [item for item in provider_items if amazon_location_matches(item, source)]
            return len(items), [_amazon_summary(item) for item in items[:3]]
        raise SourceDiscoveryError(f"Preview is not implemented for connector: {source.kind}")


def same_source(left: SourceConfig, right: SourceConfig) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind in {"google_careers", "amazon_jobs"}:
        return _canonical_url(left.url) == _canonical_url(right.url)
    return (
        left.kind,
        left.slug.casefold(),
        left.host.casefold().rstrip("/"),
        left.tenant.casefold(),
        left.site.casefold(),
    ) == (
        right.kind,
        right.slug.casefold(),
        right.host.casefold().rstrip("/"),
        right.tenant.casefold(),
        right.site.casefold(),
    )


_ROLE_QUERY_KEYS = {"q", "base_query"}


def _canonical_url(value: str) -> str:
    parts = urlsplit(value)
    # Role-text params are always overwritten by personalize_source() at sync time, so they
    # must not affect source identity - otherwise a stale/legacy "q=" makes two rows that
    # target the same provider+location look like different sources (and vice versa).
    pairs = [
        pair
        for pair in parse_qsl(parts.query, keep_blank_values=True)
        if pair[0] not in _ROLE_QUERY_KEYS
    ]
    query = urlencode(sorted(pairs), doseq=True)
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), query, ""))


def _required_segment(segments: list[str], label: str) -> str:
    if not segments:
        raise SourceDiscoveryError(f"The URL does not contain a {label}")
    return segments[0]


def _company_name(value: str, segments: list[str]) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    if len(clean) > 120:
        raise SourceDiscoveryError("Company name must be 120 characters or fewer")
    if clean:
        return clean
    if not segments:
        return "Company careers"
    return re.sub(r"[-_]+", " ", segments[0]).strip().title()


def _source_id(kind: str, company: str, identifier: str) -> str:
    material = f"{kind}-{company}-{identifier}".casefold()
    value = re.sub(r"[^a-z0-9]+", "-", material).strip("-")
    return value[:100]


def relocation_source_candidates(countries: list[dict[str, str]]) -> list[SourceConfig]:
    """Build one generated Google Careers and one generated Amazon Jobs source per target country.

    No role text is included in the URL - personalize_source() injects the candidate's real
    target_roles at every sync, so a placeholder here would never actually reach either provider.
    """
    candidates: list[SourceConfig] = []
    for country in countries:
        code, name = country["code"], country["name"]
        candidates.append(SourceConfig.from_dict({
            # Country-suffixed so dozens of otherwise-identical rows are traceable to a country on
            # the Sources health table. The Jobs-page filter still groups all of a kind under one
            # label (see _GROUPED_SOURCE_LABELS in app.py) - that grouping is separate from this name.
            "id": _source_id("google-careers", "", name), "kind": "google_careers", "name": f"Google Careers — {name}",
            "company": "Google", "enabled": False, "min_sync_interval_seconds": 14400, "trust_priority": 100,
            "max_pages": 10,  # both collectors break early on an empty page, so this is a ceiling, not a target
            "ingestion_filter": True, "official_first_party": True,
            "url": f"https://www.google.com/about/careers/applications/jobs/results/?{urlencode({'location': name})}",
        }))
        candidates.append(SourceConfig.from_dict({
            "id": _source_id("amazon-jobs", "", name), "kind": "amazon_jobs", "name": f"Amazon Jobs — {name}",
            "company": "Amazon", "enabled": False, "min_sync_interval_seconds": 14400, "trust_priority": 100,
            "max_pages": 10, "location_filter_code": code, "location_filter_text": name,
            "ingestion_filter": True, "official_first_party": True,
            "url": f"https://www.amazon.jobs/en/search?{urlencode({'loc_query': name})}",
        }))
    # One additional, non-country-scoped source: Google Careers' own "Remote eligible" filter
    # (has_remote=true, confirmed against the live site) catches roles with no fixed country tag
    # at all, which no per-country query above can ever match. Added once, not per country.
    # Amazon Jobs has no equivalent filter in its own search UI (checked: Industry experience,
    # Job type, Job category, Country/Region, State/Province - no remote facet) - guessing a
    # query param for it would just add another source that silently returns nothing.
    candidates.append(SourceConfig.from_dict({
        "id": "google-careers-remote", "kind": "google_careers", "name": "Google Careers — Remote",
        "company": "Google", "enabled": False, "min_sync_interval_seconds": 14400, "trust_priority": 100,
        "max_pages": 10, "ingestion_filter": True, "official_first_party": True,
        "url": f"https://www.google.com/about/careers/applications/jobs/results/?{urlencode({'has_remote': 'true'})}",
    }))
    return candidates


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15, connect=5),
        follow_redirects=True,
        headers={"User-Agent": "RoleBeacon/0.2 (+https://github.com/srknzl/rolebeacon)"},
    )


def _greenhouse_summary(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("title", "")),
        "location": str((item.get("location") or {}).get("name", "")),
        "url": str(item.get("absolute_url", "")),
    }


def _lever_summary(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("text", "")),
        "location": str((item.get("categories") or {}).get("location", "")),
        "url": str(item.get("hostedUrl", "")),
    }


def _ashby_summary(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("title", "")),
        "location": str(item.get("location", "")),
        "url": str(item.get("jobUrl", item.get("applyUrl", ""))),
    }


def _smartrecruiters_summary(item: dict[str, Any]) -> dict[str, str]:
    location = item.get("location") or {}
    return {
        "title": str(item.get("name", "")),
        "location": ", ".join(
            str(value) for value in (location.get("city"), location.get("region"), location.get("country")) if value
        ),
        "url": str(item.get("ref", "")),
    }


def _workday_summary(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("title", "")),
        "location": str(item.get("locationsText", "")),
        "url": str(item.get("externalPath", "")),
    }


def _personio_summary(item: ElementTree.Element, host: str) -> dict[str, str]:
    source_job_id = _element_text(item, "id")
    return {
        "title": _element_text(item, "name"),
        "location": _element_text(item, "office"),
        "url": f"{host.rstrip('/')}/job/{source_job_id}?display=en",
    }


def _element_text(item: ElementTree.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""


class _GoogleResultsParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        href = unescape(values.get("href", ""))
        label = unescape(values.get("aria-label", ""))
        if "jobs/results/" not in href or not label.startswith("Learn more about "):
            return
        if href.startswith("jobs/results/"):
            base = urlsplit(self.base_url)
            resolved = urljoin(f"{base.scheme}://{base.netloc}/about/careers/applications/", href)
        else:
            resolved = urljoin(self.base_url, href)
        self.links.append((resolved, label.removeprefix("Learn more about ").strip()))


def google_result_links(value: str, base_url: str) -> list[tuple[str, str]]:
    parser = _GoogleResultsParser(base_url)
    parser.feed(value)
    return list(dict.fromkeys(parser.links))


def amazon_search_params(careers_url: str, offset: int, limit: int, location_filter_code: str = "") -> dict[str, Any]:
    query = parse_qs(urlsplit(careers_url).query)
    allowed = {
        "base_query", "loc_query", "country", "city", "region", "category[]", "business_category[]",
        "job_type[]", "is_manager[]", "is_intern[]", "latitude", "longitude", "radius",
        "distanceType", "loc_group_id",
    }
    params: dict[str, Any] = {key: values if len(values) > 1 else values[0] for key, values in query.items() if key in allowed}
    # amazon.jobs only geo-filters server-side on the ISO alpha-3 "country" param. Without it, "loc_query"
    # is a hint the API mostly ignores, and a page sorted by "recent" across every country returns almost
    # no matches for one. This derives it from the source's own configured location filter.
    if "country" not in params and location_filter_code:
        country = pycountry.countries.get(alpha_2=location_filter_code.upper())
        if country:
            params["country"] = country.alpha_3
    params.update({"offset": offset, "result_limit": limit, "sort": query.get("sort", ["recent"])[0]})
    return params


def _amazon_location_filter(query: dict[str, list[str]]) -> dict[str, str]:
    if query.get("latitude") and query.get("longitude"):
        return {}
    location = query.get("loc_query", [""])[0].strip()
    if not location:
        return {}
    folded = location.casefold()
    for item in country_catalog():
        if folded in {item["code"].casefold(), item["name"].casefold()}:
            return {
                "location_filter_code": item["code"],
                "location_filter_text": item["name"],
            }
    return {"location_filter_text": location}


def amazon_location_matches(item: dict[str, Any], source: SourceConfig) -> bool:
    location = str(item.get("location", "")).strip()
    code = str(source.options.get("location_filter_code", "")).strip().upper()
    text = str(source.options.get("location_filter_text", "")).strip().casefold()
    if code:
        first_segment = location.split(",", 1)[0].strip().upper()
        return first_segment == code or bool(text and text in location.casefold())
    return not text or text in location.casefold()


def plain_html_text(value: str) -> str:
    class TextParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            if data.strip():
                self.parts.append(data.strip())

    parser = TextParser()
    parser.feed(value)
    return " ".join(parser.parts)


def _amazon_summary(item: dict[str, Any]) -> dict[str, str]:
    return {
        "title": str(item.get("title", "")).strip(),
        "location": str(item.get("location", "")),
        "url": urljoin("https://www.amazon.jobs", str(item.get("job_path", ""))),
    }
