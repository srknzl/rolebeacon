"""Interactive terminal setup, mirroring the six-step web wizard.

The wizard holds one in-memory `SetupPayloadV1`-shaped draft and writes nothing until the final
confirmation, so cancelling at any prompt leaves the installation untouched. Completeness,
validation, the source catalog, and activation all come from `SetupService`, never from a second
interpretation living here.
"""

from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .profile import (
    DEFAULT_SCORE_WEIGHTS,
    SETUP_PLANNING_PROMPT,
    country_catalog,
    relocation_region_options,
)
from .scoring import seniority_level_options
from .setup import SetupService
from .source_catalog import SourceCatalog
from .terminal import CLEAR_WORD, Cancelled, GoBack, Terminal

STEP_TITLES = ("Start", "Profile", "Eligibility", "Sources", "Optional settings", "Review")
CLEARANCE_CHOICES = (
    ("unknown", "Unknown / not configured"),
    ("cannot_meet", "I explicitly cannot meet clearance requirements"),
    ("eligible_to_attempt", "I may be eligible to undergo vetting"),
    ("has_active_clearance", "I have an active clearance (add exact credentials through setup JSON)"),
)
SCORE_WEIGHT_LABELS = (
    ("role_domain", "Role match"),
    ("stack", "Skills"),
    ("domain_experience", "Domain experience"),
    ("seniority", "Seniority"),
    ("location_authorization", "Location and authorization"),
    ("salary_employment", "Salary and employment"),
)
CONTACT_NOTE = "Contact details are used only for applications. They are excluded from scoring prompts."
# Kinds the web wizard also hides, because they need an API key that setup does not collect.
KEYED_SOURCE_KINDS = frozenset({"adzuna", "jooble", "serpapi", "google_careers", "amazon_jobs"})


def blank_draft() -> dict[str, Any]:
    return {
        "candidate": {
            "schema_version": "1.0",
            "name": "",
            "headline": "",
            "summary": "",
            "contact": {"email": "", "phone": "", "website": None, "github": None, "linkedin": None},
            "location": {"country_code": "", "country_name": "", "city": ""},
            "skills": {},
            "experience": [],
            "projects": [],
            "education": [],
            "languages": [],
        },
        "mobility": {
            "schema_version": "1.0",
            "current_country_code": "",
            "work_authorizations": [],
            "relocation_targets": [],
            "remote_from_current_country": True,
            "willing_to_relocate": True,
            "contractor_allowed": True,
            "eor_allowed": True,
            "sponsorship_required_outside_authorized_countries": True,
            "timezone": "",
            "clearance_policy": {
                "status": "unknown",
                "willing_to_undergo_vetting": None,
                "explicitly_excluded_requirements": [],
                "credentials": [],
            },
        },
        "preferences": {
            "schema_version": "1.0",
            "target_roles": [],
            "preferred_skills": [],
            "preferred_domains": [],
            "preferred_seniority": [],
            "priority_companies": [],
            "company_watchlist": [],
            "company_blocklist": [],
            "exclude_phrases": [],
            "salary": {"minimum": None, "currency": "", "hard_filter": False},
            "daily_review_limit": 15,
            "score_weights": dict(DEFAULT_SCORE_WEIGHTS),
        },
        "enabled_source_ids": [],
        "llm": {
            "mode": "rules",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3:8b",
            "api_key": "",
            "api_key_action": "preserve",
        },
        "activate": False,
    }


def describe_issue(issue: Any) -> str:
    if isinstance(issue, dict):
        location = ".".join(str(part) for part in issue.get("loc", ()))
        message = str(issue.get("msg", "is invalid"))
        return f"{location}: {message}" if location else message
    return str(issue)


