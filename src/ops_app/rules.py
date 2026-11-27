"""Rule versions for the airline's editable autonomy limits, under four-eyes approval.

One OCC duty manager proposes new limits and a different one approves or rejects them; only the
proposer may withdraw. At most one proposal waits at a time. The approved change with the highest
version is in force and applies to plans built afterwards (control tower and rules spec §3).
Both stores run the same checks, so their behaviour is identical.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from ops_app.policy import RELEASE_LIMITS, Limits, classify, limits_of, originals_of

logger = logging.getLogger("ops_app")
Status = Literal["proposed", "approved", "rejected", "withdrawn"]
Action = Literal["approve", "reject", "withdraw"]
RELEASE_ACTOR = "arrow-release"
RELEASE_REASON = "ARROW release values"
MAX_LIMIT = 2_000_000_000  # fits SQL int; there is no business upper bound
COLUMNS = (
    "change_id, cost_cap_eur, rebook_limit, base_version, reason, proposed_by, proposed_at,"
    " status, version, decided_by, decided_at"
)
INSERTED = ", ".join(f"INSERTED.{column.strip()}" for column in COLUMNS.split(","))


class RuleConflict(RuntimeError):
    """Another change is waiting, this one is no longer waiting, or the rules changed since."""


class RuleRefused(PermissionError):
    """Four-eyes: this duty manager may not take this decision."""


@dataclass(frozen=True)
class Proposal:
    change_id: int
    cost_cap_eur: int
    rebook_limit: int
    base_version: int
    reason: str
    proposed_by: str
    proposed_at: str
    status: Status
    version: int | None = None
    decided_by: str | None = None
    decided_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuleStore(Protocol):
    def in_force(self) -> Limits: ...

    def pending(self) -> Proposal | None: ...

    def history(self) -> list[Proposal]: ...

    def propose(
        self, cost_cap_eur: int, rebook_limit: int, reason: str, actor: str
    ) -> Proposal: ...

    def decide(self, change_id: int, action: Action, actor: str) -> Proposal: ...


def check_proposal(
    current: Limits, pending: Proposal | None, cost_cap_eur: int, rebook_limit: int, reason: str
) -> None:
    if pending is not None:
        raise RuleConflict("A change is already waiting for approval")
    for label, value in (("Cost limit", cost_cap_eur), ("Rebooking limit", rebook_limit)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_LIMIT:
            raise ValueError(f"{label} must be a whole number from 0 to {MAX_LIMIT:,}")
    if not 3 <= len(reason.strip()) <= 300:
        raise ValueError("A reason of 3 to 300 characters is required")
    if (cost_cap_eur, rebook_limit) == (current.cost_cap_eur, current.rebook_limit):
        raise ValueError("The proposal changes nothing")


def check_decision(proposal: Proposal | None, action: str, actor: str, current: Limits) -> Status:
    """The proposal's new status if this duty manager may take this action now."""
    if action not in ("approve", "reject", "withdraw"):
        raise ValueError(f"Unknown action: {action}")
    if proposal is None or proposal.status != "proposed":
        raise RuleConflict("This change is no longer waiting for approval")
    if action == "withdraw":
        if actor != proposal.proposed_by:
            raise RuleRefused("Only the proposer can withdraw this change")
        return "withdrawn"
    if actor == proposal.proposed_by:
        raise RuleRefused("You proposed this change; another duty manager approves it")
    if action == "approve" and current.version != proposal.base_version:
        raise RuleConflict("Rules changed since this proposal; propose again")
    return "approved" if action == "approve" else "rejected"


def in_force_of(changes: Iterable[Proposal]) -> Proposal:
    """The approved change with the highest version."""
    approved = [change for change in changes if change.status == "approved"]
    if not approved:
        raise LookupError("No rule version is in force")
    return max(approved, key=lambda change: change.version or 0)


def limits_from(change: Proposal) -> Limits:
    if change.version is None:
        raise LookupError(f"Change {change.change_id} has no version")
    return Limits(change.version, change.cost_cap_eur, change.rebook_limit)


def preview_effect(documents: list[dict[str, Any]], limits: Limits) -> list[dict[str, str]]:
    """The listed plans whose route would change under ``limits`` (spec §3.4).

    Each plan is classified under its own recorded limits and under ``limits``; computed on read
    and never stored. Plans already built keep their own version whatever is approved.
    """
    from ops_app.trail import case_card  # trail imports engine, which imports this module

    effect = []
    for document in documents:
        originals = originals_of(document)
        for plan in document.get("options") or []:
            if not isinstance(plan, Mapping):
                continue
            validation = plan.get("validation")
            now = classify(
                plan, originals=originals, validation=validation, limits=limits_of(plan)
            ).route
            proposed = classify(
                plan, originals=originals, validation=validation, limits=limits
            ).route
            if now != proposed:
                effect.append(
                    {
                        "display_id": str(document.get("display_id") or ""),
                        "headline": str(case_card(document)["headline"]),
                        "plan": str(plan.get("plan_id") or ""),
                        "now": now,
                        "proposed": proposed,
                    }
                )
    return effect


def log_change(change: Proposal, action: str, actor: str) -> None:
    logger.info(
        "rules_change change_id=%s action=%s actor=%s version=%s cost_cap_eur=%s rebook_limit=%s",
        change.change_id,
        action,
        actor,
        change.version,
        change.cost_cap_eur,
        change.rebook_limit,
    )


def _utc(at: datetime) -> str:
    """ISO UTC to the millisecond, as SQL datetime2(3) keeps it."""
    return at.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


