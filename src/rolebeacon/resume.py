from __future__ import annotations

import asyncio
import html
import json
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright


def _items(values: list[str]) -> str:
    return "".join(f"<li>{html.escape(str(value))}</li>" for value in values if value)


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
    projects = []
    for item in profile.get("projects", []):
        projects.append(
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
{f'<h2>Projects</h2>{"".join(projects)}' if projects else ''}
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
        html_path.write_text(render_resume_html(profile, job), encoding="utf-8")
        json_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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


class ExternalCommandResumeRenderer:
    def __init__(self, command: tuple[str, ...], profile_path: Path):
        if not command:
            raise ValueError("External resume command is empty")
        self.command = command
        self.profile_path = profile_path

    async def render(
        self,
        *,
        profile: dict[str, Any],
        job: dict[str, Any],
        output_dir: Path,
    ) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        jd_path = output_dir / "job-description.txt"
        output_path = output_dir / "resume.pdf"
        jd_path.write_text(str(job.get("description", "")), encoding="utf-8")
        values = {"jd": str(jd_path), "output": str(output_path), "profile": str(self.profile_path)}
        argv = [part.format_map(values) for part in self.command]
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            raise RuntimeError(message or "External resume generator failed")
        if not output_path.exists():
            raise RuntimeError("External resume generator did not create the configured output file")
        return output_path
