from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Literal

import pycountry
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

COUNTRY_NAME_OVERRIDES = {"TR": "Türkiye"}
RELOCATION_REGION_CODES = {
    "EUROPE": "Europe",
    "ASIA": "Asia",
    "AFRICA": "Africa",
    "NORTH_AMERICA": "North America",
    "SOUTH_AMERICA": "South America",
    "OCEANIA": "Oceania",
}
# ISO 3166-1 alpha-2 members of each region above, used to expand a one-click continent
# selection into real per-country relocation targets and source coverage, and by
# scoring._country_match to recognize a job's location against a region strategy. Antarctica and
# non-sovereign territories/dependencies are excluded; Europe intentionally excludes Belarus and
# Vatican City.
CONTINENT_COUNTRY_CODES: dict[str, tuple[str, ...]] = {
    "EUROPE": (
        "AL", "AD", "AT", "BE", "BA", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
        "HU", "IS", "IE", "IT", "LV", "LI", "LT", "LU", "MT", "MD", "MC", "ME", "NL", "MK", "NO",
        "PL", "PT", "RO", "SM", "RS", "SK", "SI", "ES", "SE", "CH", "UA", "GB",
    ),
    "ASIA": (
        "AE", "AF", "AM", "AZ", "BD", "BH", "BN", "BT", "CN", "GE", "HK", "ID", "IL", "IN", "IQ",
        "IR", "JO", "JP", "KG", "KH", "KP", "KR", "KW", "KZ", "LA", "LB", "LK", "MM", "MN", "MO",
        "MV", "MY", "NP", "OM", "PH", "PK", "PS", "QA", "RU", "SA", "SG", "SY", "TH", "TJ", "TL",
        "TM", "TR", "TW", "UZ", "VN", "YE",
    ),
    "AFRICA": (
        "DZ", "AO", "BJ", "BW", "BF", "BI", "CV", "CM", "CF", "TD", "KM", "CG", "CD", "CI", "DJ",
        "EG", "GQ", "ER", "SZ", "ET", "GA", "GM", "GH", "GN", "GW", "KE", "LS", "LR", "LY", "MG",
        "MW", "ML", "MR", "MU", "MA", "MZ", "NA", "NE", "NG", "RW", "ST", "SN", "SC", "SL", "SO",
        "ZA", "SS", "SD", "TZ", "TG", "TN", "UG", "ZM", "ZW",
    ),
    "NORTH_AMERICA": (
        "AG", "BS", "BB", "BZ", "CA", "CR", "CU", "DM", "DO", "SV", "GD", "GT", "HT", "HN", "JM",
        "MX", "NI", "PA", "KN", "LC", "VC", "TT", "US",
    ),
    "SOUTH_AMERICA": ("AR", "BO", "BR", "CL", "CO", "EC", "GY", "PY", "PE", "SR", "UY", "VE"),
    "OCEANIA": ("AU", "FJ", "KI", "MH", "FM", "NR", "NZ", "PW", "PG", "WS", "SB", "TO", "TV", "VU"),
}


@lru_cache
def country_catalog() -> tuple[dict[str, str], ...]:
    countries = [
        {"code": item.alpha_2, "name": COUNTRY_NAME_OVERRIDES.get(item.alpha_2, item.name)}
        for item in pycountry.countries
        if hasattr(item, "alpha_2")
    ]
    return tuple(sorted(countries, key=lambda item: item["name"]))


@lru_cache
def country_names_by_code() -> dict[str, str]:
    return {item["code"]: item["name"] for item in country_catalog()}


def normalize_iso_country_code(value: str) -> str:
    code = value.upper()
    if code not in country_names_by_code():
        raise ValueError("Use a valid ISO 3166-1 alpha-2 country code, such as TR or DE")
    return code


def normalize_relocation_target_code(value: str) -> str:
    """Accept an ISO country code or a deliberately small set of named regions."""
    code = value.upper()
    if code in RELOCATION_REGION_CODES:
        return code
    return normalize_iso_country_code(code)


