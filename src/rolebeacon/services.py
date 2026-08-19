from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .database import Database
from .llm import LlmClient
from .resume import BuiltinResumeRenderer, tailor_profile_for_job

# greeting/closing are deliberately absent: a model asked to fill them invents a hiring-manager
# name we never supplied. Python writes both from a small language-keyed template instead.
COVER_LETTER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 4},
    },
    "required": ["subject", "paragraphs"],
}

# ponytail: two languages, English default - extend when a third market shows up. Keyed off the
# same German-application signal cover_letter_recommendation() already checks for.
_GREETING_CLOSING = {"de": ("Sehr geehrte Damen und Herren,", "Mit freundlichen Grüßen")}
_DEFAULT_GREETING_CLOSING = ("Dear Hiring Team,", "Sincerely,")


def _greeting_and_closing(job: dict[str, Any]) -> tuple[str, str]:
    text = f"{job.get('title', '')} {job.get('description', '')}".casefold()
    if re.search(r"anschreiben|motivationsschreiben|bewerbung", text):
        return _GREETING_CLOSING["de"]
    return _DEFAULT_GREETING_CLOSING


def _validate_cover_letter(value: dict[str, Any]) -> None:
    subject = str(value.get("subject", "")).strip()
    if not subject or len(subject) > 120:
        raise ValueError("Subject must be a short, non-empty line")
    body = "\n\n".join(str(paragraph).strip() for paragraph in value.get("paragraphs", []))
    word_count = len(re.findall(r"\b\w+\b", body))
    if not 200 <= word_count <= 400:
        raise ValueError(f"Cover letter length is outside the target range: {word_count} words (aim for 250-350)")


