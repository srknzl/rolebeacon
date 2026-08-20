"""Terminal prompts for the interactive setup wizard.

Standard library only: the wizard needs numbered lists, masked secrets, and a country filter,
and none of that is worth a new runtime dependency. Every prompt accepts `b` to return to the
previous step and `q` to abandon the wizard, so navigation and cancellation are uniform.
"""

from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

# A repeated prompt numbers its entries ("Target role (2)"); the field is still one question.
QUESTION_INDEX = re.compile(r"\s*\(\d+\)$")

CANCEL_WORDS = frozenset({"q", "quit", "cancel"})
BACK_WORDS = frozenset({"b", "back"})
CLEAR_WORD = "-"


class Cancelled(Exception):
    """The user abandoned the wizard. Nothing has been written."""


class GoBack(Exception):
    """The user asked to return to the previous step."""


class Terminal:
    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        reader: Callable[[], str] | None = None,
        secret_reader: Callable[[str], str] | None = None,
        page_size: int = 12,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._reader = reader if reader is not None else input
        # Secrets are read through a separate callable so they never pass through the echoing
        # reader, and so tests can supply a value without it appearing in captured output.
        self._secret_reader = secret_reader if secret_reader is not None else getpass.getpass
        self._page_size = page_size
        # Plain text off a terminal, so captured output and pipes stay byte-for-byte assertable.
        self._colour = bool(getattr(self._stream, "isatty", lambda: False)())
        self._question = 0
        self._questions_total = 0
        self._question_key = ""

    # Output ---------------------------------------------------------------

    def write(self, text: str = "") -> None:
        print(text, file=self._stream)

    def _style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._colour else text

    def note(self, text: str) -> None:
        if text:
            self.write(f"  {self._style(text, '2')}")

    def error(self, text: str) -> None:
        self.write(f"  {self._style(f'! {text}', '31')}")

    def heading(self, text: str) -> None:
        self.write()
        self.write(self._style(text, "1"))
        self.write("-" * len(text))

    def step_header(self, index: int, total: int, title: str) -> None:
        self._question = 0
        self._questions_total = 0
        self._question_key = ""
        self.heading(f"Step {index} of {total} — {title}")

    def expect_questions(self, total: int) -> None:
        """Declare how many questions this step asks, so each prompt can say where it is.

        A step whose length depends on the answers declares nothing and its prompts are numbered
        without a total, which is the only honest thing to print when the end is not yet known.
        """
        self._questions_total = total

    def list_hint(self, default: Sequence[str] = (), *, required: bool = False) -> None:
        """How to answer a repeated prompt. On a first run there is nothing to keep, so offering
        a blank line as the way to "keep the current values" contradicted the same field's
        "Required" and described Enter as preserving something that does not exist yet.
        """
        if default:
            self.note(f"One per line. Blank line keeps the current values; {CLEAR_WORD} clears them.")
        elif required:
            self.note("One per line. Required — enter at least one, then a blank line.")
        else:
            self.note("One per line. Blank line when you are done.")

    def start_question(self, prompt: str, help_text: str = "") -> None:
        """Open a question: a blank line separates it from the previous answer, then its own hint.

        The hint used to print straight after the previous answer, which read as feedback on what
        had just been typed rather than as guidance for what to type next. Callers that print their
        own preamble open the question themselves; every `ask_*` does it for you.
        """
        key = QUESTION_INDEX.sub("", prompt)
        if key != self._question_key:
            self._question_key = key
            self._question += 1
            self.write()
        self.note(help_text)

    def _position(self) -> str:
        if not self._question:
            return ""
        if self._questions_total and self._question <= self._questions_total:
            return f"[{self._question}/{self._questions_total}] "
        return f"[{self._question}] "

    # Input ----------------------------------------------------------------

    def _read(self, prompt: str) -> str:
        print(f"{self._position()}{prompt}: ", end="", file=self._stream, flush=True)
        try:
            answer = self._reader()
        except (EOFError, KeyboardInterrupt) as error:
            raise Cancelled("Setup cancelled. Nothing was saved.") from error
        answer = answer.strip()
        # Accepted cost of uniform navigation: these words can never be entered as data, so a
        # company named "Q" or an exclude phrase "cancel" cannot be typed here. A prefix such as
        # ":q" would fix it but makes every other prompt worse, and the values are rare enough
        # that the documented `rolebeacon setup --from-json PATH` is the escape hatch for them.
        if answer.casefold() in CANCEL_WORDS:
            raise Cancelled("Setup cancelled. Nothing was saved.")
        if answer.casefold() in BACK_WORDS:
            raise GoBack()
        return answer

    def ask_text(self, prompt: str, *, default: str = "", required: bool = False, help_text: str = "") -> str:
        self.start_question(prompt, help_text)
        while True:
            answer = self._read(f"{prompt} [{default}]" if default else prompt)
            if answer == CLEAR_WORD:
                answer = ""
            elif not answer:
                answer = default
            if answer or not required:
                return answer
            self.error("This value is required.")

    def ask_lines(
        self,
        prompt: str,
        *,
        default: Sequence[str] = (),
        required: bool = False,
        help_text: str = "",
    ) -> list[str]:
        self.start_question(prompt, help_text)
        if default:
            self.note(f"current: {', '.join(default)}")
        self.list_hint(default, required=required)
        values: list[str] = []
        while True:
            answer = self._read(f"{prompt} ({len(values) + 1})")
            if answer == CLEAR_WORD:
                if required:
                    self.error("At least one value is required.")
                    continue
                return []
            if not answer:
                result = values or list(default)
                if result or not required:
                    return result
                self.error("At least one value is required.")
                continue
            values.append(answer)

    def ask_bool(self, prompt: str, *, default: bool, help_text: str = "") -> bool:
        self.start_question(prompt, help_text)
        while True:
            answer = self._read(f"{prompt} [{'Y/n' if default else 'y/N'}]").casefold()
            if not answer:
                return default
            if answer in {"y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False
            self.error("Answer y or n.")

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        return self.ask_bool(prompt, default=default)

    def ask_int(self, prompt: str, *, default: int, minimum: int, maximum: int, help_text: str = "") -> int:
        self.start_question(prompt, help_text)
        while True:
            answer = self._read(f"{prompt} [{default}]")
            if not answer:
                return default
            try:
                value = int(answer)
            except ValueError:
                self.error(f"Enter a whole number between {minimum} and {maximum}.")
                continue
            if minimum <= value <= maximum:
                return value
            self.error(f"Enter a whole number between {minimum} and {maximum}.")

    def ask_optional_number(self, prompt: str, *, default: float | None, help_text: str = "") -> float | None:
        self.start_question(prompt, help_text)
        while True:
            shown = "none" if default is None else str(default)
            answer = self._read(f"{prompt} [{shown}]")
            if not answer:
                return default
            if answer == CLEAR_WORD:
                return None
            try:
                value = float(answer)
            except ValueError:
                self.error(f"Enter a number, or {CLEAR_WORD} for none.")
                continue
            if value >= 0:
                return value
            self.error("Enter a value of zero or more.")

    def ask_private(self, prompt: str, *, help_text: str = "") -> str:
        """Read a secret without echoing it and without routing it through the visible reader."""
        self.start_question(prompt, help_text)
        try:
            return self._secret_reader(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt) as error:
            raise Cancelled("Setup cancelled. Nothing was saved.") from error

    def ask_multiline(self, prompt: str, *, terminator: str = "END", help_text: str = "") -> str:
        self.start_question(prompt, help_text)
        self.note(f"Paste the document, then enter {terminator} on its own line.")
        self.write(f"{prompt}:")
        lines: list[str] = []
        while True:
            try:
                line = self._reader()
            except (EOFError, KeyboardInterrupt) as error:
                raise Cancelled("Setup cancelled. Nothing was saved.") from error
            if line.strip() == terminator:
                return "\n".join(lines)
            if line.strip().casefold() in CANCEL_WORDS and not lines:
                raise Cancelled("Setup cancelled. Nothing was saved.")
            lines.append(line)

    def ask_choice(
        self,
        prompt: str,
        options: Sequence[tuple[str, str]],
        *,
        default: str = "",
        help_text: str = "",
    ) -> str:
        self.start_question(prompt, help_text)
        codes = [code for code, _ in options]
        for position, (code, label) in enumerate(options, start=1):
            self.write(f"  {position:>2}) {label}{' (current)' if code == default else ''}")
        while True:
            answer = self._read(prompt)
            if not answer and default:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return codes[int(answer) - 1]
            if answer.casefold() in {code.casefold() for code in codes}:
                return next(code for code in codes if code.casefold() == answer.casefold())
            self.error(f"Choose a number between 1 and {len(options)}.")

    def ask_multi_choice(
        self,
        prompt: str,
        options: Sequence[tuple[str, str]],
        *,
        selected: Sequence[str] = (),
        help_text: str = "",
    ) -> list[str]:
        """Toggle entries in a paginated numbered list; long catalogs must stay readable."""
        self.start_question(prompt, help_text)
        known = {code for code, _ in options}
        chosen = {code for code in selected if code in known}
        pages = max(1, -(-len(options) // self._page_size))
        page = 0
        while True:
            start = page * self._page_size
            visible = options[start : start + self._page_size]
            for position, (code, label) in enumerate(visible, start=start + 1):
                self.write(f"  {position:>3}) [{'x' if code in chosen else ' '}] {label}")
            if pages > 1:
                self.write(f"  Page {page + 1} of {pages}. {len(chosen)} selected.")
            else:
                self.write(f"  {len(chosen)} selected.")
            paging = " · n next page · p previous page" if pages > 1 else ""
            answer = self._read(f"{prompt} (numbers toggle · a all{paging} · Enter when done)")
            if not answer:
                return [code for code, _ in options if code in chosen]
            command = answer.casefold()
            if command == "n":
                page = (page + 1) % pages
                continue
            if command == "p":
                page = (page - 1) % pages
                continue
            if command == "a":
                chosen.update(code for code, _ in visible)
                continue
            if command == CLEAR_WORD:
                chosen.clear()
                continue
            picks = [item for item in answer.replace(",", " ").split() if item.isdigit()]
            if not picks:
                self.error("Enter one or more numbers from the list, or a command.")
                continue
            for pick in picks:
                index = int(pick) - 1
                if not 0 <= index < len(options):
                    self.error(f"{pick} is not in the list.")
                    continue
                code = options[index][0]
                if code in chosen:
                    chosen.discard(code)
                else:
                    chosen.add(code)

    def ask_from_catalog(
        self,
        prompt: str,
        catalog: Sequence[tuple[str, str]],
        *,
        help_text: str = "",
    ) -> tuple[str, str] | None:
        """Resolve one catalog entry by exact code or by filtering on a typed fragment."""
        self.start_question(prompt, help_text)
        while True:
            answer = self._read(prompt)
            if not answer:
                return None
            if answer == CLEAR_WORD:
                return CLEAR_WORD, CLEAR_WORD
            needle = answer.casefold()
            exact = [entry for entry in catalog if entry[0].casefold() == needle]
            if exact:
                return exact[0]
            matches = [entry for entry in catalog if needle in entry[1].casefold()]
            if not matches:
                self.error(f"No entry matches {answer!r}.")
                continue
            if len(matches) == 1:
                return matches[0]
            if len(matches) > self._page_size:
                self.error(f"{len(matches)} entries match {answer!r}. Type more of the name.")
                continue
            position = (self._question, self._question_key)
            code = self.ask_choice(
                "Which one",
                [(entry[0], f"{entry[1]} — {entry[0]}") for entry in matches],
            )
            self._question, self._question_key = position
            return next(entry for entry in matches if entry[0] == code)