def relocation_countries(relocation_targets: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Expand relocation targets into concrete {code, name} entries, expanding continents and deduplicating."""
    names = country_names_by_code()
    result: dict[str, str] = {}
    for item in relocation_targets:
        code = str(item.get("country_code", "")).upper()
        if code in CONTINENT_COUNTRY_CODES:
            for member in CONTINENT_COUNTRY_CODES[code]:
                result.setdefault(member, names[member])
        elif code in names:
            result.setdefault(code, str(item.get("country_name") or names[code]))
    return [{"code": code, "name": name} for code, name in result.items()]


def relocation_region_options() -> tuple[dict[str, str], ...]:
    """One entry per continent picker button: code, display name, and its member ISO codes."""
    return tuple(
        {"code": code, "name": name, "codes": ",".join(CONTINENT_COUNTRY_CODES[code])}
        for code, name in RELOCATION_REGION_CODES.items()
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ContactProfile(StrictModel):
    email: str = ""
    phone: str = ""
    website: HttpUrl | None = None
    github: HttpUrl | None = None
    linkedin: HttpUrl | None = None


class CandidateLocation(StrictModel):
    country_code: str = Field(min_length=2, max_length=2)
    country_name: str = Field(min_length=2)
    city: str = ""

    @field_validator("country_code")
    @classmethod
    def normalize_country_code(cls, value: str) -> str:
        return normalize_iso_country_code(value)


class ExperienceEntry(StrictModel):
    company: str = Field(min_length=1)
    title: str = Field(min_length=1)
    start: str = ""
    end: str = ""
    location: str = ""
    highlights: list[str] = Field(default_factory=list)


class ProjectEntry(StrictModel):
    name: str = Field(min_length=1)
    summary: str = ""
    highlights: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    url: HttpUrl | None = None


class EducationEntry(StrictModel):
    institution: str = Field(min_length=1)
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""


class LanguageEntry(StrictModel):
    name: str = Field(min_length=1)
    proficiency: str = ""


class CandidateProfileV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(min_length=1)
    headline: str = ""
    summary: str = ""
    contact: ContactProfile = Field(default_factory=ContactProfile)
    location: CandidateLocation
    skills: dict[str, list[str]] = Field(default_factory=dict)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    languages: list[LanguageEntry] = Field(default_factory=list)


class CountryPreference(StrictModel):
    country_code: str = Field(min_length=2, max_length=16)
    country_name: str = Field(min_length=2)
    cities: list[str] = Field(default_factory=list)

    @field_validator("country_code")
    @classmethod
    def normalize_country_code(cls, value: str) -> str:
        return normalize_relocation_target_code(value)


class MobilityProfileV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    current_country_code: str = Field(min_length=2, max_length=2)
    work_authorizations: list[str] = Field(default_factory=list)
    relocation_targets: list[CountryPreference] = Field(default_factory=list)
    remote_from_current_country: bool = True
    willing_to_relocate: bool = True
    contractor_allowed: bool = True
    eor_allowed: bool = True
    sponsorship_required_outside_authorized_countries: bool = True
    timezone: str = ""

    @field_validator("current_country_code")
    @classmethod
    def normalize_current_country(cls, value: str) -> str:
        return normalize_iso_country_code(value)

    @field_validator("work_authorizations")
    @classmethod
    def normalize_authorizations(cls, values: list[str]) -> list[str]:
        return sorted({normalize_iso_country_code(value) for value in values})

    @model_validator(mode="after")
    def include_current_country(self) -> MobilityProfileV1:
        if self.current_country_code not in self.work_authorizations:
            self.work_authorizations.append(self.current_country_code)
            self.work_authorizations.sort()
        return self


class SalaryPreference(StrictModel):
    minimum: float | None = Field(default=None, ge=0)
    currency: str = ""
    hard_filter: bool = False


class SearchPreferencesV1(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    target_roles: list[str] = Field(min_length=1)
    preferred_skills: list[str] = Field(default_factory=list)
    preferred_domains: list[str] = Field(default_factory=list)
    preferred_seniority: list[str] = Field(default_factory=list)
    priority_companies: list[str] = Field(default_factory=list)
    company_watchlist: list[str] = Field(default_factory=list)
    company_blocklist: list[str] = Field(default_factory=list)
    exclude_phrases: list[str] = Field(default_factory=list)
    salary: SalaryPreference = Field(default_factory=SalaryPreference)
    daily_review_limit: int = Field(default=15, ge=1, le=100)


class LlmSetup(StrictModel):
    mode: Literal["rules", "ollama", "custom"] = "rules"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3:8b"
    api_key: str = ""


class SetupPayloadV1(StrictModel):
    candidate: CandidateProfileV1
    mobility: MobilityProfileV1
    preferences: SearchPreferencesV1
    enabled_source_ids: list[str] = Field(default_factory=list)
    llm: LlmSetup = Field(default_factory=LlmSetup)
    activate: bool = True


class SearchStrategyV1(StrictModel):
    id: str
    label: str
    kind: Literal["priority_company", "company_watchlist", "authorized_local", "relocation", "remote", "other"]
    threshold: int = Field(ge=0, le=100)
    country_code: str = ""
    country_name: str = ""
    cities: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    requires_sponsorship: bool = False


def strategy_id(prefix: str, value: str = "") -> str:
    suffix = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return f"{prefix}-{suffix}" if suffix else prefix


def generate_strategies(
    candidate: CandidateProfileV1,
    mobility: MobilityProfileV1,
    preferences: SearchPreferencesV1,
) -> list[SearchStrategyV1]:
    strategies: list[SearchStrategyV1] = []
    if preferences.priority_companies:
        strategies.append(
            SearchStrategyV1(
                id="priority-companies",
                label="Priority companies",
                kind="priority_company",
                threshold=65,
                companies=preferences.priority_companies,
            )
        )
    if preferences.company_watchlist:
        strategies.append(
            SearchStrategyV1(
                id="company-watchlist",
                label="Company watchlist",
                kind="company_watchlist",
                threshold=70,
                companies=preferences.company_watchlist,
            )
        )

    known_countries: dict[str, CountryPreference] = {
        candidate.location.country_code: CountryPreference(
            country_code=candidate.location.country_code,
            country_name=candidate.location.country_name,
            cities=[candidate.location.city] if candidate.location.city else [],
        )
    }
    known_countries.update({item.country_code: item for item in mobility.relocation_targets})
    for country_code in mobility.work_authorizations:
        target = known_countries.get(country_code)
        if target:
            strategies.append(
                SearchStrategyV1(
                    id=strategy_id("local", country_code),
                    label=f"Authorized work in {target.country_name}",
                    kind="authorized_local",
                    threshold=75,
                    country_code=country_code,
                    country_name=target.country_name,
                    cities=target.cities,
                )
            )

    if mobility.willing_to_relocate:
        for target in mobility.relocation_targets:
            if target.country_code in mobility.work_authorizations:
                continue
            strategies.append(
                SearchStrategyV1(
                    id=strategy_id("relocate", target.country_code),
                    label=f"Relocation to {target.country_name}",
                    kind="relocation",
                    threshold=70,
                    country_code=target.country_code,
                    country_name=target.country_name,
                    cities=target.cities,
                    requires_sponsorship=mobility.sponsorship_required_outside_authorized_countries,
                )
            )

    if mobility.remote_from_current_country:
        strategies.append(
            SearchStrategyV1(
                id=strategy_id("remote-from", mobility.current_country_code),
                label=f"Remote from {candidate.location.country_name}",
                kind="remote",
                threshold=75,
                country_code=mobility.current_country_code,
                country_name=candidate.location.country_name,
            )
        )
    strategies.append(SearchStrategyV1(id="other", label="Other opportunities", kind="other", threshold=80))
    return strategies


def candidate_schema() -> dict[str, Any]:
    return CandidateProfileV1.model_json_schema()


CV_CONVERSION_PROMPT = """Convert my CV into CandidateProfileV1 JSON that validates against the supplied JSON Schema.
Use only facts explicitly present in the CV. Do not infer dates, metrics, skills, contact details, locations,
work authorization, or proficiency. Use empty strings or empty lists for missing optional information.
Set schema_version to \"1.0\" and return JSON only.
"""

SETUP_PLANNING_PROMPT = """Help a candidate plan a RoleBeacon setup. Return only one valid SetupPayloadV1 JSON object, with no Markdown or explanation.

Preserve the supplied candidate profile exactly. Use only stated facts for work authorization, relocation, remote-work eligibility, compensation, and seniority. Do not infer a work right, visa sponsorship, salary, or location permission. For countries where the candidate requires a visa or sponsorship, add them only as relocation targets, not as work authorizations. Work authorizations and current country must use ISO 3166-1 alpha-2 codes. Relocation targets may additionally use `EUROPE` with the country name `Europe` when the candidate wants Europe-wide relocation. Include focused target roles, relevant preferred skills and domains, and a conservative company priority/watch list based on the candidate's stated goals. Keep `activate` false so the candidate reviews the configuration before any source is contacted."""
