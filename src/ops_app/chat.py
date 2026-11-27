"""The conversation on a case (case chat spec §4): a duty manager's questions and the Case
Assistant's answers, kept with the case. Asking grants no authority; a turn never changes the
case version. The decision log names each turn by its id and never holds the question."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from airline_systems.caller import Caller
from ops_app.case_digest import case_digest, visible_plans
from ops_app.engine import CloudDependencyError, Gateway
from ops_app.store import CaseStore
from ops_app.trail import AGENTS, run_steps

MAX_QUESTION = 500
MAX_ANSWER = 4000
MAX_TURNS = 50
EARLIER_TURNS = 6
ANSWER_SECONDS = 120.0
RETRY_SECONDS = 15.0
# A turn pending longer (ARROW restarted while it ran) fails when the next question arrives.
STALE = timedelta(minutes=5)
INTERRUPTED = "No answer (interrupted); ask again"
MAX_DETAIL = 300
ASSISTANT = {"actor": "case-assistant", "autonomy": "L2"}
TITLES = {"read_case": "Read the case", "what_if": "Asked ARROW for a what-if plan"}
ROUTES = {"occ": "OCC decides", "auto": "runs automatically", "blocked": "blocked"}
_FOUNDRY = {agent.key: agent.foundry for agent in AGENTS}
_A2A = {agent.key: agent.tool for agent in AGENTS}
# A plan number however an answer words it: "plan 3", "plan is number 3", "plan #3", "plan no. 3",
# "plans 2 and 3", "plan three", "a third plan"; never a figure such as "plan, 30 min later".
_COUNTS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_COUNTS |= {"six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
_ORDINALS |= {"sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_UNITS = (
    r"(?:%|(?:mins?|minutes?|h|hrs?|hours?|days?|percent|passengers?|people|flights?|legs?"
    r"|crews?|aircraft|seats?|kt|knots|km|eur|euros?)\b)"
)
_NUMBER = rf"(?:\d+|{'|'.join(_COUNTS)})\b(?!\s*{_UNITS})"
_PLAN_NUMBER = re.compile(
    r"\b(plans?)\b(?:\s+(?:is|was|becomes|became|would\s+be|will\s+be))?"
    rf"(?:\s+(?:number|num\.?|no\.?|nr\.?))?\s*#?\s*({_NUMBER})",
    re.IGNORECASE,
)
_AND_PLAN = re.compile(rf"\s*(?:and|or|&)\s*({_NUMBER})", re.IGNORECASE)
_AND_PLANS = re.compile(rf"\s*(?:,|and|or|&|to|-|–)\s*({_NUMBER})", re.IGNORECASE)
_NTH_PLAN = re.compile(
    rf"\b({'|'.join(_ORDINALS)}|\d+(?:st|nd|rd|th))\s+(?:(?:new|what[\s-]?if)\s+)*plan\b",
    re.IGNORECASE,
)
# Curly quotes and Unicode hyphens read as their plain forms ("I’ve", “Delay …”, "what‑if").
_PLAIN = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2010": "-", "\u2011": "-"}
)
_SPACE = re.compile(r"\s+")
# An answer saying ARROW made a plan: "the new plan", "I've run a what-if", "I asked ARROW for the
# what-if", "this adds a plan", "the what-if plan was added", "this would add a plan", "a what-if
# plan would be added".
_MADE_PLAN = re.compile(
    r"\bnew\s+(?:what[\s-]?if\s+)?plan\b"
    r"|\b(?:I|we|ARROW)(?:'ve|\s+(?:have|has|had))?\s+(?:now\s+|just\s+|already\s+)?"
    r"(?:run|ran|built|created|added|made)\s+(?:(?:a|an|the|this|that|your|one)\s+)?"
    r"(?:new\s+)?what[\s-]?if\b"
    r"|\basked\s+ARROW\s+for\s+(?:a|an|the|this|that|your)\s+(?:new\s+)?what[\s-]?if\b"
    r"|\bwhat[\s-]?if(?:\s+plan)?\s+(?:has\s+been|was|is\s+now)\s+(?:run|built|created|added)\b"
    r"|\b(?:adds|added)\s+(?:a|one|another)\s+(?:new\s+)?(?:what[\s-]?if\s+)?plan\b"
    r"|(?:\b(?:would|will)|'d|'ll)\s+(?:now\s+)?add\s+(?:a|one|another)\s+(?:new\s+)?"
    r"(?:what[\s-]?if\s+)?plan\b"
    r'|\b(?:what[\s-]?if(?:\s+plan)?|plan)(?:\s+\d+)?(?:\s+(?:"[^"\n]{1,60}"|\([^)\n]{1,60}\)))*'
    r"\s+(?:would|will)\s+(?:now\s+)?be\s+added\b",
    re.IGNORECASE,
)
# What makes such words no claim, within their own clause (which ends at a sentence's end, a
# comma, semicolon or colon, a dash, "but" or "so"; never inside "13:45" or "€123,242"): a
# negation before them ("ARROW hasn't built a new plan"), a negated result after them ("a new
# plan cannot be built", "is not possible", "was not needed", "does not exist"), an offer before
# them ("if you want a new plan"), a request ("ask ARROW for a new plan", "I would ask"), or a
# what-if refused in so many words ("the what-if was declined"). An indefinite "a new plan" that
# names no euros is no claim either when its sentence points to a what-if question or says when
# one is possible. A bare "if" is no hedge: invented results read "If maintenance takes 1 hour
# more, the new plan costs €…"; nor is a modal before any other verb ("I can confirm", "should").
_SENTENCE_END = re.compile(r"[.!?][*\"')\]]*(?=\s|$)|\n")
_CLAUSE_END = re.compile(
    r"[.!?][*\"')\]]*(?=\s|$)|\n|[,:](?=\s|$)|;|—|\s[–-]\s|\b(?:but|so)\b", re.IGNORECASE
)
_NEGATION = re.compile(
    r"\b(?:not|never|cannot|unable|without|nor|none|nothing|neither)\b|n't\b|\bno\b(?!\.?\s*#?\d)",
    re.IGNORECASE,
)
_DENIED = re.compile(
    r"(?:\b(?:not|never|cannot|none)\b|n't\b)(?:\s+\w+){0,3}?\s+(?:be\s+|been\s+)?"
    r"(?:built|added|made|created|possible|needed|exists?)\b",
    re.IGNORECASE,
)
_OFFER = re.compile(r"\bif\s+you(?:'d|\s+would)?\s+(?:want|like|wish|prefer)\b", re.IGNORECASE)
_REQUEST = re.compile(
    r"\b(?:ask|request)\b(?:\s+(?:ARROW|me|us))?(?:\s+for)?\s+(?:a|an|another)\s+$"
    r"|(?:\b(?:would|could|can|may|might|should|will)|'d|'ll)\s+(?:ask|request)\b",
    re.IGNORECASE,
)
_WHAT_IF_REFUSED = re.compile(
    r"\bwhat[\s-]?if(?:\s+(?:plan|request))?\s+(?:was|has\s+been|had\s+been|is|got)\s+"
    r"(?:refused|declined|rejected)\b",
    re.IGNORECASE,
)
_INDEFINITE = re.compile(r"\b(?:a|another)\s+$", re.IGNORECASE)
_EUROS = re.compile(r"€|\beur(?:os?)?\b", re.IGNORECASE)
_POINTER = re.compile(
    r"\bask\b.*\bwhat[\s-]?if\b|\bwhat[\s-]?if\b.*\bask\b"
    r"|\bonly\s+possible\b|\bpossible\s+(?:only|while|when|once|if|until)\b",
    re.IGNORECASE,
)

logger = logging.getLogger("ops_app")


class ChatBusy(RuntimeError):
    """The case takes no question now: an answer is pending, or it holds the most turns."""


def _short(text: str) -> str:
    return text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…"


def _object(value: Any) -> Mapping[str, Any] | None:
    """A JSON object, or one written as text; None for anything else."""
    if isinstance(value, Mapping):
        return value
    try:
        found = json.loads(str(value))
    except ValueError:
        return None
    return found if isinstance(found, Mapping) else None


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        named = [
            str(item.get("option_id") or item.get("kind") or "item")
            if isinstance(item, Mapping)
            else str(item)
            for item in value
        ]
        return ", ".join(named) or "none"
    return "none" if value is None or value == "" else str(value)


def _arguments(arguments: Mapping[str, Any]) -> str:
    """A tool's arguments in words: the case number, then each lever. The agent's wording of
    the question is left out: the turn keeps the duty manager's own."""
    parts: list[str] = []

    def add(values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            # An empty lever (no standby crew) reads as it did when it was null: not at all.
            if key == "question" or value is None or value == "" or value == []:
                continue
            if isinstance(value, Mapping):
                add(value)
            else:
                parts.append(str(value) if key == "case_id" else f"{key} {_value(value)}")

    add(arguments)
    return " · ".join(parts)


def _plan(output: Any) -> str:
    """ARROW's answer to a what-if: the plan by its card's number, and its route in words. A
    long answer is clipped in the run's record, so a number and route are also read from text."""
    plan = _object(output)
    if plan is None:
        text = str(output)
        number_match = re.search(r'"number":\s*(\d+)', text)
        route_match = re.search(r'"route":\s*"(\w+)"', text)
        plan = {
            "number": int(number_match.group(1)) if number_match else None,
            "route": route_match.group(1) if route_match else None,
        }
    number = plan.get("number")
    named = f"Plan {number}" if type(number) is int else "A plan"
    route = ROUTES.get(str(plan.get("route")))
    return f"{named} · {route}" if route else named


def _said(answer: str) -> str:
    """A domain agent's answer in words: its reason, else its first fact; plain text as it is."""
    found = _object(answer)
    if found is None:
        return answer
    recommendation = found.get("recommendation")
    reasons = [
        recommendation.get("reason") if isinstance(recommendation, Mapping) else None,
        found.get("reason"),
        found.get("summary"),
    ]
    for reason in reasons:
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    facts = found.get("facts")
    for key, value in (facts if isinstance(facts, Mapping) else found).items():
        if not isinstance(value, Mapping) and key != "recommendation":
            return f"{str(key).replace('_', ' ')}: {_value(value)}"
    return "answered"


def _calls(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What the run did, in order and in words: ARROW's tools it called and the agents it asked,
    each with its endpoint (the MCP tool, or the agent's A2A connection)."""
    calls: list[dict[str, Any]] = []
    for step in run_steps(items):
        if step["kind"] == "tool":
            name = str(step["tool"])
            detail = _arguments(step["raw"]["arguments"])
            # A case digest says nothing new here; a what-if's plan or a refusal does.
            if not step["ok"]:
                # Foundry returns a tool's refusal as "Error executing tool <name>: <reason>".
                reason = re.sub(r"^Error executing tool \S+:\s*", "", str(step["result"]))
                detail += f" → Refused: {reason}"
            elif name == "what_if":
                detail += f" → {_plan(step['raw']['output'])}"
            calls.append(
                {
                    "kind": "tool",
                    "name": name,
                    "title": TITLES.get(name, step["title"]),
                    "access": "adds a plan" if name == "what_if" else "read",
                    "endpoint": f"MCP {name}",
                    "detail": _short(detail),
                }
            )
        elif step["kind"] == "ask":
            key = str(step["agent"])
            question = _short(str(step["detail"]))
            answer = _said(str(step.get("answer") or ""))
            calls.append(
                {
                    "kind": "agent",
                    "name": _FOUNDRY.get(key, "agent"),
                    "title": step["title"],
                    "access": "read",
                    "endpoint": f"A2A {_A2A[key]}" if key in _A2A else "A2A",
                    "detail": f"{question} → {_short(answer)}" if answer else question,
                }
            )
    return calls


def _number(word: str) -> int:
    word = word.lower()
    return _COUNTS.get(word) or _ORDINALS.get(word) or int(re.sub(r"\D", "", word))


def _plan_numbers(answer: str) -> set[int]:
    """Every plan number the answer names, however it words it."""
    found: set[int] = set()
    for match in _PLAN_NUMBER.finditer(answer):
        found.add(_number(match.group(2)))
        joined = _AND_PLANS if match.group(1).lower() == "plans" else _AND_PLAN
        position = match.end()
        while more := joined.match(answer, position):
            found.add(_number(more.group(1)))
            position = more.end()
    found.update(_number(match.group(1)) for match in _NTH_PLAN.finditer(answer))
    return found


def _bounds(ends: re.Pattern[str], text: str, start: int, end: int) -> tuple[int, int]:
    """Where the clause or sentence (``ends``) holding text[start:end] begins and ends."""
    begin = max((found.end() for found in ends.finditer(text, 0, start)), default=0)
    after = ends.search(text, end)
    return begin, after.start() if after else len(text)


def _says_plan_made(answer: str) -> bool:
    """Whether the answer says ARROW made a plan, other than where its clause makes it no claim
    (as the comment above _SENTENCE_END lists)."""
    for match in _MADE_PLAN.finditer(answer):
        begin, end = _bounds(_CLAUSE_END, answer, match.start(), match.end())
        before, rest = answer[begin : match.start()], answer[match.end() : end]
        clause = answer[begin:end]
        first, last = _bounds(_SENTENCE_END, answer, match.start(), match.end())
        if (
            _NEGATION.search(before)
            or _DENIED.search(rest)
            or _OFFER.search(before)
            or _REQUEST.search(before)
            or _WHAT_IF_REFUSED.search(clause)
            or (
                match.group().lower().startswith("new")
                and _INDEFINITE.search(before)
                and not _EUROS.search(clause)
                and _POINTER.search(answer[first:last])
            )
        ):
            continue
        return True
    return False


def _what_if_outcomes(items: list[Mapping[str, Any]]) -> tuple[bool, bool]:
    """Whether ARROW answered one of the run's what_if calls with a plan (the one it built, or
    the one it had already built from the same levers), and whether ARROW refused one ("Error
    executing tool what_if: …"). A call that failed otherwise (a lost connection) built nothing
    and is neither."""
    calls = [step for step in run_steps(items) if step["kind"] == "tool"]
    what_ifs = [step for step in calls if step["tool"] == "what_if"]
    built = any(step["ok"] for step in what_ifs)
    refused = any(
        not step["ok"] and str(step["result"]).startswith("Error executing tool")
        for step in what_ifs
    )
    return built, refused


def _plain(answer: str) -> str:
    """The answer as the guard reads it: the part the chat keeps, with plain quotes and
    hyphens, and each run of spaces as one (a line break when it holds one)."""
    text = answer[:MAX_ANSWER].translate(_PLAIN)
    return _SPACE.sub(lambda run: "\n" if "\n" in run.group() else " ", text)


def _unsupported(answer: str, shown: int, made: bool) -> str | None:
    """Why an answer is not shown: it names a plan the case does not have, or says ARROW made a
    plan where ``made`` is false (no what_if of its run returned a plan or was refused by ARROW,
    and the case has no what-if plan). None when it may be shown."""
    text = _plain(answer)
    if any(not 1 <= number <= shown for number in _plan_numbers(text)):
        return "named a plan this case does not have"
    if not made and _says_plan_made(text):
        return "reported a what-if plan ARROW did not build"
    return None


def _retryable(error: Exception) -> bool:
    """Only a 429 or 5xx is asked again: after a timeout the run may still be working, and a
    second run could duplicate its work."""
    if not isinstance(error, CloudDependencyError) or error.may_continue:
        return False
    status = error.status_code
    return status is not None and (status == 429 or status >= 500)


def _why(error: Exception) -> str:
    """Why a turn has no answer, in plain words."""
    if isinstance(error, CloudDependencyError):
        if error.status_code == 424:
            # Foundry's Failed Dependency: a tool or agent the run called failed.
            return "a live check it asked for failed"
        if error.status_code is not None:
            return f"the Case Assistant's service answered HTTP {error.status_code}"
        if error.may_continue:
            return "the Case Assistant did not answer in time"
        text = str(error)
        if text.startswith("The Case Assistant"):
            return text[:1].lower() + text[1:]
        return "the Case Assistant could not be reached"
    if isinstance(error, ValueError) and str(error).startswith("The Case Assistant"):
        return str(error)[:1].lower() + str(error)[1:]
    return "ARROW could not ask the Case Assistant"


class Chat:
    def __init__(
        self,
        store: CaseStore,
        gateway: Gateway,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.now = now
        self.sleep = sleep

    def ask(self, trace: str, question: str, asker: Caller) -> dict[str, Any]:
        """Record a question on the case, pending until ``answer`` runs; one at a time."""
        text = question.strip()
        if not text or len(text) > MAX_QUESTION:
            raise ValueError(f"A question has 1 to {MAX_QUESTION} characters")
        now = self.now()
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "asked_at": now.isoformat(),
            "asked_by": {"oid": asker.oid, "name": asker.name or "OCC operator"},
            "question": text,
            "status": "pending",
            "answer": None,
            "refused": False,
            "answered_at": None,
            "ended_at": None,
            "calls": [],
            "what_if_plan_ids": [],
            "response_id": None,
            "error": None,
        }
        interrupted: list[str] = []

        def update(chat: list[dict[str, Any]]) -> list[dict[str, Any]]:
            interrupted.clear()
            for turn in chat:
                if (
                    turn.get("status") == "pending"
                    and now - datetime.fromisoformat(turn["asked_at"]) > STALE
                ):
                    turn.update(status="failed", error=INTERRUPTED)
                    interrupted.append(turn["id"])
            if any(turn.get("status") == "pending" for turn in chat):
                raise ChatBusy("An answer is pending on this case; ask again once it arrives")
            if len(chat) >= MAX_TURNS:
                raise ChatBusy(f"This case already keeps {MAX_TURNS} questions, the most it takes")
            return [*chat, entry]

        self.store.update_chat(trace, update)
        for entry_id in interrupted:
            self._log(trace, "chat_failed", entry_id, "The Case Assistant's answer was interrupted")
        self.store.append_event(
            trace,
            {
                "actor": asker.oid,
                "autonomy": "human",
                "event": "chat_asked",
                "entry_id": entry["id"],
                "message": "Asked the Case Assistant about the case",
            },
        )
        return entry

    def answer(self, trace: str, entry_id: str) -> None:
        """Ask the Case Assistant a pending question and record its answer, or why there is
        none, on the turn, whatever the case's status by then."""
        document = self.store.get(trace)
        chat: list[dict[str, Any]] = document.get("chat") or []
        index = next((n for n, turn in enumerate(chat) if turn["id"] == entry_id), None)
        if index is None or chat[index]["status"] != "pending":
            return
        entry = chat[index]
        earlier = [
            {"question": turn["question"], "answer": turn["answer"]}
            for turn in chat[:index]
            if turn.get("status") == "answered"
        ][-EARLIER_TURNS:]
        brief = {
            "case_id": document["display_id"],
            # The case as read_case returns it, so every answer starts from the record; a live
            # run showed the agent skipping read_case once earlier turns were given.
            "case": case_digest(document),
            "question": entry["question"],
            "asked_by": entry["asked_by"]["name"],
            "earlier": earlier,
        }
        try:
            result = self._ask(trace, brief)
            # Code, not the prompt, keeps an answer to the plans the case has and the what-ifs
            # ARROW built: live runs described a what-if "Plan 3", then "the new plan is number
            # 3", with figures the Case Assistant never asked ARROW for. Saying a what-if plan
            # was made stands when this run's what_if returned one or ARROW refused it (the
            # answer says why), or the case has a what-if plan (a follow-up about it).
            built, refused = _what_if_outcomes(result["trail"].get("items") or [])
            plans = visible_plans(self.store.get(trace))
            made = built or refused or any(plan.get("what_if") for plan in plans)
            if problem := _unsupported(str(result["answer"]), len(plans), made):
                raise ValueError(f"The Case Assistant {problem}")
        except Exception as error:
            if isinstance(error, CloudDependencyError | ValueError):
                logger.warning("No answer for chat turn %s of %s: %s", entry_id, trace, error)
            else:
                logger.exception("No answer for chat turn %s of %s", entry_id, trace)
            values: dict[str, Any] = {
                "status": "failed",
                "error": f"No answer: {_why(error)}; ask again",
            }
            event, message = "chat_failed", "The Case Assistant gave no answer"
        else:
            trail = result["trail"]
            values = {
                "status": "answered",
                "answer": str(result["answer"])[:MAX_ANSWER],
                "refused": result["refused"] is True,
                "answered_at": self.now().isoformat(),
                "calls": _calls(trail.get("items") or []),
                "response_id": trail.get("response_id"),
            }
            event, message = "chat_answered", "The Case Assistant answered"
        # The plans its run added, even when the run then failed.
        values["what_if_plan_ids"] = [
            plan["plan_id"]
            for plan in self.store.get(trace).get("options") or []
            if (plan.get("what_if") or {}).get("entry_id") == entry_id
        ]
        # The end of the turn's run, whatever its outcome: the decision trail lists the system
        # calls the run caused apart from the assessment's (trail.build_trail).
        values["ended_at"] = self.now().isoformat()
        recorded: list[bool] = []

        def update(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for turn in turns:
                # A turn already failed as interrupted keeps its outcome.
                if turn["id"] == entry_id and turn.get("status") == "pending":
                    turn.update(values)
                    recorded.append(True)
            return turns

        self.store.update_chat(trace, update)
        if recorded:
            self._log(trace, event, entry_id, message)

    def _ask(self, trace: str, brief: dict[str, Any]) -> dict[str, Any]:
        """One call on a 120 s deadline; a 429 or 5xx is asked once more after 15 s."""
        try:
            return self.gateway.ask_case_assistant(trace, brief, timeout=ANSWER_SECONDS)
        except CloudDependencyError as error:
            if not _retryable(error):
                raise
            logger.warning("Case Assistant unavailable for %s, asking again: %s", trace, error)
        self.sleep(RETRY_SECONDS)
        return self.gateway.ask_case_assistant(trace, brief, timeout=ANSWER_SECONDS)

    def _log(self, trace: str, event: str, entry_id: str, message: str) -> None:
        self.store.append_event(
            trace, {**ASSISTANT, "event": event, "entry_id": entry_id, "message": message}
        )
