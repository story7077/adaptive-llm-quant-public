from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc

HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class DeterministicReplayArtifactV1:
    challenger_id: str
    candidate_artifact_hash: str
    config_hash: str
    code_hash: str
    data_manifest_hash: str
    first_replay_hash: str
    second_replay_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", require_aware_utc(self.created_at))
        if not self.challenger_id:
            raise ValueError("challenger_id is required")
        for field_name in (
            "candidate_artifact_hash",
            "config_hash",
            "code_hash",
            "data_manifest_hash",
            "first_replay_hash",
            "second_replay_hash",
        ):
            if HASH_PATTERN.fullmatch(getattr(self, field_name)) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 hash")

    @property
    def deterministic_match(self) -> bool:
        return self.first_replay_hash == self.second_replay_hash

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": "deterministic_replay_artifact_v1",
            "challenger_id": self.challenger_id,
            "candidate_artifact_hash": self.candidate_artifact_hash,
            "config_hash": self.config_hash,
            "code_hash": self.code_hash,
            "data_manifest_hash": self.data_manifest_hash,
            "first_replay_hash": self.first_replay_hash,
            "second_replay_hash": self.second_replay_hash,
            "deterministic_match": self.deterministic_match,
            "created_at": self.created_at,
        }
        return {
            **payload,
            "artifact_hash": canonical_hash(payload),
        }

    @property
    def artifact_hash(self) -> str:
        return str(self.payload()["artifact_hash"])