class ProfileValidationError(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


def validate_candidate_profile(profile: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for experience in profile.get("experience", []):
        end = str(experience.get("end", ""))
        if not end.isdigit():
            continue
        end_year = int(end)
        for highlight in experience.get("highlights", []):
            years = [int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", str(highlight))]
            if years and max(years) > end_year:
                issues.append(
                    f"{experience.get('company', 'Experience')} ends in {end_year}, but a highlight mentions {max(years)}"
                )
    return issues


def cover_letter_recommendation(job: dict[str, Any]) -> tuple[bool, str]:
    text = f"{job.get('title', '')} {job.get('description', '')}".casefold()
    route = job.get("route", "")
    score = int(job.get("score") or 0)
    if re.search(r"cover letter|anschreiben|motivationsschreiben", text):
        return True, "The job description requests a cover letter"
    if str(route).startswith("relocate-"):
        return True, "A tailored cover letter may clarify motivation and relocation readiness"
    if score >= 85:
        return True, "This is a high-priority match where a specific motivation note may add context"
    return False, "A cover letter is optional and unlikely to add enough value for this application"


class ArtifactService:
    def __init__(self, settings: Settings, database: Database, llm: LlmClient):
        self.settings = settings
        self.database = database
        self.llm = llm

    def application_dir(self, job_id: int) -> Path:
        directory = self.settings.data_dir / "applications" / str(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    async def generate_resume(self, job_id: int) -> Path:
        job = self._require_job(job_id)
        profile = self.settings.load_candidate_profile()
        issues = validate_candidate_profile(profile)
        if issues:
            raise ProfileValidationError(issues)
        directory = self.application_dir(job_id)
        jd_path = directory / "job-description.txt"
        jd_path.write_text(str(job.get("description", "")), encoding="utf-8")
        output_path = await BuiltinResumeRenderer().render(profile=profile, job=job, output_dir=directory)
        self.database.save_application(job_id, status="preparing", resume_path=str(output_path))
        return output_path

    async def generate_cover_letter(self, job_id: int) -> Path:
        job = self._require_job(job_id)
        profile = self.settings.load_candidate_profile()
        issues = validate_candidate_profile(profile)
        if issues:
            raise ProfileValidationError(issues)
        # Tailored and stripped: only the sections relevant to this job, and no contact PII
        # (email/phone/linkedin/github) reaches the prompt - mirrors llm.py score()'s compact_profile.
        tailored = tailor_profile_for_job(profile, job)
        compact_profile = {key: tailored.get(key) for key in ("summary", "location", "experience", "projects", "skills", "education")}
        mobility = self.settings.load_mobility_profile()
        search_profile = self.settings.load_search_profile()
        prompt = (
            "Write a concise, specific cover letter for this application. Use 3-4 short paragraphs totalling "
            "250-350 words. Explain motivation, connect two concrete candidate examples to the role, and "
            "address relocation or remote-work eligibility only when relevant, using only the mobility facts "
            "supplied below. Do not invent metrics, company facts, a hiring-manager name, work authorization, "
            "or skills. Avoid generic praise and do not repeat the resume verbatim. Write in the same language "
            "as the job description.\n\n"
            f"CANDIDATE:\n{json.dumps(compact_profile, ensure_ascii=False)}\n\n"
            f"MOBILITY:\n{json.dumps({key: mobility.get(key) for key in ('work_authorizations', 'willing_to_relocate', 'sponsorship_required_outside_authorized_countries', 'timezone')}, ensure_ascii=False)}\n\n"
            f"PREFERENCES:\n{json.dumps({key: search_profile.get(key) for key in ('target_roles', 'preferred_domains')}, ensure_ascii=False)}\n\n"
            f"JOB:\n{json.dumps({key: job.get(key) for key in ('title', 'company', 'location', 'route', 'sponsorship', 'relocation')}, ensure_ascii=False)}\n\n"
            f"DESCRIPTION:\n{str(job.get('description', ''))[:20000]}"
        )
        value = await self.llm.generate_text(
            "You draft factual software-engineering cover letters grounded only in supplied evidence.",
            prompt,
            COVER_LETTER_SCHEMA,
            "cover_letter",
            validate=_validate_cover_letter,
        )
        body = "\n\n".join(str(paragraph).strip() for paragraph in value["paragraphs"])
        greeting, closing = _greeting_and_closing(job)
        directory = self.application_dir(job_id)
        text_path = directory / "cover-letter.txt"
        text_path.write_text(
            f"{value['subject']}\n\n{greeting}\n\n{body}\n\n{closing}\n{profile.get('name', '')}\n",
            encoding="utf-8",
        )
        html_path = directory / "cover-letter.html"
        paragraphs = "".join(f"<p>{html.escape(str(paragraph))}</p>" for paragraph in value["paragraphs"])
        html_path.write_text(
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            "@page{size:A4;margin:22mm}body{font:11pt/1.5 Arial,sans-serif;color:#111;max-width:170mm;margin:auto}"
            "p{margin:0 0 12pt}</style></head><body>"
            f"<p><strong>{html.escape(value['subject'])}</strong></p>"
            f"<p>{html.escape(greeting)}</p>{paragraphs}"
            f"<p>{html.escape(closing)}<br>{html.escape(profile.get('name', ''))}</p>"
            "</body></html>",
            encoding="utf-8",
        )
        self.database.save_application(job_id, status="preparing", cover_letter_path=str(text_path))
        return text_path

    def prepare_application(self, job_id: int) -> Path:
        job = self._require_job(job_id)
        profile = self.settings.load_candidate_profile()
        mobility = self.settings.load_mobility_profile()
        directory = self.application_dir(job_id)
        resume = directory / "resume.pdf"
        cover_letter = directory / "cover-letter.txt"
        contact = profile.get("contact", {})
        location = profile.get("location", {}) if isinstance(profile.get("location"), dict) else {}
        first_name, _, last_name = str(profile.get("name", "")).partition(" ")
        packet = {
            "job_id": job_id,
            "job_url": job.get("apply_url") or job.get("canonical_url"),
            "ats": detect_ats(str(job.get("apply_url") or job.get("canonical_url") or "")),
            "candidate": {
                "first_name": first_name,
                "last_name": last_name,
                "full_name": profile.get("name", ""),
                "email": contact.get("email", ""),
                "phone": contact.get("phone", ""),
                "location": ", ".join(filter(None, (location.get("city", ""), location.get("country_name", "")))),
                "website": contact.get("website", ""),
                "github": contact.get("github", ""),
                "linkedin": contact.get("linkedin", ""),
                "work_authorizations": mobility.get("work_authorizations", []),
                "requires_sponsorship_outside_authorized_countries": mobility.get(
                    "sponsorship_required_outside_authorized_countries", True
                ),
                "willing_to_relocate": mobility.get("willing_to_relocate", False),
                "contractor_allowed": mobility.get("contractor_allowed", False),
                "eor_allowed": mobility.get("eor_allowed", False),
            },
            "resume_path": str(resume) if resume.exists() else "",
            "cover_letter_path": str(cover_letter) if cover_letter.exists() else "",
            "manual_review": [
                "Salary expectation",
                "Relocation preference",
                "Work-authorization wording",
                "Legal and demographic declarations",
                "Every custom question",
                "Final submission",
            ],
        }
        packet_path = directory / "application-packet.json"
        packet_path.write_text(json.dumps(packet, indent=2, ensure_ascii=False), encoding="utf-8")
        self.database.save_application(job_id, status="ready", packet_path=str(packet_path))
        if self.settings.open_browser:
            subprocess.Popen(
                [sys.executable, "-m", "rolebeacon.browser", "--packet", str(packet_path), "--profile-dir", str(self.settings.data_dir / "browser-profile")],
                cwd=self.settings.root,
                env=os.environ.copy(),
                start_new_session=True,
            )
        return packet_path

    def _require_job(self, job_id: int) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if not job:
            raise LookupError(f"Job {job_id} was not found")
        return job


def detect_ats(url: str) -> str:
    host = urlsplit(url).netloc.casefold()
    if "greenhouse" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host:
        return "workday"
    if "google" in host:
        return "google"
    if "microsoft" in host:
        return "microsoft"
    return "generic"
