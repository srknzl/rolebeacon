from __future__ import annotations

import asyncio
import base64
import html
import os
import re
import stat
import tempfile
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
from .source_discovery import amazon_location_matches, amazon_search_params, google_result_links

USER_AGENT = "RoleBeacon/0.2 (+https://github.com/srknzl/rolebeacon)"

MOJIBAKE_REPLACEMENTS = {
    "â€™": "’",
    "â€˜": "‘",
    "â€œ": "“",
    "â€": "”",
    "â€“": "–",
    "â€”": "—",
    "â€¦": "…",
    "Â ": " ",
}


# Site chrome that carries no employer information. Skipping it at extraction time keeps
# navigation, cookie banners, and hidden modals out of job descriptions and company evidence alike.
NON_CONTENT_TAGS = {
    "script", "style", "noscript", "svg", "template", "head", "nav", "footer",
    "aside", "dialog", "form", "button", "select", "iframe",
}
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
# Landmarks and widgets that hold site chrome rather than employer or role information.
NON_CONTENT_ROLES = {
    "navigation", "banner", "contentinfo", "dialog", "alertdialog", "alert",
    "menu", "menubar", "search", "complementary", "tablist", "toolbar",
}
# Popups, cookie bars, and screen-reader-only links are hidden with CSS classes rather than the
# hidden attribute, so structure alone cannot find them. Matching class and id substrings does.
# ponytail: substring list, not a CSS engine — dropping one real paragraph costs less than
# feeding a newsletter modal into scoring as if it were employer evidence.
NON_CONTENT_CLASS_HINTS = (
    "cookie", "popup", "modal", "lightbox", "toast", "notification", "newsletter",
    "skip-link", "skiplink", "breadcrumb", "sr-only", "screen-reader", "screenreader",
    "visually-hidden", "visuallyhidden", "off-screen", "offscreen", "u-hide", "is-hidden",
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        # Skipping tracks nesting of the skipped tag only, so sloppy markup inside it
        # (an unclosed <option>, a stray </div>) cannot end the skip early or late.
        self._skip_tag = ""
        self._skip_depth = 0

    @property
    def skipping(self) -> bool:
        return bool(self._skip_tag)

    def _break(self) -> None:
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    @staticmethod
    def _is_chrome(attrs: list[tuple[str, str | None]]) -> bool:
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            value = str(raw_value or "").casefold()
            if name == "hidden":
                return True
            if name == "aria-hidden" and value == "true":
                return True
            if name == "role" and value in NON_CONTENT_ROLES:
                return True
            if name in {"class", "id"} and any(hint in value for hint in NON_CONTENT_CLASS_HINTS):
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if self.skipping:
            if normalized == self._skip_tag:
                self._skip_depth += 1
            return
        if normalized not in VOID_TAGS and (normalized in NON_CONTENT_TAGS or self._is_chrome(attrs)):
            self._skip_tag = normalized
            self._skip_depth = 1
            return
        if normalized == "li":
            self._break()
            self.parts.append("• ")
        elif normalized in {"br", "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if self.skipping:
            if normalized == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = ""
            return
        if normalized in {"li", "p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._break()

    def handle_data(self, data: str) -> None:
        if self.skipping:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if value:
            if self.parts and self.parts[-1] != "\n" and not self.parts[-1].endswith((" ", "• ")):
                self.parts.append(" ")
            self.parts.append(value)


def plain_text(value: str | None) -> str:
    if not value:
        return ""
    if not re.search(r"<\s*[a-zA-Z][^>]*>", value):
        return _normalize_description_text(html.unescape(value))
    parser = _TextExtractor()
    parser.feed(html.unescape(value))
    return _normalize_description_text("".join(parser.parts))


def _normalize_description_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return repair_text(normalized.strip())


DESCRIPTION_HEADINGS = {
    "about the job", "about the role", "the role", "what you'll do", "what you will do",
    "responsibilities", "your responsibilities", "requirements", "qualifications",
    "minimum qualifications", "preferred qualifications", "basic qualifications",
    "what we offer", "benefits", "skills and experience", "who you are", "nice to have",
}


def description_blocks(value: str | None) -> list[dict[str, Any]]:
    """Return safe semantic blocks without rewriting or summarizing the posting."""
    text = plain_text(value)
    if not text:
        return []
    for heading in sorted(DESCRIPTION_HEADINGS, key=len, reverse=True):
        pattern = re.compile(rf"(?i)\b({re.escape(heading)})\s*:\s*")
        text = pattern.sub(lambda match: f"\n\n{match.group(1).strip()}\n", text)

    blocks: list[dict[str, Any]] = []
    list_items: list[str] = []

    def flush_list() -> None:
        if list_items:
            blocks.append({"kind": "list", "items": list_items.copy()})
            list_items.clear()

    for section in re.split(r"\n{2,}", text):
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        paragraph_lines: list[str] = []
        for line in lines:
            bullet = re.match(r"^[•*\-–]\s+(.+)$", line)
            if bullet:
                if paragraph_lines:
                    flush_list()
                    _append_paragraphs(blocks, " ".join(paragraph_lines))
                    paragraph_lines.clear()
                list_items.append(bullet.group(1).strip())
                continue
            flush_list()
            if _is_description_heading(line):
                if paragraph_lines:
                    _append_paragraphs(blocks, " ".join(paragraph_lines))
                    paragraph_lines.clear()
                blocks.append({"kind": "heading", "text": line.rstrip(":")})
            else:
                paragraph_lines.append(line)
        flush_list()
        if paragraph_lines:
            _append_paragraphs(blocks, " ".join(paragraph_lines))
    return blocks


def _is_description_heading(value: str) -> bool:
    normalized = value.rstrip(":").strip().casefold()
    return normalized in DESCRIPTION_HEADINGS or (value.endswith(":") and len(value) <= 80)


def _append_paragraphs(blocks: list[dict[str, Any]], value: str) -> None:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", value)
    chunk = ""
    for sentence in sentences:
        candidate = f"{chunk} {sentence}".strip()
        if chunk and len(candidate) > 620:
            blocks.append(_paragraph_block(chunk))
            chunk = sentence
        else:
            chunk = candidate
    if chunk:
        blocks.append(_paragraph_block(chunk))


def _paragraph_block(value: str) -> dict[str, Any]:
    block: dict[str, Any] = {"kind": "paragraph", "text": value}
    segments = _safe_markdown_segments(value)
    if any(segment["kind"] != "text" for segment in segments):
        block["segments"] = segments
    return block


def _safe_markdown_segments(value: str) -> list[dict[str, str]]:
    """Parse a small Markdown subset into auto-escaped template data; raw HTML is never trusted."""
    pattern = re.compile(r"(\[[^\]\n]+\]\(https?://[^)\s]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|(?<!\*)\*[^*\n]+\*(?!\*))")
    segments: list[dict[str, str]] = []
    position = 0
    for match in pattern.finditer(value):
        if match.start() > position:
            segments.append({"kind": "text", "text": value[position:match.start()]})
        token = match.group(0)
        link = re.fullmatch(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", token)
        if link:
            segments.append({"kind": "link", "text": link.group(1), "url": link.group(2)})
        elif token.startswith(("**", "__")):
            segments.append({"kind": "strong", "text": token[2:-2]})
        else:
            segments.append({"kind": "emphasis", "text": token[1:-1]})
        position = match.end()
    if position < len(value):
        segments.append({"kind": "text", "text": value[position:]})
    return segments or [{"kind": "text", "text": value}]


def repair_text(value: str | None) -> str:
    """Repair common UTF-8-as-Windows-1252 damage without altering ordinary text."""
    if not value:
        return ""
    repaired = str(value)
    for broken, replacement in MOJIBAKE_REPLACEMENTS.items():
        repaired = repaired.replace(broken, replacement)
    return repaired


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
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
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
            result.append(job)
        return CollectionBatch(jobs=result, complete_snapshot=True, provider_total=len(result))


def _metadata_value(metadata: list[dict[str, Any]], key: str) -> str:
    for item in metadata:
        if str(item.get("name", "")).casefold() == key.casefold():
            return str(item.get("value") or "")
    return ""


class LeverCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        if not self.config.slug:
            raise ValueError("Lever source requires a site slug")
        base = self.config.host.rstrip("/") if self.config.host else "https://api.lever.co"
        result = []
        requests = 0
        page_size = 100
        truncated = False
        for page in range(max(1, self.config.max_pages)):
            response = await self.client.get(
                f"{base}/v0/postings/{self.config.slug}",
                params={"mode": "json", "limit": page_size, "skip": page * page_size},
            )
            requests += 1
            response.raise_for_status()
            items = response.json()
            for item in items:
                categories = item.get("categories") or {}
                salary = item.get("salaryRange") or {}
                location = categories.get("location", "")
                result.append(CollectedJob(
                    source=self.config.id, source_job_id=str(item.get("id", item.get("hostedUrl", ""))),
                    title=item.get("text", ""), company=self.config.company or self.config.name,
                    location=location, description=plain_text(item.get("descriptionPlain") or item.get("description")),
                    url=item.get("hostedUrl", ""), apply_url=item.get("applyUrl", item.get("hostedUrl", "")),
                    remote_scope=location if "remote" in location.casefold() else "",
                    employment_type=categories.get("commitment", ""), salary_min=salary.get("min"),
                    salary_max=salary.get("max"), salary_currency=salary.get("currency", ""),
                    published_at=parse_datetime(item.get("createdAt")), updated_at=parse_datetime(item.get("updatedAt")),
                    metadata={"team": categories.get("team", ""), "department": categories.get("department", "")},
                ))
            if len(items) < page_size:
                break
        else:
            truncated = bool(result) and len(items) == page_size
        return CollectionBatch(
            jobs=result, complete_snapshot=not truncated, provider_total=len(result) if not truncated else None,
            requests_made=requests, truncated=truncated,
        )


class AshbyCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
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
            result.append(job)
        return CollectionBatch(jobs=result, complete_snapshot=True, provider_total=len(result))


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
                location=item.get("location", "") or "",
                description=plain_text(item.get("description")),
                url=item.get("url", ""),
                apply_url=item.get("apply_url", item.get("url", "")),
                remote_scope=item.get("location", "") or "",
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
                raw_company = (
                    (item.get("company") or {}).get("name", "")
                    if isinstance(item.get("company"), dict)
                    else item.get("companyName") or item.get("company") or ""
                )
                company = str(raw_company).strip()
                if company.casefold() in {"", "name"}:
                    company = str(item.get("companySlug") or "").strip()
                job = CollectedJob(
                    source=self.config.id,
                    source_job_id=source_job_id,
                    title=item.get("title", ""),
                    company=company,
                    location=str(restrictions or ""),
                    description=plain_text(item.get("description") or item.get("descriptionHtml")),
                    url=item.get("applicationLink") or item.get("guid") or item.get("url", ""),
                    apply_url=item.get("applicationLink") or item.get("guid") or item.get("url", ""),
                    remote_scope=str(restrictions or ""),
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


class PersonioCollector(Collector):
    """Collect a complete current Personio board from its public, no-key XML feed."""

    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        if not self.config.host:
            raise ValueError("Personio source requires a board host")
        base = self.config.host.rstrip("/")
        response = await self.client.get(f"{base}/xml")
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        result: list[CollectedJob] = []
        for item in root.findall("./position"):
            source_job_id = _xml_text(item, "id")
            if not source_job_id:
                continue
            offices = [_xml_text(item, "office")]
            offices.extend(
                (node.text or "").strip()
                for node in item.findall("./additionalOffices/office")
                if (node.text or "").strip()
            )
            description_parts = []
            for section in item.findall("./jobDescriptions/jobDescription"):
                heading = _xml_text(section, "name")
                value = _xml_text(section, "value")
                description_parts.append(plain_text(f"<h2>{html.escape(heading)}</h2>{value}"))
            public_url = f"{base}/job/{quote(source_job_id)}?display=en"
            result.append(
                CollectedJob(
                    source=self.config.id,
                    source_job_id=source_job_id,
                    title=_xml_text(item, "name"),
                    company=self.config.company or self.config.name,
                    location=", ".join(dict.fromkeys(value for value in offices if value)),
                    description="\n\n".join(value for value in description_parts if value),
                    url=public_url,
                    apply_url=public_url,
                    employment_type=_xml_text(item, "recruitingCategory") or _xml_text(item, "employmentType"),
                    published_at=parse_datetime(_xml_text(item, "createdAt")),
                    metadata={
                        "department": _xml_text(item, "department"),
                        "seniority": _xml_text(item, "seniority"),
                        "schedule": _xml_text(item, "schedule"),
                        "categories": [
                            value
                            for value in (
                                _xml_text(item, "department"),
                                _xml_text(item, "occupationCategory"),
                                _xml_text(item, "keywords"),
                            )
                            if value
                        ],
                    },
                )
            )
        return CollectionBatch(jobs=result, complete_snapshot=True, provider_total=len(result))


class SmartRecruitersCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        if not self.config.slug:
            raise ValueError("SmartRecruiters source requires a company identifier")
        result = []
        offset = 0
        total = 0
        requests = 0
        complete = False
        for _ in range(max(1, self.config.max_pages)):
            response = await self.client.get(
                f"https://api.smartrecruiters.com/v1/companies/{self.config.slug}/postings",
                params={"limit": 100, "offset": offset},
            )
            requests += 1
            response.raise_for_status()
            payload = response.json()
            total = int(payload.get("totalFound", 0))
            items = payload.get("content", [])
            for summary in items:
                detail_response = await self.client.get(summary.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{self.config.slug}/postings/{summary['id']}")
                requests += 1
                detail_response.raise_for_status()
                item = detail_response.json()
                location_value = item.get("location") or {}
                location = ", ".join(filter(None, (location_value.get("city"), location_value.get("region"), location_value.get("country"))))
                sections = item.get("jobAd") or {}
                description = "\n\n".join(
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
                result.append(job)
            offset += len(items)
            if not items or offset >= total:
                complete = True
                break
        return CollectionBatch(
            jobs=result, complete_snapshot=complete, provider_total=total, requests_made=requests,
            truncated=not complete,
        )


class WorkdayCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        if not all((self.config.host, self.config.tenant, self.config.site)):
            raise ValueError("Workday source requires host, tenant, and site")
        host = self.config.host.rstrip("/")
        base = f"{host}/wday/cxs/{self.config.tenant}/{self.config.site}"
        result = []
        offset = 0
        total = 0
        requests = 0
        complete = False
        for _ in range(max(1, self.config.max_pages)):
            response = await self.client.post(
                f"{base}/jobs",
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            )
            requests += 1
            response.raise_for_status()
            payload = response.json()
            total = int(payload.get("total", 0))
            items = payload.get("jobPostings", [])
            for summary in items:
                external_path = summary.get("externalPath", "")
                detail_response = await self.client.get(f"{base}{external_path}")
                requests += 1
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
                result.append(job)
            offset += len(items)
            if not items or offset >= total:
                complete = True
                break
        return CollectionBatch(
            jobs=result, complete_snapshot=complete, provider_total=total, requests_made=requests,
            truncated=not complete,
        )


class GoogleCareersCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        if not self.config.url:
            raise ValueError("Google Careers source requires a filtered search URL")
        jobs: list[CollectedJob] = []
        seen_urls: set[str] = set()
        requests = 0
        truncated = False
        for page_number in range(1, max(1, self.config.max_pages) + 1):
            search_url = str(httpx.URL(self.config.url).copy_set_param("page", str(page_number)))
            try:
                response = await self.client.get(search_url)
                requests += 1
                response.raise_for_status()
            except httpx.HTTPError:
                # Page 1 failing means the source itself is broken - a real error. A later page
                # failing (e.g. a slow-walked/rate-limited request) must not discard every job
                # already found on the pages before it - keep them and stop paginating.
                if page_number == 1:
                    raise
                truncated = True
                break
            links = [item for item in google_result_links(response.text, str(response.url)) if item[0] not in seen_urls]
            if not links:
                break
            for url, title in links:
                seen_urls.add(url)
                try:
                    detail_response = await self.client.get(url)
                    requests += 1
                    detail_response.raise_for_status()
                except httpx.HTTPError:
                    continue  # one flaky job detail page must not cost every job already found
                detail = _google_job_detail(plain_text(detail_response.text), title)
                identifier_match = re.search(r"/jobs/results/(\d+)", url)
                jobs.append(
                    CollectedJob(
                        source=self.config.id,
                        source_job_id=identifier_match.group(1) if identifier_match else stable_alert_job_id(url),
                        title=title,
                        company=self.config.company or "Google",
                        location=detail["location"],
                        description=detail["description"],
                        url=url,
                        apply_url=url,
                        metadata={"official_first_party": True, "careers_system": "google_careers"},
                    )
                )
        else:
            truncated = True
        return CollectionBatch(jobs=jobs, requests_made=requests, attribution="Google Careers", truncated=truncated)


# Some teams (DeepMind, Ads, Pixel...) prepend an "about us" blurb that itself mentions "Google"
# before the real location line, so the search below occasionally grabs a sentence instead of a
# place name. A real location line is short and reads as place names, not prose - reject anything
# containing a lowercase prose word rather than surface a wrong location silently.
_LOCATION_PROSE_WORDS = {"the", "with", "and", "our", "we", "is", "are", "to", "for", "on", "that", "this"}


def _looks_like_location(value: str) -> bool:
    if not value or len(value) > 90 or not re.search(r"[A-Za-z]{2,}", value):
        return False
    # Case-sensitive on purpose: real location text capitalizes every word ("Waterloo, ON,
    # Canada"), so a casefolded check would misfire on state/province codes that happen to
    # spell an English word, like Ontario's "ON" reading as the preposition "on".
    return not any(word in _LOCATION_PROSE_WORDS for word in re.findall(r"[a-zA-Z']+", value))


def _google_job_detail(text: str, title: str) -> dict[str, str]:
    start = text.rfind(title)
    detail = text[start:] if start >= 0 else text
    footer = detail.find("Information collected and processed as part of your Google Careers profile")
    if footer >= 0:
        detail = detail[:footer]
    # Search only after the title: the title itself often contains "Google" (e.g. "..., Google
    # Cloud"), which would otherwise match first. The real location line follows as either the
    # old icon-label markup ("corporate_fare Google place Berlin, Germany bar_chart ...") or the
    # current one (just "Google Berlin, Germany" on its own line) - one pattern covers both since
    # every icon word here is optional and the capture stops at the line break either way.
    location_match = re.search(r"(?:corporate_fare\s+)?Google\s+(?:place\s+)?([^\n]+)", detail[len(title):])
    location = location_match.group(1).split("bar_chart")[0].strip(" ;") if location_match else ""
    if not _looks_like_location(location):
        location = ""
    return {"location": location, "description": detail}


class AmazonJobsCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        if not self.config.url:
            raise ValueError("Amazon Jobs source requires a filtered search URL")
        jobs: list[CollectedJob] = []
        requests = 0
        page_size = 100
        truncated = False
        provider_total = 0
        for page_number in range(max(1, self.config.max_pages)):
            try:
                response = await self.client.get(
                    "https://www.amazon.jobs/en/search.json",
                    params=amazon_search_params(
                        self.config.url, page_number * page_size, page_size,
                        str(self.config.options.get("location_filter_code", "")),
                    ),
                )
                requests += 1
                response.raise_for_status()
            except httpx.HTTPError:
                # Same reasoning as Google: the first page failing is a real error, but a later
                # page failing must not discard the jobs already found on earlier pages.
                if page_number == 0:
                    raise
                truncated = True
                break
            payload = response.json()
            provider_total = int(payload.get("hits", 0))
            provider_items = payload.get("jobs", [])
            items = [item for item in provider_items if amazon_location_matches(item, self.config)]
            for item in items:
                published = parse_datetime(item.get("posted_date")) or _month_date(item.get("posted_date"))
                description = "\n\n".join(
                    f"{heading}\n{plain_text(str(item.get(key, '')))}"
                    for key, heading in (
                        ("description", "About the job"),
                        ("basic_qualifications", "Basic qualifications"),
                        ("preferred_qualifications", "Preferred qualifications"),
                    )
                    if item.get(key)
                )
                job = CollectedJob(
                    source=self.config.id,
                    source_job_id=str(item.get("id_icims") or item.get("id") or item.get("job_path", "")),
                    title=str(item.get("title", "")).strip(),
                    company=self.config.company or "Amazon",
                    location=str(item.get("location", "")),
                    description=description,
                    url=urljoin("https://www.amazon.jobs", str(item.get("job_path", ""))),
                    apply_url=urljoin("https://www.amazon.jobs", str(item.get("job_path", ""))),
                    employment_type=str(item.get("job_schedule_type", "")),
                    published_at=published,
                    updated_at=parse_datetime(item.get("updated_time")),
                    metadata={
                        "official_first_party": True,
                        "business_category": item.get("business_category", ""),
                        "job_category": item.get("job_category", ""),
                    },
                )
                if is_recent(job, since):
                    jobs.append(job)
            if not provider_items or (page_number + 1) * page_size >= provider_total:
                break
        else:
            truncated = provider_total > max(1, self.config.max_pages) * page_size
        return CollectionBatch(
            jobs=jobs, provider_total=provider_total, requests_made=requests,
            attribution="Amazon Jobs", truncated=truncated,
        )


def _month_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


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
        return CollectionBatch(jobs=jobs, provider_total=provider_total, requests_made=requests)


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
        return CollectionBatch(jobs=jobs, provider_total=payload.get("jobCount"), requests_made=1)


class RemotiveCollector(Collector):
    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        params: dict[str, Any] = {"limit": 100}
        category = str(self.config.options.get("category", "")).strip()
        if category:
            params["category"] = category
        search = str(self.config.options.get("search", "")).strip()
        if search:
            params["search"] = search
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
            jobs=jobs, requests_made=1,
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
                params={"app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": self.config.options.get("query", "")},
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
            json={"keywords": self.config.options.get("query", ""), "location": self.config.options.get("location", "")},
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
            params={"engine": "google_jobs", "q": self.config.options.get("query", ""), "location": self.config.options.get("location", "Germany"), "api_key": api_key},
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

        configured_data_dir = str(self.config.options.get("data_dir") or "").strip()
        if not configured_data_dir:
            raise RuntimeError("Gmail collector requires the configured application-data directory")
        data_dir = Path(configured_data_dir).expanduser()
        credentials_path = Path(self.config.options.get("credentials_file") or os.getenv("GMAIL_CREDENTIALS_FILE") or data_dir / "gmail-credentials.json")
        token_path = Path(self.config.options.get("token_file") or os.getenv("GMAIL_TOKEN_FILE") or data_dir / "gmail-token.json")
        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
        credentials = Credentials.from_authorized_user_file(token_path, scopes) if token_path.exists() else None
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            if not credentials_path.exists():
                raise RuntimeError(f"Gmail credentials file not found: {credentials_path}")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes)
            credentials = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".gmail-token.", dir=token_path.parent)
            try:
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
                with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
                    token_file.write(credentials.to_json())
                    token_file.flush()
                    os.fsync(token_file.fileno())
                os.replace(temporary_name, token_path)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
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
    "personio": PersonioCollector,
    "smartrecruiters": SmartRecruitersCollector,
    "workday": WorkdayCollector,
    "google_careers": GoogleCareersCollector,
    "amazon_jobs": AmazonJobsCollector,
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
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=20),
    )
