"""Trusted adapter boundary, not model-callable tools or HTTP input schemas.

The official adapter must prove conditional-write and terminal receipt semantics
before advertising them. Capability absence is unknown, never implicit support.
No transport is selected or installed by this module.
"""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ProjectView:
    destination: dict
    snapshot: dict
    observed_at: str
    # Only ordinary, permitted business fields: excludes status, ACLs and templates.
    writable_fields: frozenset[str] = frozenset()
    # Configured fields omitted from the value read. These still require an
    # authoritative unfinished-required proof immediately before a fill write.
    fillable_required_fields: frozenset[str] = frozenset()
    # IDs map to actual target status and explicit closure classification.
    transitions: dict = field(default_factory=dict)
    allowed_actions: frozenset[str] = frozenset()
    conditional_actions: frozenset[str] = frozenset()
    conditional_token: str | None = None
    # Append-only operations whose authorization is checked by the official
    # mutation endpoint. This is not an effective-permission preflight claim.
    server_enforced_actions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProjectReceipt:
    operation_id: str
    write_digest: str
    outcome: str  # applied, rejected, partial, unknown
    terminal: bool
    # Durable remote request/operation record. Not a model conclusion or log string.
    evidence_ref: str | None = None
    applied_fields: tuple[str, ...] = ()


class ProjectTransport(Protocol):
    def preflight(self, destination: dict) -> ProjectView:
        """Fresh read including effective permissions and current transition metadata."""
        ...

    def write(self, packet: dict) -> None:
        """Issue once. A returned HTTP success is not a confirmed business result."""
        ...

    def reconcile(self, packet: dict) -> ProjectReceipt:
        """Read only. Return terminal only after proving no remaining remote work.

        Applied requires exact request correlation and confirmed application, not
        merely finding similar text or desired field values. Partial identifies
        exactly which requested fields were applied and proves the rest stopped.
        """
        ...
