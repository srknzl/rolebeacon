from __future__ import annotations

import io
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from rolebeacon.config import Settings
from rolebeacon.setup import SetupService
from rolebeacon.terminal import Cancelled, GoBack, Terminal
from rolebeacon.wizard import SetupWizard


class Console:
    """Answer wizard prompts by matching the text the wizard just printed.

    Rules are consumed in order per prompt fragment; any prompt without a rule receives a blank
    line, which every prompt reads as "keep the current value" or "finish this list".
    """

    def __init__(
        self,
        answers: Mapping[str, Iterable[str]] | None = None,
        *,
        secrets: Iterable[str] = (),
        limit: int = 400,
    ) -> None:
        self.answers = {key: list(values) for key, values in (answers or {}).items()}
        self.secrets = list(secrets)
        self.output = io.StringIO()
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []
        self._read_upto = 0
        self._last_prompt = ""
        self._limit = limit

    def read(self) -> str:
        written = self.output.getvalue()
        prompt = written[self._read_upto :]
        self._read_upto = len(written)
        # ask_multiline reads several lines under one printed prompt, so an empty slice means
        # "still answering the previous question".
        self._last_prompt = prompt.strip() or self._last_prompt
        self.prompts.append(self._last_prompt)
        if len(self.prompts) > self._limit:
            raise AssertionError(f"The wizard asked more than {self._limit} questions: {self._last_prompt!r}")
        for fragment, values in self.answers.items():
            if fragment in self._last_prompt and values:
                return values.pop(0)
        return ""

    def read_secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        return self.secrets.pop(0) if self.secrets else ""

    def terminal(self) -> Terminal:
        return Terminal(stream=self.output, reader=self.read, secret_reader=self.read_secret)

    @property
    def text(self) -> str:
        return self.output.getvalue()


HAPPY_PATH: dict[str, list[str]] = {
    "Full name": ["Ada Lovelace"],
    "Current country": ["TR"],
    "Skill category name": ["Languages"],
    "Skill in Languages": ["Python"],
    "Target role": ["Backend Engineer"],
    "Country where you can work today": ["TR"],
    "Source packs": ["1"],
    "Public feeds and official searches": ["1"],
}


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings.load()
    settings.ensure_directories()
    return settings


def run_wizard(tmp_path: Path, console: Console) -> tuple[SetupWizard, dict | None]:
    wizard = SetupWizard(settings_for(tmp_path), console.terminal())
    return wizard, wizard.run()


def test_guided_path_saves_the_profile_without_activating_by_default(tmp_path) -> None:
    console = Console(HAPPY_PATH)
    wizard, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert summary["setup_complete"] is True
    # The final activation prompt defaults to no, so an empty answer must never contact a source.
    assert summary["activated"] is False
    assert summary["scoring_mode"] == "rules"
    assert summary["enabled_source_ids"]
    saved = Settings.load()
    assert saved.load_candidate_profile()["name"] == "Ada Lovelace"
    assert saved.load_candidate_profile()["skills"] == {"Languages": ["Python"]}
    assert saved.load_mobility_profile()["work_authorizations"] == ["TR"]
    assert saved.load_search_profile()["target_roles"] == ["Backend Engineer"]


def test_explicit_confirmation_activates_collection(tmp_path) -> None:
    console = Console({**HAPPY_PATH, "Activate scheduled collection": ["y"]})
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert summary["activated"] is True


@pytest.mark.parametrize(
    "cancel_at",
    [
        "How do you want to start",
        "Full name",
        "Target role",
        "Source packs",
        "Scoring engine",
        "What next",
    ],
)
def test_cancelling_any_stage_saves_nothing(tmp_path, cancel_at: str) -> None:
    console = Console({**HAPPY_PATH, cancel_at: ["q"]})
    _, summary = run_wizard(tmp_path, console)

    assert summary is None
    assert "Setup cancelled" in console.text
    assert not Settings.load().setup_complete
    assert not Settings.load().setup_state_path.exists()


def test_back_returns_to_the_previous_step_and_keeps_earlier_answers(tmp_path) -> None:
    # Enter the eligibility step, go back to the profile, then continue with the saved name shown
    # as the default rather than being asked for it again.
    console = Console({**HAPPY_PATH, "Target role": ["b", "Backend Engineer"]})
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert Settings.load().load_candidate_profile()["name"] == "Ada Lovelace"
    assert "Step 2 of 6 — Profile" in console.text
    assert console.text.count("Step 3 of 6 — Eligibility") == 2


def test_back_from_the_first_step_cancels_without_saving(tmp_path) -> None:
    console = Console({"How do you want to start": ["b"]})
    _, summary = run_wizard(tmp_path, console)

    assert summary is None
    assert not Settings.load().setup_complete


