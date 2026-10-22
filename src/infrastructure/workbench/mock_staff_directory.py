"""Read-only Mock team membership sourced from the controlled identity fixture."""

from __future__ import annotations

import json
from pathlib import Path


class MockStaffDirectory:
    def __init__(self, identities_path: Path | None = None) -> None:
        path = identities_path or (
            Path(__file__).resolve().parents[3] / "data" / "fixtures" / "mock-identities.json"
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self._members = {
            (account["tenant_id"], account["subject_id"]): (
                tuple(account["team_ids"]),
                account["role"],
            )
            for account in data["accounts"]
        }

    def belongs_to_any_team(
        self, tenant_id: str, subject_id: str, team_ids: tuple[str, ...]
    ) -> bool:
        membership = self._members.get((tenant_id, subject_id))
        return bool(
            membership
            and membership[1] in {"support", "human_agent"}
            and set(team_ids).intersection(membership[0])
        )