class MemoryRules:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        now = _utc(datetime.now(UTC))
        self._changes: list[Proposal] = [
            Proposal(
                change_id=1,
                cost_cap_eur=RELEASE_LIMITS.cost_cap_eur,
                rebook_limit=RELEASE_LIMITS.rebook_limit,
                base_version=RELEASE_LIMITS.version,
                reason=RELEASE_REASON,
                proposed_by=RELEASE_ACTOR,
                proposed_at=now,
                status="approved",
                version=RELEASE_LIMITS.version,
                decided_by=RELEASE_ACTOR,
                decided_at=now,
            )
        ]

    def in_force(self) -> Limits:
        with self._lock:
            return limits_from(in_force_of(self._changes))

    def pending(self) -> Proposal | None:
        with self._lock:
            return next((c for c in self._changes if c.status == "proposed"), None)

    def history(self) -> list[Proposal]:
        with self._lock:
            return list(reversed(self._changes))

    def propose(self, cost_cap_eur: int, rebook_limit: int, reason: str, actor: str) -> Proposal:
        with self._lock:
            current = self.in_force()
            check_proposal(current, self.pending(), cost_cap_eur, rebook_limit, reason)
            proposal = Proposal(
                change_id=self._changes[-1].change_id + 1,
                cost_cap_eur=cost_cap_eur,
                rebook_limit=rebook_limit,
                base_version=current.version,
                reason=reason.strip(),
                proposed_by=actor,
                proposed_at=_utc(datetime.now(UTC)),
                status="proposed",
            )
            self._changes.append(proposal)
        log_change(proposal, "propose", actor)
        return proposal

    def decide(self, change_id: int, action: Action, actor: str) -> Proposal:
        with self._lock:
            current = self.in_force()
            proposal = next((c for c in self._changes if c.change_id == change_id), None)
            status = check_decision(proposal, action, actor, current)
            assert proposal is not None  # check_decision refuses a missing proposal
            decided = replace(
                proposal,
                status=status,
                version=current.version + 1 if status == "approved" else None,
                decided_by=actor,
                decided_at=_utc(datetime.now(UTC)),
            )
            self._changes[self._changes.index(proposal)] = decided
        log_change(decided, action, actor)
        return decided


Connect = Callable[[], AbstractContextManager[Any]]  # yields a pyodbc connection; commits on exit


def _proposal(row: Any) -> Proposal:
    return Proposal(
        change_id=int(row[0]),
        cost_cap_eur=int(row[1]),
        rebook_limit=int(row[2]),
        base_version=int(row[3]),
        reason=str(row[4]),
        proposed_by=str(row[5]),
        proposed_at=_utc(row[6]),
        status=row[7],
        version=int(row[8]) if row[8] is not None else None,
        decided_by=str(row[9]) if row[9] is not None else None,
        decided_at=_utc(row[10]) if row[10] is not None else None,
    )


class SqlRules:
    """Rule versions in ``ops.policy_change``; each call is one transaction under row locks.

    Every change first locks the row in force, so proposals and decisions run one at a time; the
    filtered unique indexes stay as the database's own guard.
    """

    def __init__(self, connect: Connect) -> None:
        self.connect = connect

    @staticmethod
    def _in_force(conn: Any) -> Limits:
        row = conn.execute(
            "SELECT TOP 1 version, cost_cap_eur, rebook_limit FROM ops.policy_change"
            " WITH (UPDLOCK, ROWLOCK) WHERE status = 'approved' ORDER BY version DESC"
        ).fetchone()
        if row is None:
            raise LookupError("No rule version is in force")
        return Limits(int(row[0]), int(row[1]), int(row[2]))

    @staticmethod
    def _pending(conn: Any) -> Proposal | None:
        row = conn.execute(
            f"SELECT {COLUMNS} FROM ops.policy_change WITH (UPDLOCK, ROWLOCK)"
            " WHERE status = 'proposed'"
        ).fetchone()
        return _proposal(row) if row else None

    def in_force(self) -> Limits:
        with self.connect() as conn:
            return self._in_force(conn)

    def pending(self) -> Proposal | None:
        with self.connect() as conn:
            return self._pending(conn)

    def history(self) -> list[Proposal]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM ops.policy_change ORDER BY change_id DESC"
            ).fetchall()
        return [_proposal(row) for row in rows]

    def propose(self, cost_cap_eur: int, rebook_limit: int, reason: str, actor: str) -> Proposal:
        with self.connect() as conn:
            current = self._in_force(conn)
            check_proposal(current, self._pending(conn), cost_cap_eur, rebook_limit, reason)
            row = conn.execute(
                "INSERT INTO ops.policy_change"
                " (cost_cap_eur, rebook_limit, base_version, reason, proposed_by, proposed_at,"
                f" status) OUTPUT {INSERTED}"
                " VALUES (?, ?, ?, ?, ?, SYSUTCDATETIME(), 'proposed')",
                cost_cap_eur,
                rebook_limit,
                current.version,
                reason.strip(),
                actor,
            ).fetchone()
        proposal = _proposal(row)
        log_change(proposal, "propose", actor)
        return proposal

    def decide(self, change_id: int, action: Action, actor: str) -> Proposal:
        with self.connect() as conn:
            current = self._in_force(conn)
            row = conn.execute(
                f"SELECT {COLUMNS} FROM ops.policy_change WITH (UPDLOCK, ROWLOCK)"
                " WHERE change_id = ?",
                change_id,
            ).fetchone()
            status = check_decision(_proposal(row) if row else None, action, actor, current)
            row = conn.execute(
                "UPDATE ops.policy_change"
                " SET status = ?, version = ?, decided_by = ?, decided_at = SYSUTCDATETIME()"
                f" OUTPUT {INSERTED} WHERE change_id = ?",
                status,
                current.version + 1 if status == "approved" else None,
                actor,
                change_id,
            ).fetchone()
        decided = _proposal(row)
        log_change(decided, action, actor)
        return decided
