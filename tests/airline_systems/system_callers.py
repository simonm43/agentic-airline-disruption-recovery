"""Entra app roles as Easy Auth passes them to the airline systems (tools/systems/app_roles.py)."""

READS = ("Schedule.Read", "Maintenance.Read", "Crew.Read", "Booking.Read", "Slots.Read")
# ARROW's identity (id-airrec-ops-dev): four systems' reads and the only write role. It records
# defects in Maintenance but never reads it, so it has no Maintenance.Read.
ARROW = ("Schedule.Read", "Crew.Read", "Booking.Read", "Slots.Read", "Actions.Write")


def role_claims(roles: tuple[str, ...]) -> list[dict[str, str]]:
    return [{"typ": "roles", "val": role} for role in roles]
