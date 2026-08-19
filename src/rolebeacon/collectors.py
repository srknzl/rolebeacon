from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import blake2s
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from .domain import CollectedJob, CollectionBatch, SourceConfig
from .source_discovery import amazon_location_matches, amazon_search_params, google_result_links

log = logging.getLogger(__name__)

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
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError("Greenhouse returned an invalid jobs payload")
        result = []
        for item in payload["jobs"]:
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
        if not isinstance(payload, dict):
            raise ValueError("Ashby returned an invalid job-board payload")
        items = payload.get("jobs", payload.get("jobPostings"))
        if not isinstance(items, list):
            raise ValueError("Ashby returned an invalid jobs payload")
        result = []
        for item in items:
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
                    # Himalayas occasionally returns the literal placeholder "name" for `company`
                    # instead of the real value; companySlug is still the actual employer, just
                    # hyphenated, so title-case it into a readable stand-in company name.
                    company = str(item.get("companySlug") or "").strip().replace("-", " ").title()
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
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as error:
            raise ValueError("Personio returned invalid XML") from error
        if root.tag.rsplit("}", 1)[-1] != "workzag-jobs":
            raise ValueError("Personio returned an unexpected XML payload")
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
        summaries: list[dict[str, Any]] = []
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
            summaries.extend(items)
            offset += len(items)
            if not items or offset >= total:
                complete = True
                break
        # The listing endpoint has no full description, so every posting needs its own detail
        # request. A large board (hundreds of open roles at a company like Bosch) made that a
        # multi-minute sync when fetched one at a time; bound the concurrency instead of either
        # serializing it or opening hundreds of connections at once.
        semaphore = asyncio.Semaphore(10)

        async def fetch_detail(summary: dict[str, Any]) -> CollectedJob:
            nonlocal requests
            async with semaphore:
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
            return CollectedJob(
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

        jobs = list(await asyncio.gather(*(fetch_detail(summary) for summary in summaries)))
        return CollectionBatch(
            jobs=jobs, complete_snapshot=complete, provider_total=total, requests_made=requests,
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
                        source_job_id=identifier_match.group(1) if identifier_match else _stable_job_id_from_url(url),
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


def _stable_job_id_from_url(url: str) -> str:
    # Fallback source_job_id for the rare listing whose URL doesn't carry a numeric job ID.
    parts = urlsplit(html.unescape(url))
    normalized = f"{parts.netloc.casefold()}{parts.path.rstrip('/')}"
    return base64.urlsafe_b64encode(normalized.encode()).decode().rstrip("=")


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


# --- LinkedIn -----------------------------------------------------------------------------
# RoleBeacon reads only the same pages a signed-out visitor sees: LinkedIn's credential-free
# guest search and posting fragments. It never signs in, never sends a cookie, and never touches
# an authenticated page, a profile, a connection, or a message.

LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

# Verified against the live endpoint: start=975 returns an empty body and start=1000 returns
# HTTP 400. No amount of pacing gets past it - only a narrower f_TPR window reaches older jobs.
LINKEDIN_RESULT_CEILING = 1000
# LinkedIn's shortest documented recency filter is an hour; anything smaller is rejected.
LINKEDIN_MINIMUM_TIME_FILTER_SECONDS = 3600

# Pacing, applied by _linkedin_gate() to every request from every LinkedIn source at once.
# Randomized so a run never produces the same request rhythm twice. The posting delay is
# measured, not guessed: against the live endpoint ~1s spacing draws HTTP 429 after about ten
# postings, while 3s spacing completed an 18-posting run untouched. Slower is the right trade here
# - the walk is resumable and unattended, so a rate that avoids the limiter beats one that races
# into it and then waits out a minute-long penalty.
LINKEDIN_POSTING_DELAY_RANGE = (2.5, 4.5)
LINKEDIN_BREAK_AFTER_RANGE = (450, 550)
LINKEDIN_BREAK_DURATION_RANGE = (240.0, 300.0)
LINKEDIN_HEARTBEAT_SECONDS = 60
LINKEDIN_RATE_LIMIT_BACKOFF_SECONDS = 60.0
# LinkedIn's guest search intermittently answers a perfectly good query with HTTP 500 and serves
# the same query fine seconds later, so a blip must not end a walk with hours of progress behind
# it. Retried far sooner than a rate limit, which needs real waiting to clear.
LINKEDIN_SERVER_ERROR_BACKOFF_SECONDS = 5.0
# Backing off costs a minute; abandoning the walk costs the rest of the search. Retry a few times.
LINKEDIN_RATE_LIMIT_ATTEMPTS = 3
# LinkedIn publishes no rate budget, and the measured one is neither the delay range above nor a
# constant: a real run held ~16 postings a minute for four and a half minutes, spent the next five
# trading a 60s penalty for every three or four postings, then recovered to its old rate without
# being asked. A fixed number cannot describe that, so the spacing widens on every rate limit and
# eases back with every request LinkedIn serves, letting a walk settle at whatever it is allowing
# right now instead of at whatever it allowed an hour ago.
LINKEDIN_PACE_WIDENING = 1.5
LINKEDIN_PACE_RELAXATION = 0.99
LINKEDIN_PACE_CEILING_SECONDS = 45.0
# An empty result page is not proof that a search is exhausted. In a real run LinkedIn answered
# start=250 with an empty body and ended two walks that were nowhere near finished; asked again
# unhurried, that same offset served a full page, as did 260 and 400. Under load LinkedIn says
# "nothing here" rather than 429, so an empty page is slept on and asked again before it is
# believed - the cost of being wrong is silently dropping most of a location's postings.
LINKEDIN_EMPTY_PAGE_ATTEMPTS = 2


async def _linkedin_pause(seconds: float) -> None:
    """Single sleep seam so tests can run the pacing logic without waiting for it."""
    await asyncio.sleep(seconds)


_LINKEDIN_PACE = asyncio.Lock()
_linkedin_ready_at = 0.0
_linkedin_pace_scale = 1.0


def linkedin_widen_pace() -> float:
    """Slow every LinkedIn source down after LinkedIn pushes back. Returns the new scale."""
    global _linkedin_pace_scale
    ceiling = LINKEDIN_PACE_CEILING_SECONDS / LINKEDIN_POSTING_DELAY_RANGE[1]
    _linkedin_pace_scale = min(_linkedin_pace_scale * LINKEDIN_PACE_WIDENING, ceiling)
    return _linkedin_pace_scale


def linkedin_relax_pace() -> float:
    """Ease back toward the base rhythm while LinkedIn keeps answering. Never faster than base."""
    global _linkedin_pace_scale
    _linkedin_pace_scale = max(_linkedin_pace_scale * LINKEDIN_PACE_RELAXATION, 1.0)
    return _linkedin_pace_scale


async def _linkedin_gate() -> None:
    """Hold every LinkedIn request to one shared rhythm, however many sources are walking.

    The delay range is per posting, but sources sync concurrently: four location rows pacing
    themselves independently would quadruple the rate LinkedIn actually sees, landing right at
    the ~1s spacing that was measured to draw 429s. One clock for the host, not one per source.
    """
    global _linkedin_ready_at
    async with _LINKEDIN_PACE:
        waiting = _linkedin_ready_at - time.monotonic()
        if waiting > 0:
            await _linkedin_pause(waiting)
        _linkedin_ready_at = time.monotonic() + random.uniform(*LINKEDIN_POSTING_DELAY_RANGE) * _linkedin_pace_scale


def linkedin_posting_url(job_id: str) -> str:
    """The stable canonical URL for a posting.

    Search cards link to a country-specific host with tracking parameters (no.linkedin.com/...
    ?refId=...), so two sources can return the same job under two different URLs. Deriving the
    URL from the job ID instead keeps deduplication working across sources.
    """
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def linkedin_time_filter(since: datetime, now: datetime | None = None) -> str:
    """LinkedIn's f_TPR recency filter covering everything back to `since`."""
    elapsed = int(((now or datetime.now(UTC)) - since).total_seconds())
    return f"r{max(elapsed, LINKEDIN_MINIMUM_TIME_FILTER_SECONDS)}"


def linkedin_search_params(config: SourceConfig, keywords: str, since: datetime, start: int) -> dict[str, Any]:
    params: dict[str, Any] = {
        "keywords": keywords,
        "location": str(config.options.get("location", "")),
        "start": start,
        "f_TPR": linkedin_time_filter(since),
    }
    if config.options.get("remote"):
        params["f_WT"] = "2"
    return params


def linkedin_role_queries(config: SourceConfig) -> list[str]:
    """The searches this source walks, one per target role.

    LinkedIn ranks and caps each query on its own, so five roles OR'd into one string share a
    single 1,000-result ceiling and a single relevance ordering - the roles at the end of the OR
    are the ones that lose. Walked separately they get a ceiling each, and each role's own best
    matches are reached rather than whatever the combined query happened to rank first.

    A source whose keywords were written by hand stays one query: splitting an expression like
    "java AND (kafka OR pulsar)" on OR would produce two broken searches.
    """
    stored = config.options.get("role_queries")
    queries = [str(value).strip() for value in stored if str(value).strip()] if isinstance(stored, list) else []
    return queries or [str(config.options.get("keywords", "")).strip()]


def linkedin_query_fingerprint(config: SourceConfig) -> str:
    """Identify the searches a saved position belongs to.

    Deliberately excludes f_TPR, which is derived from the incremental sync window and so differs
    on every run; including it would discard a usable offset every time. The role list is included
    because a position means nothing once the searches it counted through have changed.
    """
    key = "|".join([
        *linkedin_role_queries(config),
        str(config.options.get("location", "")),
        str(config.options.get("remote", "")),
    ])
    return blake2s(key.encode(), digest_size=6).hexdigest()


def linkedin_parse_cursor(cursor: str, fingerprint: str) -> tuple[int, int]:
    """Which role search to resume and at which offset, or the start when the cursor is unusable."""
    stored, _, position = str(cursor).partition(":")
    index, separator, offset = position.partition(":")
    if not separator or stored != fingerprint:
        return 0, 0
    try:
        resume = (int(index), int(offset))
    except ValueError:
        return 0, 0
    if resume[0] < 0 or not 0 <= resume[1] < LINKEDIN_RESULT_CEILING:
        return 0, 0
    return resume


class _JobCardParser(HTMLParser):
    """One dict per result card in a guest search fragment."""

    _FIELDS = {
        "base-search-card__title": "title",
        "base-search-card__subtitle": "company",
        "job-search-card__location": "location",
    }

    def __init__(self) -> None:
        super().__init__()
        self.cards: list[dict[str, str]] = []
        self._card: dict[str, str] | None = None
        self._field = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): unescape(value or "") for key, value in attrs}
        urn = values.get("data-entity-urn", "")
        if urn.startswith("urn:li:jobPosting:"):
            self._card = {"id": urn.rsplit(":", 1)[-1]}
            self._field = ""
            self.cards.append(self._card)
            return
        if self._card is None:
            return
        for token in values.get("class", "").split():
            if token in self._FIELDS:
                self._field = self._FIELDS[token]
                return
        if tag.casefold() == "time" and values.get("datetime"):
            self._card.setdefault("datetime", values["datetime"])

    def handle_endtag(self, tag: str) -> None:
        # The company name sits in an <a> nested inside the subtitle <h4>, so only the
        # field-bearing containers end a capture.
        if tag.casefold() in {"h3", "h4", "span", "div"}:
            self._field = ""

    def handle_data(self, data: str) -> None:
        if self._card is None or not self._field:
            return
        value = data.strip()
        if value:
            self._card[self._field] = f"{self._card.get(self._field, '')} {value}".strip()


