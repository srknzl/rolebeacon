from __future__ import annotations

import asyncio
import base64
import html
import os
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from .domain import CollectedJob, CollectionBatch, SourceConfig

USER_AGENT = "RoleBeacon/0.2 (+https://github.com/srknzl/rolebeacon)"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skipped_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg", "template"}:
            self.skipped_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg", "template"} and self.skipped_depth:
            self.skipped_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skipped_depth:
            return
        value = data.strip()
        if value:
            self.parts.append(value)


def plain_text(value: str | None) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(html.unescape(value))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(timestamp, UTC)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        try:
            return parsedate_to_datetime(text).astimezone(UTC)
        except (TypeError, ValueError):
            return None


def is_recent(job: CollectedJob, since: datetime) -> bool:
    date = job.updated_at or job.published_at
    return date is None or date >= since


class Collector(ABC):
    def __init__(self, config: SourceConfig, client: httpx.AsyncClient):
        self.config = config
        self.client = client

    @abstractmethod
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob] | CollectionBatch:
        raise NotImplementedError


def as_batch(value: list[CollectedJob] | CollectionBatch) -> CollectionBatch:
    return value if isinstance(value, CollectionBatch) else CollectionBatch(jobs=value)


def _signals(**values: Any) -> dict[str, Any]:
    return {"signals": {key: value for key, value in values.items() if value not in (None, "", [], {})}}


class GreenhouseCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        if not self.config.slug:
            raise ValueError("Greenhouse source requires a board slug")
        response = await self.client.get(
            f"https://boards-api.greenhouse.io/v1/boards/{self.config.slug}/jobs",
            params={"content": "true"},
        )
        response.raise_for_status()
        result = []
        for item in response.json().get("jobs", []):
            metadata = item.get("metadata") or []
            job = CollectedJob(
                source=self.config.id,
                source_job_id=str(item["id"]),
                title=item.get("title", ""),
                company=self.config.company or self.config.name,
                location=(item.get("location") or {}).get("name", ""),
                description=plain_text(item.get("content")),
                url=item.get("absolute_url", ""),
                apply_url=item.get("absolute_url", ""),
                updated_at=parse_datetime(item.get("updated_at")),
                employment_type=_metadata_value(metadata, "employment type"),
                metadata={"departments": item.get("departments", []), "offices": item.get("offices", [])},
            )
            if is_recent(job, since):
                result.append(job)
        return result


def _metadata_value(metadata: list[dict[str, Any]], key: str) -> str:
    for item in metadata:
        if str(item.get("name", "")).casefold() == key.casefold():
            return str(item.get("value") or "")
    return ""


class LeverCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        if not self.config.slug:
            raise ValueError("Lever source requires a site slug")
        base = self.config.host.rstrip("/") if self.config.host else "https://api.lever.co"
        response = await self.client.get(
            f"{base}/v0/postings/{self.config.slug}",
            params={"mode": "json", "limit": 500},
        )
        response.raise_for_status()
        result = []
        for item in response.json():
            categories = item.get("categories") or {}
            salary = item.get("salaryRange") or {}
            location = categories.get("location", "")
            job = CollectedJob(
                source=self.config.id,
                source_job_id=str(item.get("id", item.get("hostedUrl", ""))),
                title=item.get("text", ""),
                company=self.config.company or self.config.name,
                location=location,
                description=plain_text(item.get("descriptionPlain") or item.get("description")),
                url=item.get("hostedUrl", ""),
                apply_url=item.get("applyUrl", item.get("hostedUrl", "")),
                remote_scope=location if "remote" in location.casefold() else "",
                employment_type=categories.get("commitment", ""),
                salary_min=salary.get("min"),
                salary_max=salary.get("max"),
                salary_currency=salary.get("currency", ""),
                published_at=parse_datetime(item.get("createdAt")),
                updated_at=parse_datetime(item.get("updatedAt")),
                metadata={"team": categories.get("team", ""), "department": categories.get("department", "")},
            )
            if is_recent(job, since):
                result.append(job)
        return result


class AshbyCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        if not self.config.slug:
            raise ValueError("Ashby source requires a board slug")
        response = await self.client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{self.config.slug}",
            params={"includeCompensation": "true"},
        )
        response.raise_for_status()
        payload = response.json()
        result = []
        for item in payload.get("jobs", payload.get("jobPostings", [])):
            compensation = item.get("compensation") or {}
            location = item.get("location", "")
            job = CollectedJob(
                source=self.config.id,
                source_job_id=str(item.get("id", item.get("jobUrl", ""))),
                title=item.get("title", ""),
                company=self.config.company or self.config.name,
                location=location,
                description=plain_text(item.get("descriptionHtml") or item.get("description")),
                url=item.get("jobUrl", item.get("applyUrl", "")),
                apply_url=item.get("applyUrl", item.get("jobUrl", "")),
                remote_scope=location if item.get("isRemote") or "remote" in location.casefold() else "",
                employment_type=item.get("employmentType", ""),
                salary_min=compensation.get("minValue"),
                salary_max=compensation.get("maxValue"),
                salary_currency=compensation.get("currencyCode", ""),
                published_at=parse_datetime(item.get("publishedAt")),
                updated_at=parse_datetime(item.get("updatedAt")),
                metadata={"department": item.get("department", ""), "team": item.get("team", "")},
            )
            if is_recent(job, since):
                result.append(job)
        return result


class RemoteOkCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        response = await self.client.get("https://remoteok.com/api")
        response.raise_for_status()
        result = []
        for item in response.json():
            if "id" not in item or "position" not in item:
                continue
            salary_min, salary_max = _salary_pair(item.get("salary_min"), item.get("salary_max"))
            job = CollectedJob(
                source=self.config.id,
                source_job_id=str(item["id"]),
                title=item.get("position", ""),
                company=item.get("company", ""),
                location=item.get("location", "Worldwide") or "Worldwide",
                description=plain_text(item.get("description")),
                url=item.get("url", ""),
                apply_url=item.get("apply_url", item.get("url", "")),
                remote_scope=item.get("location", "Worldwide") or "Worldwide",
                employment_type="full-time",
                salary_min=salary_min,
                salary_max=salary_max,
                salary_currency="USD" if salary_min or salary_max else "",
                published_at=parse_datetime(item.get("date") or item.get("epoch")),
                metadata={"tags": item.get("tags", [])},
            )
            if is_recent(job, since):
                result.append(job)
        return result


def _salary_pair(minimum: Any, maximum: Any) -> tuple[float | None, float | None]:
    try:
        min_value = float(minimum) if minimum not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        min_value = None
    try:
        max_value = float(maximum) if maximum not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        max_value = None
    return min_value, max_value


class HimalayasCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        result: list[CollectedJob] = []
        offset = 0
        for _ in range(25):
            response = await self.client.get(
                "https://himalayas.app/jobs/api",
                params={"limit": 20, "offset": offset},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("jobs", payload if isinstance(payload, list) else [])
            if not items:
                break
            oldest: datetime | None = None
            for item in items:
                published = parse_datetime(item.get("pubDate") or item.get("publishedAt") or item.get("createdAt"))
                if published and (oldest is None or published < oldest):
                    oldest = published
                salary = item.get("salary") or {}
                restrictions = item.get("locationRestrictions") or item.get("location", "")
                if isinstance(restrictions, list):
                    restrictions = ", ".join(map(str, restrictions))
                source_job_id = str(item.get("id") or item.get("guid") or item.get("applicationLink") or item.get("slug") or "")
                if not source_job_id:
                    continue
                job = CollectedJob(
                    source=self.config.id,
                    source_job_id=source_job_id,
                    title=item.get("title", ""),
                    company=(item.get("company") or {}).get("name", "") if isinstance(item.get("company"), dict) else str(item.get("companyName") or item.get("company") or ""),
                    location=str(restrictions or "Worldwide"),
                    description=plain_text(item.get("description") or item.get("descriptionHtml")),
                    url=item.get("applicationLink") or item.get("guid") or item.get("url", ""),
                    apply_url=item.get("applicationLink") or item.get("guid") or item.get("url", ""),
                    remote_scope=str(restrictions or "Worldwide"),
                    employment_type=str(item.get("employmentType", "")),
                    salary_min=(salary.get("min") if isinstance(salary, dict) and salary else item.get("minSalary")),
                    salary_max=(salary.get("max") if isinstance(salary, dict) and salary else item.get("maxSalary")),
                    salary_currency=(salary.get("currency", "") if isinstance(salary, dict) and salary else str(item.get("currency", ""))),
                    published_at=published,
                    updated_at=parse_datetime(item.get("updatedAt")),
                    metadata={
                        "timezone": item.get("timezoneRestrictions", []),
                        "seniority": item.get("seniority", ""),
                        "categories": item.get("categories", []),
                        "company_slug": item.get("companySlug", ""),
                        "salary_period": item.get("salaryPeriod", ""),
                    },
                )
                if is_recent(job, since):
                    result.append(job)
            if len(items) < 20 or (oldest and oldest < since):
                break
            offset += len(items)
        return result


class WwrCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        url = self.config.url or "https://weworkremotely.com/remote-jobs.rss"
        response = await self.client.get(url)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        result = []
        for item in root.findall("./channel/item"):
            link = _xml_text(item, "link")
            title = _xml_text(item, "title")
            company, separator, role = title.partition(":")
            published = parse_datetime(_xml_text(item, "pubDate"))
            job = CollectedJob(
                source=self.config.id,
                source_job_id=_xml_text(item, "guid") or link,
                title=role.strip() if separator else title,
                company=company.strip() if separator else "",
                location="Remote",
                description=plain_text(_xml_text(item, "description")),
                url=link,
                apply_url=link,
                remote_scope="Remote — restrictions unknown",
                published_at=published,
                metadata={"category": [(node.text or "").strip() for node in item.findall("category") if node.text]},
            )
            if is_recent(job, since):
                result.append(job)
        return result


def _xml_text(item: ElementTree.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""


class SmartRecruitersCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        if not self.config.slug:
            raise ValueError("SmartRecruiters source requires a company identifier")
        result = []
        offset = 0
        for _ in range(20):
            response = await self.client.get(
                f"https://api.smartrecruiters.com/v1/companies/{self.config.slug}/postings",
                params={"limit": 100, "offset": offset},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("content", [])
            for summary in items:
                detail_response = await self.client.get(summary.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{self.config.slug}/postings/{summary['id']}")
                detail_response.raise_for_status()
                item = detail_response.json()
                location_value = item.get("location") or {}
                location = ", ".join(filter(None, (location_value.get("city"), location_value.get("region"), location_value.get("country"))))
                sections = item.get("jobAd") or {}
                description = " ".join(
                    plain_text(section.get("text"))
                    for section in sections.get("sections", {}).values()
                    if isinstance(section, dict)
                )
                job = CollectedJob(
                    source=self.config.id,
                    source_job_id=str(item["id"]),
                    title=item.get("name", ""),
                    company=self.config.company or self.config.name,
                    location=location,
                    description=description,
                    url=item.get("ref", summary.get("ref", "")),
                    apply_url=item.get("applyUrl", item.get("ref", "")),
                    remote_scope=location if "remote" in location.casefold() else "",
                    employment_type=(item.get("typeOfEmployment") or {}).get("label", ""),
                    published_at=parse_datetime(item.get("releasedDate")),
                    updated_at=parse_datetime(item.get("lastUpdatedDate")),
                    metadata={"department": (item.get("department") or {}).get("label", "")},
                )
                if is_recent(job, since):
                    result.append(job)
            offset += len(items)
            if not items or offset >= payload.get("totalFound", 0):
                break
        return result


class WorkdayCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        if not all((self.config.host, self.config.tenant, self.config.site)):
            raise ValueError("Workday source requires host, tenant, and site")
        host = self.config.host.rstrip("/")
        base = f"{host}/wday/cxs/{self.config.tenant}/{self.config.site}"
        result = []
        offset = 0
        for _ in range(20):
            response = await self.client.post(
                f"{base}/jobs",
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("jobPostings", [])
            for summary in items:
                external_path = summary.get("externalPath", "")
                detail_response = await self.client.get(f"{base}{external_path}")
                detail_response.raise_for_status()
                item = detail_response.json().get("jobPostingInfo", {})
                url = item.get("externalUrl") or urljoin(f"{host}/", external_path.lstrip("/"))
                job = CollectedJob(
                    source=self.config.id,
                    source_job_id=str(item.get("jobReqId", external_path)),
                    title=item.get("title", summary.get("title", "")),
                    company=self.config.company or self.config.name,
                    location=item.get("location", summary.get("locationsText", "")),
                    description=plain_text(item.get("jobDescription")),
                    url=url,
                    apply_url=url,
                    remote_scope=item.get("location", "") if "remote" in item.get("location", "").casefold() else "",
                    employment_type=item.get("timeType", ""),
                    published_at=parse_datetime(item.get("startDate")),
                    metadata={"job_req_id": item.get("jobReqId", "")},
                )
                if is_recent(job, since):
                    result.append(job)
            offset += len(items)
            if not items or offset >= payload.get("total", 0):
                break
        return result


class ArbeitnowCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        jobs: list[CollectedJob] = []
        requests = 0
        url = self.config.url or "https://www.arbeitnow.com/api/job-board-api"
        filters = {"visa_sponsorship": "true"} if self.config.options.get("visa_sponsorship") else {}
        if filters:
            url = str(httpx.URL(url).copy_merge_params(filters))
        provider_total: int | None = None
        for _ in range(max(1, self.config.max_pages)):
            response = await self.client.get(url)
            requests += 1
            response.raise_for_status()
            payload = response.json()
            provider_total = payload.get("total") or provider_total
            oldest: datetime | None = None
            for item in payload.get("data", []):
                tags = item.get("tags") or []
                location = str(item.get("location") or "Germany")
                remote = bool(item.get("remote"))
                published = parse_datetime(item.get("created_at"))
                if published and (oldest is None or published < oldest):
                    oldest = published
                sponsorship = True if self.config.options.get("visa_sponsorship") else item.get("visa_sponsorship")
                job = CollectedJob(
                    source=self.config.id,
                    source_job_id=str(item.get("slug") or item.get("url")),
                    title=str(item.get("title") or ""),
                    company=str(item.get("company_name") or ""),
                    location=location,
                    description=plain_text(item.get("description")),
                    url=str(item.get("url") or ""),
                    apply_url=str(item.get("url") or ""),
                    remote_scope=location if remote else "",
                    published_at=published,
                    metadata={"tags": tags, **_signals(visa_sponsorship=sponsorship, remote=remote)},
                )
                if is_recent(job, since):
                    jobs.append(job)
            next_url = (payload.get("links") or {}).get("next")
            if not next_url or (oldest and oldest < since):
                break
            url = str(httpx.URL(next_url).copy_merge_params(filters))
        return CollectionBatch(jobs=jobs, complete_snapshot=True, provider_total=provider_total, requests_made=requests)


class JobicyCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        params = {"count": min(100, int(self.config.options.get("count", 100)))}
        if self.config.options.get("industry"):
            params["industry"] = self.config.options["industry"]
        response = await self.client.get(self.config.url or "https://jobicy.com/api/v2/remote-jobs", params=params)
        response.raise_for_status()
        payload = response.json()
        jobs = []
        for item in payload.get("jobs", []):
            location = str(item.get("jobGeo") or "Remote — restrictions unknown")
            salary_min, salary_max = _salary_pair(item.get("annualSalaryMin"), item.get("annualSalaryMax"))
            job = CollectedJob(
                source=self.config.id,
                source_job_id=str(item.get("id") or item.get("url")),
                title=str(item.get("jobTitle") or ""),
                company=str(item.get("companyName") or ""),
                location=location,
                description=plain_text(item.get("jobDescription")),
                url=str(item.get("url") or ""),
                apply_url=str(item.get("url") or ""),
                remote_scope=location,
                employment_type=str(item.get("jobType") or ""),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_currency=str(item.get("salaryCurrency") or ""),
                published_at=parse_datetime(item.get("pubDate")),
                metadata={"industry": item.get("jobIndustry", []), **_signals(remote_region=location)},
            )
            if is_recent(job, since):
                jobs.append(job)
        return CollectionBatch(jobs=jobs, complete_snapshot=True, provider_total=payload.get("jobCount"), requests_made=1)


class RemotiveCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        params = {"category": self.config.options.get("category", "software-dev"), "limit": 100}
        response = await self.client.get(self.config.url or "https://remotive.com/api/remote-jobs", params=params)
        response.raise_for_status()
        payload = response.json()
        jobs = []
        for item in payload.get("jobs", []):
            location = str(item.get("candidate_required_location") or "Remote — restrictions unknown")
            job = CollectedJob(
                source=self.config.id,
                source_job_id=str(item.get("id") or item.get("url")),
                title=str(item.get("title") or ""),
                company=str(item.get("company_name") or ""),
                location=location,
                description=plain_text(item.get("description")),
                url=str(item.get("url") or ""),
                apply_url=str(item.get("url") or ""),
                remote_scope=location,
                employment_type=str(item.get("job_type") or ""),
                published_at=parse_datetime(item.get("publication_date")),
                metadata={"category": item.get("category", ""), "salary": item.get("salary", ""), **_signals(remote_region=location)},
            )
            if is_recent(job, since):
                jobs.append(job)
        return CollectionBatch(
            jobs=jobs, complete_snapshot=True, requests_made=1,
            attribution="Jobs provided by Remotive (https://remotive.com).",
        )


class AdzunaCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        app_id = os.getenv("ADZUNA_APP_ID", "")
        app_key = os.getenv("ADZUNA_APP_KEY", "")
        if not app_id or not app_key:
            raise RuntimeError("Adzuna credentials are not configured")
        country = str(self.config.options.get("country", "de"))
        jobs: list[CollectedJob] = []
        requests = 0
        total: int | None = None
        for page in range(1, max(1, self.config.max_pages) + 1):
            response = await self.client.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}",
                params={"app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": self.config.options.get("query", "software engineer")},
            )
            requests += 1
            response.raise_for_status()
            payload = response.json()
            total = payload.get("count", total)
            items = payload.get("results", [])
            for item in items:
                location = (item.get("location") or {}).get("display_name", "")
                job = CollectedJob(
                    source=self.config.id, source_job_id=str(item.get("id") or item.get("redirect_url")),
                    title=str(item.get("title") or ""), company=str((item.get("company") or {}).get("display_name") or ""),
                    location=location, description=plain_text(item.get("description")),
                    url=str(item.get("redirect_url") or ""), apply_url=str(item.get("redirect_url") or ""),
                    remote_scope=location if "remote" in location.casefold() else "",
                    salary_min=item.get("salary_min"), salary_max=item.get("salary_max"),
                    salary_currency=str(self.config.options.get("currency", "")), published_at=parse_datetime(item.get("created")),
                    metadata={"category": (item.get("category") or {}).get("label", ""), **_signals(contract_time=item.get("contract_time"))},
                )
                if is_recent(job, since):
                    jobs.append(job)
            if len(items) < 50:
                break
        return CollectionBatch(jobs=jobs, provider_total=total, requests_made=requests)


class JoobleCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        api_key = os.getenv("JOOBLE_API_KEY", "")
        if not api_key:
            raise RuntimeError("Jooble credentials are not configured")
        response = await self.client.post(
            f"https://jooble.org/api/{quote(api_key)}",
            json={"keywords": self.config.options.get("query", "software engineer"), "location": self.config.options.get("location", "")},
        )
        response.raise_for_status()
        payload = response.json()
        jobs = []
        for item in payload.get("jobs", []):
            location = str(item.get("location") or "")
            job = CollectedJob(
                source=self.config.id, source_job_id=str(item.get("id") or item.get("link")),
                title=str(item.get("title") or ""), company=str(item.get("company") or ""), location=location,
                description=plain_text(item.get("snippet")), url=str(item.get("link") or ""), apply_url=str(item.get("link") or ""),
                remote_scope=location if "remote" in location.casefold() else "", employment_type=str(item.get("type") or ""),
                published_at=parse_datetime(item.get("updated")), metadata={"salary_text": item.get("salary", "")},
            )
            if is_recent(job, since):
                jobs.append(job)
        return CollectionBatch(jobs=jobs, provider_total=payload.get("totalCount"), requests_made=1)


class SerpApiCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        api_key = os.getenv("SERPAPI_API_KEY", "")
        if not api_key:
            raise RuntimeError("SerpApi credentials are not configured")
        response = await self.client.get(
            "https://serpapi.com/search.json",
            params={"engine": "google_jobs", "q": self.config.options.get("query", "software engineer"), "location": self.config.options.get("location", "Germany"), "api_key": api_key},
        )
        response.raise_for_status()
        payload = response.json()
        jobs = []
        for item in payload.get("jobs_results", []):
            detected = item.get("detected_extensions") or {}
            links = item.get("apply_options") or []
            url = str((links[0].get("link") if links else "") or item.get("share_link") or "")
            job = CollectedJob(
                source=self.config.id, source_job_id=str(item.get("job_id") or url), title=str(item.get("title") or ""),
                company=str(item.get("company_name") or ""), location=str(item.get("location") or ""),
                description=plain_text(item.get("description")), url=url, apply_url=url,
                remote_scope="Remote" if detected.get("work_from_home") else "", employment_type=str(detected.get("schedule_type") or ""),
                published_at=parse_datetime(detected.get("posted_at")), metadata={"via": item.get("via", ""), "extensions": item.get("extensions", [])},
            )
            jobs.append(job)
        return CollectionBatch(jobs=jobs, provider_total=len(jobs), requests_made=1)


class GmailLinkedInCollector(Collector):
    """Reads only a configured Gmail label; it never logs into a job site."""

    async def collect(self, since: datetime, cursor: str = "") -> list[CollectedJob]:
        return await asyncio.to_thread(self._collect_sync, since)

    def _collect_sync(self, since: datetime) -> list[CollectedJob]:
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as error:
            raise RuntimeError("Install RoleBeacon with the gmail extra to enable Gmail alerts") from error

        root = Path(__file__).resolve().parents[2]
        credentials_path = Path(self.config.options.get("credentials_file") or os.getenv("GMAIL_CREDENTIALS_FILE") or root / "data" / "gmail-credentials.json")
        token_path = Path(self.config.options.get("token_file") or os.getenv("GMAIL_TOKEN_FILE") or root / "data" / "gmail-token.json")
        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        credentials = Credentials.from_authorized_user_file(token_path, scopes) if token_path.exists() else None
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            if not credentials_path.exists():
                raise RuntimeError(f"Gmail credentials file not found: {credentials_path}")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
            credentials = flow.run_local_server(port=0)
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        label = self.config.options.get("label") or os.getenv("GMAIL_LABEL", "Job Alerts")
        query = f'label:"{label}" after:{int(since.timestamp())}'
        page_token = None
        message_refs: list[dict[str, Any]] = []
        while True:
            message_list = service.users().messages().list(
                userId="me", q=query, maxResults=100, pageToken=page_token
            ).execute()
            message_refs.extend(message_list.get("messages", []))
            page_token = message_list.get("nextPageToken")
            if not page_token:
                break
        result = []
        for message_ref in message_refs:
            message = service.users().messages().get(userId="me", id=message_ref["id"], format="full").execute()
            body = _gmail_body(message.get("payload", {}))
            urls = extract_job_urls(body)
            subject = next(
                (header["value"] for header in message.get("payload", {}).get("headers", []) if header["name"].casefold() == "subject"),
                "LinkedIn job alert",
            )
            for url in dict.fromkeys(urls):
                clean_url = html.unescape(url).rstrip(".,)")
                result.append(
                    CollectedJob(
                        source=self.config.id,
                        source_job_id=stable_alert_job_id(clean_url),
                        title=subject,
                        company=_alert_company(clean_url),
                        location="",
                        description=plain_text(body)[:8000],
                        url=clean_url,
                        apply_url=clean_url,
                        published_at=parse_datetime(int(message.get("internalDate", "0"))),
                        metadata={"gmail_message_id": message_ref["id"]},
                    )
                )
        return result


def extract_job_urls(body: str) -> list[str]:
    return re.findall(
        r"https?://(?:www\.)?(?:"
        r"linkedin\.com/(?:comm/)?jobs/view|"
        r"google\.com/about/careers/applications/jobs/results|"
        r"careers\.google\.com/jobs/results|"
        r"jobs\.careers\.microsoft\.com/global/en/job|"
        r"amazon\.jobs/(?:en/)?jobs|"
        r"metacareers\.com/jobs|"
        r"jobs\.apple\.com/[^\s/]+/details"
        r")/[^\s<>\"']+",
        html.unescape(body),
        re.IGNORECASE,
    )


def stable_alert_job_id(url: str) -> str:
    clean = html.unescape(url)
    for pattern in (
        r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d+)",
        r"microsoft\.com/global/en/job/(\d+)",
        r"amazon\.jobs/(?:en/)?jobs/(\d+)",
        r"metacareers\.com/jobs/(\d+)",
    ):
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            host = urlsplit(clean).netloc.casefold().removeprefix("www.")
            return f"{host}:{match.group(1)}"
    parts = urlsplit(clean)
    normalized = f"{parts.netloc.casefold()}{parts.path.rstrip('/')}"
    return base64.urlsafe_b64encode(normalized.encode()).decode().rstrip("=")


def _alert_company(url: str) -> str:
    host = httpx.URL(url).host.casefold()
    for needle, company in (
        ("google", "Google"),
        ("microsoft", "Microsoft"),
        ("amazon", "Amazon"),
        ("metacareers", "Meta"),
        ("apple", "Apple"),
        ("linkedin", "LinkedIn alert"),
    ):
        if needle in host:
            return company
    return "Job alert"


def _gmail_body(payload: dict[str, Any]) -> str:
    body = (payload.get("body") or {}).get("data")
    if body:
        return base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode(errors="replace")
    return "\n".join(_gmail_body(part) for part in payload.get("parts", []))


COLLECTORS: dict[str, type[Collector]] = {
    "greenhouse": GreenhouseCollector,
    "lever": LeverCollector,
    "ashby": AshbyCollector,
    "remoteok": RemoteOkCollector,
    "himalayas": HimalayasCollector,
    "wwr": WwrCollector,
    "smartrecruiters": SmartRecruitersCollector,
    "workday": WorkdayCollector,
    "arbeitnow": ArbeitnowCollector,
    "jobicy": JobicyCollector,
    "remotive": RemotiveCollector,
    "adzuna": AdzunaCollector,
    "jooble": JoobleCollector,
    "serpapi": SerpApiCollector,
    "gmail_linkedin": GmailLinkedInCollector,
}


def create_collector(config: SourceConfig, client: httpx.AsyncClient) -> Collector:
    collector_type = COLLECTORS.get(config.kind)
    if collector_type is None:
        raise ValueError(f"Unsupported collector kind: {config.kind}")
    return collector_type(config, client)


def default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8"},
        follow_redirects=True,
        timeout=httpx.Timeout(30, connect=10),
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )
