"""The base of every object a model replies with."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator


class Reply(BaseModel):
    """An object in a model's reply."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _known_fields(cls, data: Any) -> Any:
        """Raises ValueError naming the fields there are."""
        if isinstance(data, dict):
            unknown = [repr(key) for key in data if key not in cls.model_fields]
            if unknown:
                raise ValueError(
                    f"there is no field {', '.join(unknown)}; "
                    f"the fields are {', '.join(cls.model_fields)}"
                )
        return data
