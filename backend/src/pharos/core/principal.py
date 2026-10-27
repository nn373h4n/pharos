"""Who is making the request. Built from the session and the user row, never from the client."""

import uuid
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    MEMBER = "member"


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: Role
