"""Request and response shapes of the approved AI tools (M5-07)."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME_LIMIT = 40
URL_LIMIT = 300


class AiToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_LIMIT)
    url: str = Field(min_length=1, max_length=URL_LIMIT)


class AiToolPatch(BaseModel):
    """Fields that are present are changed; an empty request is refused."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_LIMIT)
    url: str | None = Field(default=None, min_length=1, max_length=URL_LIMIT)

    @model_validator(mode="after")
    def _something_to_change(self) -> "AiToolPatch":
        if self.name is None and self.url is None:
            raise ValueError("at least one of name and url")
        return self


class AiToolOut(BaseModel):
    """What everybody who is signed in sees: a name and an address, nothing else."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    url: str


class AiToolListOut(BaseModel):
    items: list[AiToolOut]