def test_review_jumps_back_to_a_chosen_step(tmp_path) -> None:
    console = Console({**HAPPY_PATH, "What next": ["profile", "save"], "Full name": ["Ada Lovelace", "Grace Hopper"]})
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert Settings.load().load_candidate_profile()["name"] == "Grace Hopper"


def test_invalid_input_is_rejected_with_every_error_listed(tmp_path) -> None:
    console = Console(
        {
            **HAPPY_PATH,
            "Current country": ["Atlantis", "TR"],
            "Daily review limit": ["nine", "0", "20"],
            "Role match": ["70", "30"],
        }
    )
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert "No entry matches 'Atlantis'" in console.text
    assert "Enter a whole number between 1 and 100" in console.text
    assert "The weights total 140. They must total 100." in console.text
    assert Settings.load().load_search_profile()["daily_review_limit"] == 20


def test_secret_input_never_reaches_the_terminal_output(tmp_path) -> None:
    console = Console(
        {**HAPPY_PATH, "Scoring engine": ["ollama"]},
        secrets=["super-secret-key", "brave-secret-key"],
    )
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert "super-secret-key" not in console.text
    assert "brave-secret-key" not in console.text
    assert Settings.load().llm_api_key == "super-secret-key"
    assert Settings.load().company_search_api_key == "brave-secret-key"
    # The saved payload the wizard would rehydrate from must not carry the secret back either.
    assert "super-secret-key" not in json.dumps(SetupService(Settings.load()).saved_payload())


def test_a_save_failure_reports_a_redacted_message_instead_of_the_key(tmp_path, monkeypatch) -> None:
    console = Console(
        {**HAPPY_PATH, "Scoring engine": ["ollama"], "Endpoint base URL": ["not-a-url"]},
        secrets=["super-secret-key", ""],
    )
    wizard = SetupWizard(settings_for(tmp_path), console.terminal())

    def refuse(value):
        raise ValueError(f"llm.base_url is invalid [input_value={value['llm']['api_key']}]")

    monkeypatch.setattr(wizard.service, "complete", refuse)
    summary = wizard.run()

    assert summary is None
    assert "super-secret-key" not in console.text
    assert "***" in console.text
    assert "Setup was not saved." in console.text
    assert not Settings.load().setup_complete


def test_a_blank_key_preserves_the_stored_one(tmp_path) -> None:
    run_wizard(tmp_path, Console({**HAPPY_PATH, "Scoring engine": ["ollama"]}, secrets=["first-key", ""]))
    assert Settings.load().llm_api_key == "first-key"

    run_wizard(tmp_path, Console({"What next": ["save"]}, secrets=["", ""]))
    assert Settings.load().llm_api_key == "first-key"

    run_wizard(tmp_path, Console({"What next": ["save"]}, secrets=["-", ""]))
    assert Settings.load().llm_api_key == ""


def test_source_step_explains_coverage_and_offers_packs(tmp_path) -> None:
    console = Console(HAPPY_PATH)
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert "contacted only after you activate collection" in console.text
    assert "official boards" in console.text
    assert "No source covers:" in console.text
    # A selected pack installs its own sources, and only at save time.
    assert len(summary["enabled_source_ids"]) > 1


def test_pasted_setup_json_is_validated_and_never_activates(tmp_path) -> None:
    document = {
        "candidate": {
            "schema_version": "1.0",
            "name": "Ada Lovelace",
            "location": {"country_code": "TR", "country_name": "Türkiye"},
        },
        "mobility": {"schema_version": "1.0", "current_country_code": "TR", "work_authorizations": ["TR"]},
        "preferences": {"schema_version": "1.0", "target_roles": ["Backend Engineer"]},
        "enabled_source_ids": [],
        "llm": {"mode": "rules", "api_key": "pasted-key"},
        "activate": True,
    }
    console = Console(
        {
            "How do you want to start": ["paste"],
            "Paste the JSON": ["{not json}", "END", json.dumps(document), "END"],
            "Public feeds and official searches": ["1"],
        }
    )
    wizard, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert "That is not valid JSON" in console.text
    assert summary["activated"] is False
    assert "pasted-key" not in console.text
    assert Settings.load().llm_api_key == ""
    assert Settings.load().load_search_profile()["target_roles"] == ["Backend Engineer"]


def test_pasted_setup_json_reports_schema_errors(tmp_path) -> None:
    console = Console(
        {
            "How do you want to start": ["paste"],
            "Paste the JSON": [json.dumps({"candidate": {"name": ""}}), "END"],
            "Paste a corrected document?": ["n"],
            **HAPPY_PATH,
        }
    )
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert "mobility: Field required" in console.text
    assert "Continuing with the guided questions instead." in console.text


