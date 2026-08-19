from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

# ponytail: a dozen very common words, not a real stopword list — good enough to stop
# "with"/"team"/"have" from counting as a relevance match; extend only if it misfires.
_STOPWORDS = {
    "with", "from", "have", "team", "this", "that", "your", "were", "will", "into",
    "using", "used", "work", "role", "years", "year", "strong", "experience",
}


def _items(values: list[str]) -> str:
    return "".join(f"<li>{html.escape(str(value))}</li>" for value in values if value)


def _terms(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z][a-z0-9+#.]{3,}", text.casefold()) if word not in _STOPWORDS}


def _relevance(text: str, job_terms: set[str]) -> int:
    return len(_terms(text) & job_terms)


def _select_relevant(
    values: list[Any], text_of: Any, job_terms: set[str], *, floor: int = 2, cap: int = 5
) -> list[Any]:
    """Rank by overlap with the job text and drop the irrelevant tail — never invents anything,
    only chooses which of the candidate's own bullets/projects to show for this job. Never prunes
    below `floor` items, and does nothing at all when the job has no usable text to compare against."""
    if not job_terms or len(values) <= floor:
        return values
    ranked = sorted(values, key=lambda v: -_relevance(text_of(v), job_terms))
    relevant = [v for v in ranked if _relevance(text_of(v), job_terms) > 0]
    kept = relevant if len(relevant) >= floor else ranked[:floor]
    return kept[:cap]


def tailor_profile_for_job(profile: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the profile with each role's highlights and the projects list pruned to
    what's relevant to this job. Selection only — nothing is added or reworded, so this stays
    truthful without an LLM. Same job + same profile always prunes the same way."""
    job_text = f"{job.get('title', '')} {job.get('description', '')}".casefold()
    job_terms = _terms(job_text)
    tailored = dict(profile)
    tailored["experience"] = [
        {**item, "highlights": _select_relevant(item.get("highlights", []), str, job_terms)}
        for item in profile.get("experience", [])
    ]
    tailored["projects"] = _select_relevant(
        profile.get("projects", []),
        lambda p: f"{p.get('name', '')} {p.get('summary', '')} {' '.join(p.get('highlights', []))}",
        job_terms,
        floor=1,
        cap=4,
    )
    return tailored


def render_resume_html(profile: dict[str, Any], job: dict[str, Any]) -> str:
    contact = profile.get("contact", {})
    location = profile.get("location", {})
    links = [
        str(contact.get(key, ""))
        for key in ("email", "phone", "website", "github", "linkedin")
        if contact.get(key)
    ]
    skills = profile.get("skills", {})
    job_text = f"{job.get('title', '')} {job.get('description', '')}".casefold()
    skill_groups = []
    if isinstance(skills, dict):
        for group, values in skills.items():
            ordered = sorted(values, key=lambda value: str(value).casefold() not in job_text)
            skill_groups.append(
                f"<p><strong>{html.escape(str(group))}:</strong> {html.escape(', '.join(map(str, ordered)))}</p>"
            )
    experience = []
    for item in profile.get("experience", []):
        dates = " – ".join(filter(None, (str(item.get("start", "")), str(item.get("end", "")))))
        experience.append(
            "<article><header><div><strong>"
            f"{html.escape(str(item.get('title', '')))}</strong> · {html.escape(str(item.get('company', '')))}</div>"
            f"<span>{html.escape(dates)}</span></header><ul>{_items(item.get('highlights', []))}</ul></article>"
        )
    project_blocks = []
    for item in profile.get("projects", []):
        project_blocks.append(
            f"<article><strong>{html.escape(str(item.get('name', '')))}</strong>"
            f"<p>{html.escape(str(item.get('summary', '')))}</p><ul>{_items(item.get('highlights', []))}</ul></article>"
        )
    education = []
    for item in profile.get("education", []):
        education.append(
            f"<p><strong>{html.escape(str(item.get('institution', '')))}</strong> — "
            f"{html.escape(' · '.join(filter(None, (str(item.get('degree', '')), str(item.get('field', ''))))))}</p>"
        )
    languages = ", ".join(
        f"{item.get('name', '')}{f' ({item.get("proficiency")})' if item.get('proficiency') else ''}"
        for item in profile.get("languages", [])
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(str(profile.get('name', 'Resume')))}</title>
<style>@page{{size:A4;margin:14mm 16mm}}*{{box-sizing:border-box}}body{{font:9.4pt/1.38 Arial,sans-serif;color:#152019;margin:0}}h1{{font-size:22pt;margin:0}}h2{{font-size:11pt;text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid #9aa79f;padding-bottom:3px;margin:15px 0 7px}}.headline{{font-size:11pt;color:#3f5b4d;margin:2px 0}}.contact{{font-size:8.5pt;color:#526158;margin:4px 0 10px}}p{{margin:3px 0}}article{{margin:0 0 8px}}article header{{display:flex;justify-content:space-between;gap:12px}}article header span{{white-space:nowrap;color:#526158}}ul{{margin:3px 0;padding-left:17px}}li{{margin:1px 0}}</style></head><body>
<h1>{html.escape(str(profile.get('name', '')))}</h1><p class="headline">{html.escape(str(profile.get('headline', '')))}</p>
<p class="contact">{html.escape(' · '.join([*filter(None, (str(location.get('city', '')), str(location.get('country_name', '')))), *links]))}</p>
<p>{html.escape(str(profile.get('summary', '')))}</p>
{f'<h2>Skills</h2>{"".join(skill_groups)}' if skill_groups else ''}
{f'<h2>Experience</h2>{"".join(experience)}' if experience else ''}
{f'<h2>Projects</h2>{"".join(project_blocks)}' if project_blocks else ''}
{f'<h2>Education</h2>{"".join(education)}' if education else ''}
{f'<h2>Languages</h2><p>{html.escape(languages)}</p>' if languages else ''}
</body></html>"""


class BuiltinResumeRenderer:
    async def render(
        self,
        *,
        profile: dict[str, Any],
        job: dict[str, Any],
        output_dir: Path,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / "resume.html"
        json_path = output_dir / "resume.json"
        pdf_path = output_dir / "resume.pdf"
        tailored = tailor_profile_for_job(profile, job)
        html_path.write_text(render_resume_html(tailored, job), encoding="utf-8")
        json_path.write_text(json.dumps(tailored, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page()
                await page.goto(html_path.resolve().as_uri(), wait_until="load")
                await page.pdf(path=str(pdf_path), format="A4", print_background=True)
                await browser.close()
        except Exception as error:
            raise RuntimeError(
                "The built-in resume HTML was created, but PDF rendering failed. "
                "Run 'playwright install chromium' and try again. "
                f"Original error: {error}"
            ) from error
        return pdf_path