class SetupWizard:
    def __init__(self, settings: Settings, terminal: Terminal | None = None) -> None:
        self.settings = settings
        self.service = SetupService(settings)
        self.catalog = SourceCatalog(settings)
        self.terminal = terminal if terminal is not None else Terminal()
        self.draft = blank_draft()
        self.selected_pack_ids: list[str] = []
        self.llm_key_configured = False
        # None means "leave whatever is stored alone"; the Brave key is a settings value rather
        # than part of SetupPayloadV1, so it is written in the same final step as the payload.
        self.company_search_key: str | None = None
        if settings.setup_complete:
            saved = self.service.saved_payload()
            self.llm_key_configured = bool(saved["llm"].pop("api_key_configured", False))
            self.draft = {**self.draft, **saved, "activate": False}

    # Navigation -----------------------------------------------------------

    def run(self) -> dict[str, Any] | None:
        """Return the saved setup summary, or None when the user cancelled or backed out."""
        steps = (
            self._step_start,
            self._step_profile,
            self._step_eligibility,
            self._step_sources,
            self._step_optional,
            self._step_review,
        )
        index = 0
        try:
            while index < len(steps):
                self.terminal.step_header(index + 1, len(steps), STEP_TITLES[index])
                try:
                    target = steps[index]()
                except GoBack:
                    if index == 0:
                        raise Cancelled("Setup cancelled. Nothing was saved.") from None
                    index -= 1
                    continue
                index = target if target is not None else index + 1
        except Cancelled as error:
            self.terminal.write()
            self.terminal.write(str(error))
            return None
        try:
            return self._save()
        except ValueError as error:
            # A pydantic failure quotes the value it rejected, and the draft carries a secret, so
            # this reports a redacted message rather than letting a traceback print the key.
            self.terminal.write()
            self.terminal.error(self._redact(str(error)))
            self.terminal.write("Setup was not saved.")
            return None

    def _redact(self, message: str) -> str:
        for secret in (str(self.draft["llm"].get("api_key", "")), self.company_search_key or ""):
            if secret:
                message = message.replace(secret, "***")
        return message

    def _save(self) -> dict[str, Any]:
        # Installing a pack writes source configuration, so it happens here rather than while the
        # user is still choosing. The saved IDs are authoritative over the ones the draft predicted.
        for pack_id in self.selected_pack_ids:
            installed = self.catalog.install(pack_id, enabled=False)
            self.draft["enabled_source_ids"] = sorted(
                set(self.draft["enabled_source_ids"]) | set(installed.source_ids)
            )
        settings = self.service.complete(self.draft)
        if self.company_search_key is not None:
            settings = settings.save_company_search_key(self.company_search_key)
        self.settings = settings
        self.terminal.write()
        self.terminal.write(
            "Setup saved and scheduled collection is active."
            if settings.activated
            else "Setup saved. No source will be contacted until you activate collection."
        )
        return {
            "setup_complete": settings.setup_complete,
            "activated": settings.activated,
            "enabled_source_ids": [source.id for source in settings.load_sources() if source.enabled],
            "scoring_mode": settings.llm_mode,
        }

    # Steps ----------------------------------------------------------------

    def _step_start(self) -> int | None:
        terminal = self.terminal
        terminal.write("Nothing is written until you confirm on the final step.")
        terminal.write("At any prompt: b returns to the previous step, q abandons setup.")
        terminal.write("No job source is contacted while you answer these questions.")
        choice = terminal.ask_choice(
            "How do you want to start",
            (
                ("guided", "Answer the questions here"),
                ("paste", "Paste a SetupPayloadV1 JSON document produced by an LLM"),
            ),
            default="guided",
        )
        if choice == "guided":
            return None
        terminal.write()
        terminal.write(SETUP_PLANNING_PROMPT)
        terminal.write()
        while True:
            document = terminal.ask_multiline(
                "Paste the JSON",
                help_text="A model API key inside the document is ignored; enter secrets privately in step 5.",
            )
            try:
                value = json.loads(document)
            except json.JSONDecodeError as error:
                terminal.error(f"That is not valid JSON: {error}")
                value = None
            if value is not None and not isinstance(value, dict):
                terminal.error("The setup document must be a JSON object.")
                value = None
            if isinstance(value, dict):
                validation = self.service.validate_setup_payload(value)
                if validation["valid"]:
                    self.draft = {**self.draft, **validation["payload"], "activate": False}
                    terminal.write("Loaded. Every value is shown for review in the next steps.")
                    return None
                for issue in validation["errors"]:
                    terminal.error(describe_issue(issue))
            if not terminal.confirm("Paste a corrected document?", default=True):
                terminal.write("Continuing with the guided questions instead.")
                return None

    def _step_profile(self) -> int | None:
        terminal = self.terminal
        candidate = self.draft["candidate"]
        candidate["name"] = terminal.ask_text("Full name", default=candidate.get("name", ""), required=True)
        candidate["headline"] = terminal.ask_text(
            "Headline",
            default=candidate.get("headline", ""),
            help_text="A concise professional title, for example: Backend and distributed-systems engineer.",
        )
        candidate["summary"] = terminal.ask_text(
            "Professional summary",
            default=candidate.get("summary", ""),
            help_text="Factual summary used for matching and generated materials.",
        )
        location = candidate["location"]
        code, name = self._ask_country(
            "Current country (name or ISO 3166-1 code)",
            current=(location.get("country_code", ""), location.get("country_name", "")),
            required=True,
        )
        location["country_code"], location["country_name"] = code, name
        location["city"] = terminal.ask_text("Current city", default=location.get("city", ""))
        self.draft["mobility"]["current_country_code"] = code

        contact = candidate["contact"]
        terminal.note(CONTACT_NOTE)
        contact["email"] = terminal.ask_text("Email", default=contact.get("email", ""))
        contact["phone"] = terminal.ask_text("Phone", default=contact.get("phone", ""))
        contact["website"] = terminal.ask_text("Website URL", default=contact.get("website") or "") or None

        candidate["skills"] = self._ask_skills(candidate.get("skills") or {})
        self._ask_detailed_candidate(candidate)
        return None

    def _step_eligibility(self) -> int | None:
        terminal = self.terminal
        mobility = self.draft["mobility"]
        preferences = self.draft["preferences"]
        terminal.write("These answers decide eligibility. RoleBeacon never infers a work right you did not state.")
        preferences["target_roles"] = terminal.ask_lines(
            "Target role",
            default=preferences.get("target_roles", ()),
            help_text="Required. Job titles you actively want; they drive discovery and ranking.",
        )
        mobility["work_authorizations"] = self._ask_country_codes(
            "Country where you can work today",
            current=mobility.get("work_authorizations", ()),
            help_text="Only countries where you can work now without employer sponsorship.",
        )
        mobility["relocation_targets"] = self._ask_relocation_targets(mobility.get("relocation_targets", ()))
        mobility["remote_from_current_country"] = terminal.ask_bool(
            "Accept remote roles that explicitly permit work from your current country",
            default=bool(mobility.get("remote_from_current_country", True)),
        )
        mobility["willing_to_relocate"] = terminal.ask_bool(
            "Willing to relocate", default=bool(mobility.get("willing_to_relocate", True))
        )
        mobility["contractor_allowed"] = terminal.ask_bool(
            "Accept independent-contractor arrangements", default=bool(mobility.get("contractor_allowed", True))
        )
        mobility["eor_allowed"] = terminal.ask_bool(
            "Accept employer-of-record arrangements", default=bool(mobility.get("eor_allowed", True))
        )
        mobility["sponsorship_required_outside_authorized_countries"] = terminal.ask_bool(
            "Require explicit visa sponsorship outside your authorized countries",
            default=bool(mobility.get("sponsorship_required_outside_authorized_countries", True)),
            help_text="Keep this on unless you already hold the work right in your relocation targets.",
        )
        mobility["timezone"] = terminal.ask_text("Timezone", default=mobility.get("timezone", ""))

        clearance = mobility["clearance_policy"]
        terminal.note("Optional and local only. RoleBeacon never infers clearance from nationality or résumé text.")
        clearance["status"] = terminal.ask_choice(
            "Security-clearance policy", CLEARANCE_CHOICES, default=str(clearance.get("status", "unknown"))
        )
        vetting = terminal.ask_bool(
            "Willing to undergo clearance vetting", default=bool(clearance.get("willing_to_undergo_vetting"))
        )
        clearance["willing_to_undergo_vetting"] = None if clearance["status"] == "unknown" and not vetting else vetting

        preferences["preferred_skills"] = terminal.ask_lines(
            "Preferred skill", default=preferences.get("preferred_skills", ())
        )
        preferences["preferred_domains"] = terminal.ask_lines(
            "Preferred domain", default=preferences.get("preferred_domains", ())
        )
        preferences["preferred_seniority"] = terminal.ask_multi_choice(
            "Preferred seniority",
            tuple((level["code"], level["label"]) for level in seniority_level_options()),
            selected=preferences.get("preferred_seniority", ()),
            help_text="Matched against the level a job title names, so only these exact levels can match.",
        )
        preferences["priority_companies"] = terminal.ask_lines(
            "Priority company", default=preferences.get("priority_companies", ())
        )
        preferences["company_watchlist"] = terminal.ask_lines(
            "Watchlist company", default=preferences.get("company_watchlist", ())
        )
        preferences["company_blocklist"] = terminal.ask_lines(
            "Blocked company", default=preferences.get("company_blocklist", ())
        )
        preferences["exclude_phrases"] = terminal.ask_lines(
            "Phrase that should reject a job", default=preferences.get("exclude_phrases", ())
        )

        salary = preferences["salary"]
        salary["minimum"] = terminal.ask_optional_number(
            "Minimum salary", default=salary.get("minimum"), help_text="Leave blank for no minimum."
        )
        salary["currency"] = terminal.ask_text("Salary currency", default=salary.get("currency", "")).upper()
        salary["hard_filter"] = terminal.ask_bool(
            "Reject postings below that minimum",
            default=bool(salary.get("hard_filter")),
            help_text="Only comparable stated pay is rejected; missing or different-currency pay stays unknown.",
        )
        preferences["daily_review_limit"] = terminal.ask_int(
            "Daily review limit", default=int(preferences.get("daily_review_limit", 15)), minimum=1, maximum=100
        )
        return None

    def _step_sources(self) -> int | None:
        terminal = self.terminal
        view = self.catalog.view()
        terminal.write("Selected sources are saved now and contacted only after you activate collection.")
        selected = set(self.draft.get("enabled_source_ids", ()))
        packs_by_id = {str(pack["id"]): pack for pack in view["packs"]}
        pack_options = tuple(
            (
                pack_id,
                f"{pack['name']}{' (recommended)' if pack.get('recommended') else ''} — "
                f"{pack['description']} [{pack['source_count']} official boards]",
            )
            for pack_id, pack in packs_by_id.items()
        )
        self.selected_pack_ids = terminal.ask_multi_choice(
            "Source packs",
            pack_options,
            selected=self.selected_pack_ids
            or [
                pack_id
                for pack_id in packs_by_id
                if self.catalog.pack_source_ids(pack_id) and set(self.catalog.pack_source_ids(pack_id)) <= selected
            ],
        )
        # A pack's saved sources can carry different IDs from its catalog entries, so resolve them
        # the same way installing would - without writing anything yet.
        from_packs = {source_id for pack_id in self.selected_pack_ids for source_id in self.catalog.pack_source_ids(pack_id)}
        # The individual list mirrors the web wizard: already-configured sources, minus the kinds
        # that need an API key and are therefore not offered during setup.
        feeds = [source for source in self.settings.load_sources() if source.kind not in KEYED_SOURCE_KINDS]
        chosen_sources = terminal.ask_multi_choice(
            "Public feeds and official searches",
            tuple((source.id, source.name) for source in feeds),
            selected=sorted(selected & {source.id for source in feeds}),
            help_text="Country eligibility and your target roles are applied after collection.",
        )
        generated = []
        if terminal.ask_bool(
            "Search Google Careers for your countries",
            default="__google_careers__" in selected,
            help_text="Expands into one search per country you can work in or would relocate to.",
        ):
            generated.append("__google_careers__")
        if terminal.ask_bool("Search Amazon Jobs for your countries", default="__amazon_jobs__" in selected):
            generated.append("__amazon_jobs__")
        self.draft["enabled_source_ids"] = sorted(from_packs | set(chosen_sources)) + generated
        if view.get("coverage_gaps"):
            terminal.note(
                "No source covers: " + ", ".join(str(gap["company"]) for gap in view["coverage_gaps"]) + "."
            )
        return None

    def _step_optional(self) -> int | None:
        terminal = self.terminal
        terminal.write("Everything here is optional. Rules-only scoring is a complete mode.")
        self.draft["preferences"]["score_weights"] = self._ask_score_weights(
            dict(self.draft["preferences"].get("score_weights") or DEFAULT_SCORE_WEIGHTS)
        )
        llm = self.draft["llm"]
        llm["mode"] = terminal.ask_choice(
            "Scoring engine",
            (
                ("rules", "Rules only — deterministic, contacts no model"),
                ("ollama", "Local Ollama"),
                ("custom", "Custom OpenAI-compatible endpoint"),
            ),
            default=str(llm.get("mode", "rules")),
        )
        if llm["mode"] != "rules":
            llm["base_url"] = terminal.ask_text("Endpoint base URL", default=str(llm.get("base_url", "")))
            llm["model"] = terminal.ask_text("Model identifier", default=str(llm.get("model", "")))
            key = terminal.ask_private(
                "Model API key",
                help_text=(
                    f"Never echoed. Blank keeps the {'saved' if self.llm_key_configured else 'current empty'} "
                    "key; - removes it."
                ),
            )
            if key == "-":
                llm["api_key"], llm["api_key_action"] = "", "remove"
            elif key:
                llm["api_key"], llm["api_key_action"] = key, "replace"
            else:
                llm["api_key"], llm["api_key_action"] = "", "preserve"
        company_key = terminal.ask_private(
            "Brave Search API key for company research",
            help_text="Optional. Never echoed. Blank keeps the stored key; - removes it. No-key research still works.",
        )
        if company_key == "-":
            self.company_search_key = ""
        elif company_key:
            self.company_search_key = company_key
        return None

    def _step_review(self) -> int | None:
        terminal = self.terminal
        review = self.service.review(self.draft)
        for item in review["items"]:
            marker = {"ready": " ", "ambiguous": "?", "missing": "!"}[item["status"]]
            terminal.write(f"  [{marker}] {item['title']}: {item['detail']}")
        terminal.write()
        for entry in review["ambiguous"]:
            terminal.write(f"  Ambiguous — {entry}")
        for entry in review["missing"]:
            terminal.write(f"  Missing — {entry}")
        if not review["ready"]:
            terminal.write()
            terminal.write("Setup cannot be saved until the missing facts above are supplied.")
        choices = [
            (title.casefold(), f"Go back to step {position}: {title}")
            for position, title in enumerate(STEP_TITLES[:-1], start=1)
        ]
        if review["ready"]:
            choices.insert(0, ("save", "Save this setup"))
        target = terminal.ask_choice("What next", tuple(choices), default=choices[0][0])
        if target != "save":
            return next(index for index, title in enumerate(STEP_TITLES[:-1]) if title.casefold() == target)
        self.draft["activate"] = terminal.confirm(
            "Activate scheduled collection after setup", default=False
        )
        return None

    # Prompts sharing catalogs with the web wizard --------------------------

    def _country_options(self, *, include_regions: bool = False) -> tuple[tuple[str, str], ...]:
        regions = (
            tuple((str(region["code"]), str(region["name"])) for region in relocation_region_options())
            if include_regions
            else ()
        )
        return regions + tuple((str(item["code"]), str(item["name"])) for item in country_catalog())

    def _ask_country(self, prompt: str, *, current: tuple[str, str], required: bool) -> tuple[str, str]:
        code, name = current
        while True:
            entry = self.terminal.ask_from_catalog(
                f"{prompt} [{name or code}]" if code else prompt, self._country_options()
            )
            if entry is not None and entry[0] != CLEAR_WORD:
                return entry
            # The clear word drops a stored country and a blank line keeps it, the same as at the
            # neighbouring country prompts. Neither can answer a required prompt, which asks again
            # rather than storing "-" as a country code and failing five steps later at the save.
            if entry is None and code and name:
                return code, name
            if not required:
                return "", ""
            self.terminal.error("This value is required.")

    def _ask_country_codes(self, prompt: str, *, current: Any, help_text: str = "") -> list[str]:
        terminal = self.terminal
        options = self._country_options()
        chosen: dict[str, str] = {}
        terminal.note(help_text)
        if current:
            names = dict(options)
            terminal.note("current: " + ", ".join(f"{names.get(code, code)} ({code})" for code in current))
        terminal.note(f"One per line. Blank line keeps the current values; {CLEAR_WORD} clears them.")
        while True:
            entry = terminal.ask_from_catalog(f"{prompt} ({len(chosen) + 1})", options)
            if entry is None:
                return list(chosen) or [str(code) for code in current]
            if entry[0] == CLEAR_WORD:
                return []
            chosen[entry[0]] = entry[1]

    def _ask_relocation_targets(self, current: Any) -> list[dict[str, Any]]:
        terminal = self.terminal
        options = self._country_options(include_regions=True)
        existing = [dict(target) for target in current]
        terminal.note("Countries or whole continents you would move to. This never claims a work right.")
        if existing:
            terminal.note("current: " + ", ".join(str(target.get("country_name", "")) for target in existing))
        terminal.note(f"One per line. Blank line keeps the current values; {CLEAR_WORD} clears them.")
        chosen: dict[str, dict[str, Any]] = {}
        while True:
            entry = terminal.ask_from_catalog(f"Relocation target ({len(chosen) + 1})", options)
            if entry is None:
                return list(chosen.values()) or existing
            if entry[0] == CLEAR_WORD:
                return []
            chosen[entry[0]] = {"country_code": entry[0], "country_name": entry[1], "cities": []}

    def _ask_skills(self, current: dict[str, list[str]]) -> dict[str, list[str]]:
        terminal = self.terminal
        buckets = {str(name): list(values) for name, values in current.items()}
        terminal.note("Group skills the way they should read on a résumé, for example Languages or Cloud & Infra.")
        if buckets:
            for name, values in buckets.items():
                terminal.note(f"current: {name}: {', '.join(values)}")
            if not terminal.ask_bool("Edit skill categories", default=False):
                return buckets
        while True:
            name = terminal.ask_text(f"Skill category name (blank when finished, {CLEAR_WORD} clears all)")
            if not name:
                return buckets
            if name == CLEAR_WORD:
                buckets = {}
                continue
            values = terminal.ask_lines(f"Skill in {name}", default=buckets.get(name, ()))
            if values:
                buckets[name] = values
            else:
                buckets.pop(name, None)

    def _ask_detailed_candidate(self, candidate: dict[str, Any]) -> None:
        terminal = self.terminal
        counts = ", ".join(
            f"{len(candidate.get(section) or ())} {section}"
            for section in ("experience", "projects", "education", "languages")
        )
        terminal.note(f"Detailed record: {counts}.")
        if not terminal.ask_bool(
            "Replace experience, projects, education, and languages from a candidate JSON document",
            default=False,
            help_text="These sections are entered as JSON in both wizards; leave this off to keep them as they are.",
        ):
            return
        while True:
            document = terminal.ask_multiline("Paste the candidate JSON")
            try:
                value = json.loads(document)
            except json.JSONDecodeError as error:
                terminal.error(f"That is not valid JSON: {error}")
                value = None
            if value is not None and not isinstance(value, dict):
                terminal.error("The candidate document must be a JSON object.")
                value = None
            if isinstance(value, dict):
                sections = ("experience", "projects", "education", "languages")
                merged = {**candidate, **{key: value.get(key, []) for key in sections}}
                validation = self.service.validate_profile(merged)
                if validation["valid"]:
                    candidate.update({key: merged[key] for key in sections})
                    terminal.write("Detailed record replaced.")
                    return
                for issue in validation["errors"]:
                    terminal.error(describe_issue(issue))
            if not terminal.confirm("Paste a corrected document?", default=True):
                return

    def _ask_score_weights(self, current: dict[str, int]) -> dict[str, int]:
        terminal = self.terminal
        terminal.note("Distribute all 100 opportunity-fit points. Eligibility stays a separate hard gate.")
        while True:
            weights = {
                key: terminal.ask_int(label, default=int(current.get(key, 0)), minimum=0, maximum=100)
                for key, label in SCORE_WEIGHT_LABELS
            }
            total = sum(weights.values())
            if total == 100:
                return weights
            terminal.error(f"The weights total {total}. They must total 100.")
            current = weights
