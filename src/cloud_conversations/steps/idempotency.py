"""Idempotency key derivation for step functions."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def derive_key(run_id: uuid.UUID, step_id: str, action: str, args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{run_id}:{step_id}:{action}:{canonical}".encode()).hexdigest()