def test_missing_facts_block_saving_until_they_are_supplied(tmp_path) -> None:
    # The first pass selects no source, so the review step must not offer "save" at all; the
    # second pass, after jumping back to the source step, can save.
    console = Console(
        {
            "Full name": ["Ada Lovelace"],
            "Current country": ["TR"],
            "Target role": ["Backend Engineer"],
            "Country where you can work today": ["TR"],
            "Public feeds and official searches": ["", "1"],
            "What next": ["sources", "save"],
        }
    )
    _, summary = run_wizard(tmp_path, console)

    assert "Missing — Sources: No source selected" in console.text
    assert "Setup cannot be saved until the missing facts above are supplied." in console.text
    assert summary is not None
    assert len(summary["enabled_source_ids"]) == 1


def test_ambiguous_facts_are_named_without_blocking_the_save(tmp_path) -> None:
    console = Console({**HAPPY_PATH, "Reject postings below that minimum": ["y"]})
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    assert "Ambiguous — Salary: Hard filter enabled without a minimum" in console.text
    assert "Ambiguous — Security clearance: Unknown" in console.text


def test_editing_an_existing_setup_preserves_saved_values(tmp_path) -> None:
    run_wizard(tmp_path, Console(HAPPY_PATH))
    console = Console({"Headline": ["Backend engineer"], "What next": ["save"]})
    _, summary = run_wizard(tmp_path, console)

    assert summary is not None
    profile = Settings.load().load_candidate_profile()
    assert profile["name"] == "Ada Lovelace"
    assert profile["headline"] == "Backend engineer"
    assert profile["skills"] == {"Languages": ["Python"]}
    assert Settings.load().load_search_profile()["target_roles"] == ["Backend Engineer"]


def test_the_clear_word_cannot_become_a_country_code(tmp_path) -> None:
    """"-" is the documented clear word, so it is tried here - and a country is required."""
    console = Console({**HAPPY_PATH, "Current country": ["-", "TR"]})

    _, summary = run_wizard(tmp_path, console)

    assert summary is not None  # rejected at the prompt, not five steps later at the save
    assert "This value is required" in console.text
    location = Settings.load().load_candidate_profile()["location"]
    assert (location["country_code"], location["country_name"]) == ("TR", "Türkiye")
    assert Settings.load().load_mobility_profile()["current_country_code"] == "TR"


def test_the_clear_word_keeps_a_stored_country_out_of_the_draft(tmp_path) -> None:
    """Re-running setup and answering "-" must not replace a saved country with the sentinel."""
    run_wizard(tmp_path, Console(HAPPY_PATH))

    _, summary = run_wizard(tmp_path, Console({"Current country": ["-", "DE"], "What next": ["save"]}))

    assert summary is not None
    assert Settings.load().load_candidate_profile()["location"]["country_code"] == "DE"


def test_the_wizard_and_the_json_import_produce_the_same_setup(tmp_path, monkeypatch) -> None:
    run_wizard(tmp_path, Console({**HAPPY_PATH, "Source packs": []}))
    from_wizard = Settings.load()
    payload = SetupService(from_wizard).saved_payload()
    payload["llm"].pop("api_key_configured", None)

    monkeypatch.setenv("ROLEBEACON_DATA_DIR", str(tmp_path / "imported"))
    imported_settings = Settings.load()
    imported_settings.ensure_directories()
    imported = SetupService(imported_settings)
    from_import = imported.complete(payload)

    assert from_import.load_candidate_profile() == from_wizard.load_candidate_profile()
    assert from_import.load_mobility_profile() == from_wizard.load_mobility_profile()
    assert from_import.load_search_profile() == from_wizard.load_search_profile()
    assert imported.review(payload) == SetupService(from_wizard).review(payload)
    assert imported.validate_setup_payload(payload) == SetupService(from_wizard).validate_setup_payload(payload)


def test_terminal_cancel_and_back_words_are_recognized_at_every_prompt() -> None:
    for word, expected in (("q", Cancelled), ("quit", Cancelled), ("b", GoBack), ("back", GoBack)):
        console = Console({"Anything": [word]})
        with pytest.raises(expected):
            console.terminal().ask_text("Anything")


def test_terminal_end_of_input_cancels_rather_than_crashing() -> None:
    def refuse() -> str:
        raise EOFError

    terminal = Terminal(stream=io.StringIO(), reader=refuse)
    with pytest.raises(Cancelled):
        terminal.ask_text("Anything")
