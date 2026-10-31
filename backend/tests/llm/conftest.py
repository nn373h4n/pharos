import pytest

from pharos.llm import schemas
from pharos.llm.types import Task

from .support import Answer


@pytest.fixture(autouse=True)
def _the_test_schema_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The schemas are a closed table of the product; for these tests every task answers with
    `Answer`, so the tests need not know the fields of the real ones."""
    for task in Task:
        monkeypatch.setitem(schemas.SCHEMAS, task, Answer)