def linkedin_parse_cards(document: str) -> list[dict[str, str]]:
    parser = _JobCardParser()
    parser.feed(document)
    return [card for card in parser.cards if card.get("title")]


class _DescriptionParser(HTMLParser):
    """Re-emit the HTML of the description subtree so plain_text() can format it.

    The posting fragment also carries the top card (title, company, location), which would
    otherwise be prepended to every description and scored as if the employer had written it.
    """

    _MARKER = "show-more-less-html__markup"

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._tag = ""
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._tag:
            values = {key.casefold(): value or "" for key, value in attrs}
            if self._MARKER in values.get("class", "").split():
                self._tag = tag.casefold()
                self._depth = 1
            return
        if tag.casefold() == self._tag:
            self._depth += 1
        self.parts.append(self.get_starttag_text() or f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._tag:
            self.parts.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if not self._tag:
            return
        if tag.casefold() == self._tag:
            self._depth -= 1
            if self._depth <= 0:
                self._tag = ""
                return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._tag:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._tag:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._tag:
            self.parts.append(f"&#{name};")


def linkedin_parse_description(document: str) -> str:
    parser = _DescriptionParser()
    parser.feed(document)
    return plain_text("".join(parser.parts))


def linkedin_build_job(config: SourceConfig, card: dict[str, str], description: str) -> CollectedJob | None:
    """A posting with no employer is unusable - upsert_job rejects an empty company identity."""
    company = card.get("company", "").strip()
    if not company:
        return None
    location = card.get("location", "").strip()
    remote = bool(config.options.get("remote"))
    return CollectedJob(
        source=config.id,
        source_job_id=card["id"],
        title=card.get("title", "").strip(),
        company=company,
        location=location,
        description=description,
        url=linkedin_posting_url(card["id"]),
        apply_url=linkedin_posting_url(card["id"]),
        remote_scope=(location or "Remote — restrictions unknown") if remote or "remote" in location.casefold() else "",
        published_at=parse_datetime(card.get("datetime")),
        metadata={"linkedin_job_id": card["id"]},
    )


def _linkedin_duration(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m" if minutes else f"{remainder}s"


class _LinkedInProgress:
    """Rate-limited progress reporting: at least once a minute, and on every state change."""

    def __init__(self, label: str):
        self.label = label
        self.started = time.monotonic()
        self._last = 0.0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def announce(self, message: str) -> None:
        """A state change: always reported, and does not reset the heartbeat clock."""
        log.info("%s: %s", self.label, message)

    def beat(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last >= LINKEDIN_HEARTBEAT_SECONDS:
            self._last = now
            log.info("%s: %s", self.label, message)


async def _linkedin_take_break(progress: _LinkedInProgress, collected: int) -> None:
    """Pause between long stretches, still reporting once a minute while paused."""
    remaining = random.uniform(*LINKEDIN_BREAK_DURATION_RANGE)
    progress.beat(f"pausing {_linkedin_duration(remaining)} after {collected:,} postings")
    while remaining > 0:
        await _linkedin_pause(min(LINKEDIN_HEARTBEAT_SECONDS, remaining))
        remaining -= LINKEDIN_HEARTBEAT_SECONDS
        if remaining > 0:
            progress.announce(f"still pausing, {_linkedin_duration(remaining)} left")


class LinkedInCollector(Collector):
    """Walk a public LinkedIn job search, reading every posting it returns.

    The walk is unbounded: it runs until the search is exhausted, LinkedIn's result ceiling is
    reached, or the user stops it. Interrupting is safe - the offset reached is returned as the
    batch cursor, so the next run continues instead of re-reading what it already has.
    """

    stopped_message = "stopped by LinkedIn"
    attribution = "Jobs collected from LinkedIn's public job search (https://www.linkedin.com/jobs)."

    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        queries = linkedin_role_queries(self.config)
        if not queries[0]:
            raise ValueError("LinkedIn source requires search keywords")
        fingerprint = linkedin_query_fingerprint(self.config)
        index, start = linkedin_parse_cursor(cursor, fingerprint)
        if index >= len(queries):  # the role list shrank after the position was saved
            index, start = 0, 0
        progress = _LinkedInProgress(self.config.name or self.config.id)
        jobs: list[CollectedJob] = []
        # One posting matches several of the role searches - a "Senior Backend Engineer" listing
        # answers both that query and "Backend Engineer" - and each read costs a paced request.
        # Deduplication downstream would still store it once, but only after paying for it twice.
        read: set[str] = set()
        requests = 0
        checkpoint = ""
        seen = 0
        empty_pages = 0
        next_break = random.randint(*LINKEDIN_BREAK_AFTER_RANGE)
        location = str(self.config.options.get("location", "")) or "anywhere"

        if (index, start) != (0, 0):
            progress.announce(
                f"continuing from posting {start:,} of role search {index + 1} of {len(queries)}, "
                f"\"{queries[index]}\"; collecting everything posted in the last "
                f"{_linkedin_duration((datetime.now(UTC) - since).total_seconds())}")
        else:
            progress.announce(
                f"starting from scratch; {len(queries)} role search{'' if len(queries) == 1 else 'es'} "
                f"in \"{location}\", beginning with \"{queries[0]}\"")

        try:
            while index < len(queries):
                if start >= LINKEDIN_RESULT_CEILING:
                    progress.announce(
                        f"reached LinkedIn's {LINKEDIN_RESULT_CEILING:,}-result ceiling "
                        f"for \"{queries[index]}\"")
                    index, start, empty_pages = index + 1, 0, 0
                    continue
                response = await self._get(
                    LINKEDIN_SEARCH_URL, progress,
                    params=linkedin_search_params(self.config, queries[index], since, start),
                )
                requests += 1
                if response is None:
                    checkpoint = self.stopped_message
                    break
                if response.status_code == httpx.codes.BAD_REQUEST:
                    progress.announce(
                        f"reached LinkedIn's {LINKEDIN_RESULT_CEILING:,}-result ceiling "
                        f"for \"{queries[index]}\"")
                    index, start, empty_pages = index + 1, 0, 0
                    continue
                response.raise_for_status()
                cards = linkedin_parse_cards(response.text)
                if not cards:
                    if empty_pages < LINKEDIN_EMPTY_PAGE_ATTEMPTS:
                        empty_pages += 1
                        linkedin_widen_pace()
                        log.warning(
                            "%s: empty result page at posting %s, which LinkedIn also serves when it "
                            "wants a slower client; waiting %s and asking again (%d of %d)",
                            progress.label, f"{start:,}", _linkedin_duration(LINKEDIN_RATE_LIMIT_BACKOFF_SECONDS),
                            empty_pages, LINKEDIN_EMPTY_PAGE_ATTEMPTS,
                        )
                        await _linkedin_pause(LINKEDIN_RATE_LIMIT_BACKOFF_SECONDS)
                        continue
                    progress.announce(
                        f"finished \"{queries[index]}\" — no more results "
                        f"(role search {index + 1} of {len(queries)}, {len(jobs):,} postings so far)")
                    index, start, empty_pages = index + 1, 0, 0
                    continue
                empty_pages = 0
                for card in cards:
                    # start counts postings finished with, and is only advanced at the end of the
                    # iteration: an interrupted walk then resumes at the posting it never read
                    # rather than one past it.
                    published = parse_datetime(card.get("datetime"))
                    readable = published is None or published >= since
                    if readable and not card.get("company", "").strip():
                        # Decided from the card so an unusable posting costs no extra request:
                        # upsert_job rejects an empty company identity anyway.
                        log.warning("%s: posting %s has no employer name; skipping", progress.label, card["id"])
                        readable = False
                    if readable and card["id"] in read:
                        readable = False  # an earlier role search in this run already read it
                    if readable:
                        read.add(card["id"])
                        job, fetched = await self._read_posting(card, progress, since, len(jobs))
                        requests += fetched
                        if job is None and not fetched:
                            checkpoint = self.stopped_message
                            break
                        if job is not None:
                            jobs.append(job)
                        seen += 1
                        if seen >= next_break:
                            seen = 0
                            next_break = random.randint(*LINKEDIN_BREAK_AFTER_RANGE)
                            await _linkedin_take_break(progress, len(jobs))
                    start += 1
                if checkpoint:
                    break
        except asyncio.CancelledError:
            checkpoint = "stopped"

        if checkpoint:
            progress.announce(
                f"{checkpoint} after {len(jobs):,} postings; will resume from posting {start:,} "
                f"of role search {index + 1} of {len(queries)}")
        else:
            progress.announce(f"finished — {len(jobs):,} postings from {len(queries)} role search"
                              f"{'' if len(queries) == 1 else 'es'}")
        return CollectionBatch(
            jobs=jobs,
            cursor=f"{fingerprint}:{index}:{start}" if checkpoint else "",
            complete_snapshot=False,
            requests_made=requests,
            attribution=self.attribution,
            truncated=bool(checkpoint),
        )

    async def _read_posting(
        self, card: dict[str, str], progress: _LinkedInProgress, since: datetime, collected: int
    ) -> tuple[CollectedJob | None, int]:
        """Return the built job and how many requests it cost; (None, 0) means rate-limited."""
        progress.announce(
            f"{collected:,} collected in {_linkedin_duration(progress.elapsed)}; now reading "
            f"\"{card.get('title', 'untitled')}\" at {card.get('company', 'unknown employer')}"
        )
        response = await self._get(LINKEDIN_POSTING_URL.format(job_id=card["id"]), progress)
        if response is None:
            return None, 0
        if response.status_code != httpx.codes.OK:
            log.warning(
                "%s: could not read posting %s (HTTP %s); skipping",
                progress.label, card["id"], response.status_code,
            )
            return None, 1
        job = linkedin_build_job(self.config, card, linkedin_parse_description(response.text))
        return (job if job is not None and is_recent(job, since) else None), 1

    async def _get(self, url: str, progress: _LinkedInProgress, params: dict[str, Any] | None = None) -> httpx.Response | None:
        """Fetch, waiting out rate limits and transient server errors.

        None means give up, so the caller can checkpoint rather than lose the walk so far.
        """
        for attempt in range(1, LINKEDIN_RATE_LIMIT_ATTEMPTS + 1):
            await _linkedin_gate()
            response = await self.client.get(url, params=params)
            limited = response.status_code == httpx.codes.TOO_MANY_REQUESTS
            if not limited and not response.is_server_error:
                linkedin_relax_pace()
                return response
            if attempt == LINKEDIN_RATE_LIMIT_ATTEMPTS:
                return None
            backoff = attempt * (
                LINKEDIN_RATE_LIMIT_BACKOFF_SECONDS if limited else LINKEDIN_SERVER_ERROR_BACKOFF_SECONDS
            )
            paced = ""
            if limited:
                scale = linkedin_widen_pace()
                low, high = (value * scale for value in LINKEDIN_POSTING_DELAY_RANGE)
                paced = f", and slowing every LinkedIn source to one request per {low:.0f}-{high:.0f}s"
            log.warning(
                "%s: LinkedIn answered HTTP %s; waiting %s before retry %d of %d%s",
                progress.label, response.status_code, _linkedin_duration(backoff),
                attempt, LINKEDIN_RATE_LIMIT_ATTEMPTS - 1, paced,
            )
            await _linkedin_pause(backoff)
        return None


# The signed-in walk. LinkedIn's credential-free endpoints are the default path, but they throttle
# hard - a measured run sustained ~16 postings a minute for four and a half minutes, then paid a
# 60s penalty for every three or four postings after that. This collector trades away the "no
# account" property to get a usable rate, and nothing else: it opens job search results and job
# postings only, never a profile, connection list, feed, or message, and it never signs in on the
# user's behalf. The window opens, the user signs in, the walk starts.
LINKEDIN_LOGIN_TIMEOUT_SECONDS = 300.0
LINKEDIN_BROWSER_TIMEOUT_MS = 45000
LINKEDIN_HOME_URL = "https://www.linkedin.com/"
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
# LinkedIn's session cookie. Only its presence is ever tested; RoleBeacon neither reads its value
# nor types a credential - the user signs in themselves in the window, and Chrome keeps the session.
LINKEDIN_SESSION_COOKIE = "li_at"
# Where a posting's description lives. LinkedIn is midway through a rewrite: the signed-out page
# and the signed-in search pane are the old server-rendered markup, while a signed-in
# /jobs/view/<id> page is a hydrated application whose class names are content hashes. The one
# stable hook it offers is the section id, which carries the job's own ID. Hydration is also why
# the walk waits for this to appear: a first signed-in run read 35 postings a fraction of a second
# after domcontentloaded and got an empty shell every time.
LINKEDIN_POSTING_BODY = (
    '#job-details, [id^="JobDetails_AboutTheJob_"], .jobs-description__content, '
    ".jobs-box__html-content, .show-more-less-html__markup"
)
LINKEDIN_POSTING_WAIT_MS = 15000
LINKEDIN_BROWSER_CLOSE_SECONDS = 10.0
# Enough text to tell a rendered posting from the section heading that arrives ahead of it. A real
# description runs to thousands of characters; the empty shell is one heading of about twenty.
LINKEDIN_DESCRIPTION_MINIMUM_CHARS = 120
LINKEDIN_FILLED_SCRIPT = """() => {
  const node = document.querySelector('__BODY__');
  return Boolean(node) && (node.innerText || '').trim().length > __MINIMUM__;
}""".replace("__BODY__", LINKEDIN_POSTING_BODY).replace("__MINIMUM__", str(LINKEDIN_DESCRIPTION_MINIMUM_CHARS))
# Which container answered is logged once per run, with the job ID trimmed off an id so the line
# stays the same from posting to posting. A LinkedIn redesign then shows up as a change in the log
# rather than as postings that quietly arrive empty.
LINKEDIN_DESCRIPTION_SCRIPT = """() => {
  const node = document.querySelector('__BODY__');
  if (!node) return {via: '', html: ''};
  const id = (node.id || '').replace(/_?\\d+$/, '');
  return {via: id || (node.className || '').split(' ')[0] || node.tagName.toLowerCase(), html: node.innerHTML};
}""".replace("__BODY__", LINKEDIN_POSTING_BODY)


class LinkedInBrowserCollector(LinkedInCollector):
    """The same walk, reading each description from the user's own signed-in browser session.

    Only the description comes from the browser. Titles, employers, locations, and posting dates
    keep coming from the public search cards, which are English and carry ISO dates. LinkedIn
    renders its own chrome in the account's display language - a session set to Turkish reports a
    London job as "Birleşik Krallık", and an eligibility gate cannot read that. The account's
    language is the user's setting to make, not something a collector should work around.

    Interactive by construction: it opens a window and may wait on a human to sign in, so sync.py
    only ever runs it for a manual refresh, never for the background scheduler.
    """

    stopped_message = "stopped by the browser"
    attribution = "Jobs collected from LinkedIn job search in the user's own signed-in session."

    def __init__(self, config: SourceConfig, client: httpx.AsyncClient):
        super().__init__(config, client)
        self._page: Any = None
        self._reported = False

    async def collect(self, since: datetime, cursor: str = "") -> CollectionBatch:
        async with _linkedin_browser(_LinkedInProgress(self.config.name or self.config.id)) as page:
            self._page = page
            return await super().collect(since, cursor)

    async def _read_posting(
        self, card: dict[str, str], progress: _LinkedInProgress, since: datetime, collected: int
    ) -> tuple[CollectedJob | None, int]:
        progress.announce(
            f"{collected:,} collected in {_linkedin_duration(progress.elapsed)}; now reading "
            f"\"{card.get('title', 'untitled')}\" at {card.get('company', 'unknown employer')}"
        )
        try:
            await _linkedin_gate()
            await _linkedin_wait_for_login(self._page, progress)
            await self._page.goto(linkedin_posting_url(card["id"]), wait_until="domcontentloaded",
                                  timeout=LINKEDIN_BROWSER_TIMEOUT_MS)
            await _linkedin_wait_for_posting(self._page)
            found = dict(await self._page.evaluate(LINKEDIN_DESCRIPTION_SCRIPT))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # the window was closed, or a page never loaded
            progress.announce(f"stopped after {type(error).__name__}: {error}")
            return None, 0
        if not self._reported:
            self._reported = True
            progress.announce(f"reading postings from {found.get('via') or 'nothing'}")
        description = plain_text(str(found.get("html", "")))
        if not description:
            # Loud rather than silent: an empty description means LinkedIn moved the markup, and
            # every later posting in the run would arrive just as empty.
            log.warning("%s: posting %s returned no description; skipping", progress.label, card["id"])
            return None, 1
        job = linkedin_build_job(self.config, card, description)
        return (job if job is not None and is_recent(job, since) else None), 1


@asynccontextmanager
async def _linkedin_browser(progress: _LinkedInProgress) -> AsyncIterator[Any]:
    """A headed Chrome on its own profile, so the LinkedIn session can be dropped on its own."""
    from playwright.async_api import async_playwright

    from .config import Settings

    profile = Settings.load().data_dir / "linkedin-profile"
    profile.mkdir(parents=True, exist_ok=True)
    progress.announce("opening Chrome")
    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        str(profile), headless=False, channel="chrome"
    )
    try:
        context.set_default_timeout(LINKEDIN_BROWSER_TIMEOUT_MS)
        yield context.pages[0] if context.pages else await context.new_page()
    finally:
        await _linkedin_close(context, playwright)


async def _linkedin_close(context: Any, playwright: Any) -> None:
    """Shut the browser down on a deadline, because a stop has to actually stop.

    A cancelled walk left the driver waiting on a browser that had already gone: the window was
    closed, every posting was collected and saved, and the process still sat there. Each step gets
    its own bounded wait and a failure to shut down is not worth reporting - the run is over.
    """
    for shutdown in (context.close, playwright.stop):
        with suppress(Exception):
            await asyncio.wait_for(shutdown(), LINKEDIN_BROWSER_CLOSE_SECONDS)


async def _linkedin_wait_for_posting(page: Any) -> None:
    """Let the description render before reading it, and read whatever is there if it never does.

    Waiting for the container is not enough: LinkedIn renders the section and its heading first
    and fills in the posting a moment later, so a walk that read on the element's arrival got a
    heading and nothing else. A timeout is not treated as an error - the caller already skips a
    posting it could not read, with a warning, and a window that has gone away fails at the very
    next call anyway.
    """
    try:
        await page.wait_for_function(LINKEDIN_FILLED_SCRIPT, timeout=LINKEDIN_POSTING_WAIT_MS)
    except Exception:  # noqa: BLE001 - a missing description is the caller's decision, not an error here
        return


async def _linkedin_signed_in(page: Any) -> bool:
    """Whether the session cookie exists. Its name is checked; its value is never read or stored."""
    cookies = await page.context.cookies(LINKEDIN_HOME_URL)
    return any(cookie.get("name") == LINKEDIN_SESSION_COOKIE for cookie in cookies)


async def _linkedin_wait_for_login(page: Any, progress: _LinkedInProgress) -> bool:
    """Hand the window over until the user signs in themselves. Returns whether it had to wait.

    The signed-out state is read from the session cookie rather than from the URL, because
    LinkedIn serves its guest job pages at the very same addresses a member sees. A first run
    walked seventeen postings as a guest through the browser without ever asking for a sign-in,
    which looks like a window refreshing itself over and over and collects nothing extra.
    """
    if await _linkedin_signed_in(page):
        return False
    progress.announce("waiting for you to sign in to LinkedIn in the Chrome window")
    await page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded", timeout=LINKEDIN_BROWSER_TIMEOUT_MS)
    deadline = time.monotonic() + LINKEDIN_LOGIN_TIMEOUT_SECONDS
    while not await _linkedin_signed_in(page):
        if time.monotonic() > deadline:
            raise TimeoutError("Timed out waiting for a LinkedIn sign-in in the browser window")
        await _linkedin_pause(2.0)
    progress.announce("signed in, continuing")
    return True


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
    "linkedin": LinkedInCollector,
    "linkedin_browser": LinkedInBrowserCollector,
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
