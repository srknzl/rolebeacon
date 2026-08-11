from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

FIELD_SELECTORS = {
    "first_name": ["input[name='first_name']", "input[name*='firstName' i]", "input[autocomplete='given-name']"],
    "last_name": ["input[name='last_name']", "input[name*='lastName' i]", "input[autocomplete='family-name']"],
    "full_name": ["input[name='name']", "input[autocomplete='name']"],
    "email": ["input[type='email']", "input[name*='email' i]"],
    "phone": ["input[type='tel']", "input[name*='phone' i]"],
    "website": ["input[name*='website' i]", "input[name*='portfolio' i]"],
    "linkedin": ["input[name*='linkedin' i]"],
    "github": ["input[name*='github' i]"],
}


def fill_first(page: Page, selectors: list[str], value: str) -> None:
    if not value:
        return
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible() and not locator.input_value():
                locator.fill(value)
                return
        except PlaywrightTimeoutError:
            continue


def fill_application(page: Page, packet: dict[str, Any]) -> None:
    candidate = packet["candidate"]
    for field, selectors in FIELD_SELECTORS.items():
        fill_first(page, selectors, str(candidate.get(field, "")))
    resume_path = packet.get("resume_path", "")
    if resume_path and Path(resume_path).exists():
        for selector in ("input[type='file'][name*='resume' i]", "input[type='file']"):
            locator = page.locator(selector).first
            if locator.count():
                locator.set_input_files(resume_path)
                break


def run(packet_path: Path, profile_dir: Path) -> None:
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(packet["job_url"], wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        fill_application(page, packet)
        print("Application fields were prepared. Review every answer and submit manually.")
        try:
            page.wait_for_event("close", timeout=0)
        finally:
            context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Open and prepare an application without submitting it")
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    args = parser.parse_args()
    run(args.packet, args.profile_dir)


if __name__ == "__main__":
    main()
