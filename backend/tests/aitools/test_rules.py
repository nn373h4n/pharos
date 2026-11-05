"""What the application accepts as the name and the address of an approved AI tool (M5-07), without
a database. The same lists run against the real API and the table in
tests/integration/test_ai_tools_api.py; here they run in milliseconds."""

# ruff: noqa: RUF001  (look-alike and invisible characters are the point of these tests)

import pytest

from pharos.aitools.service import check_url, clean_name
from pharos.core.errors import ApiError, ErrorCode


def kind_of(error: ApiError) -> str:
    assert error.code == ErrorCode.VALIDATION_FAILED and error.status == 422
    return str(error.extra["errors"][0]["type"])


@pytest.mark.parametrize(
    "url",
    [
        "https://chat.example.org",
        "https://chat.example.org/",
        "https://chat.example.org/path/to?model=small&x=1",
        "https://ai.corp.internal:8443/portal",
        "https://intranet/",
        "https://xn--exmple-cua.example.org/",
        "https://chat.example.org./",
        "https://chat.example.org/" + "a" * 270,
        "https://123.example.com/",  # a name that merely starts with digits
        "https://localhost.example.org/",  # not "localhost" itself
        "https://my-localhost-box.example.org/",
    ],
)
def test_an_address_a_browser_may_safely_be_sent_to_is_returned_as_written(url: str) -> None:
    assert check_url(url) == url


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("http://chat.example.org/", "https_only"),
        ("HTTPS://chat.example.org/", "https_only"),
        ("javascript:alert(1)", "https_only"),
        ("data:text/html,x", "https_only"),
        ("//chat.example.org/", "https_only"),
        ("https://user:pw@chat.example.org/", "credentials_not_allowed"),
        ("https://chat.example.org/?next=a@b", "credentials_not_allowed"),
        ("https://chat.example.org/#frag", "invalid_url"),
        ("https://chat.example.org/a b", "invalid_url"),
        ("https://chat.example.org/‮", "invalid_url"),  # bidirectional override
        ("https://chat.example.org/​", "invalid_url"),  # zero-width space
        ("https://chat.example.org/﻿", "invalid_url"),  # byte order mark
        ("https://chat.example.org/ ", "invalid_url"),  # no-break space
        ("https://chat.example.org/ ", "invalid_url"),  # line separator
        ("https://chat.example.org/\x00", "invalid_url"),
        ("https://chat.example.org/\u0085", "invalid_url"),
        ("https://exämple.org/", "invalid_url"),
        ("https://chat.example.org:99999/", "invalid_url"),
        ("https://[::1", "invalid_url"),  # a bracket that is not closed
        ("https://", "invalid_url"),
        ("https://-x.example.org/", "invalid_url"),
        ("https://[::1]/", "ip_address_not_allowed"),
        ("https://[v1.x]/", "ip_address_not_allowed"),  # the "IPvFuture" literal
        ("https://[v1.chat.example]/", "ip_address_not_allowed"),
        (
            "https://foo.127/",
            "ip_address_not_allowed",
        ),  # a browser reads a last label that is a number
        ("https://foo.0x7f/", "ip_address_not_allowed"),
        ("https://chat.example.org../", "invalid_url"),  # one trailing dot is legal, two are not
        ("https://localhost/", "local_host_not_allowed"),
        ("https://LOCALHOST./", "local_host_not_allowed"),
        ("https://app.localhost:8443/x", "local_host_not_allowed"),
        ("https://127.0.0.1/", "ip_address_not_allowed"),
        ("https://2130706433/", "ip_address_not_allowed"),
        ("https://0x7f.1/", "ip_address_not_allowed"),
        ("https://0177.0.0.1/", "ip_address_not_allowed"),
        ("https://chat.example.org/" + "a" * 300, "invalid_url"),
    ],
)
def test_an_address_that_hides_where_it_goes_or_is_not_https_is_refused(
    url: str, kind: str
) -> None:
    with pytest.raises(ApiError) as caught:
        check_url(url)
    assert kind_of(caught.value) == kind, url


@pytest.mark.parametrize(
    "name", ["Plain Tool", "Ünïcode Çhat", "Чат", "助手", "A-1_b.c (beta)", "n" * 40]
)
def test_an_ordinary_name_is_returned_without_its_surrounding_spaces(name: str) -> None:
    assert clean_name(f"  {name}  ") == name


@pytest.mark.parametrize(
    "name",
    ["", "   ", "<b>x</b>", "x​", "x‮", "x\x00", "x\ny", "&lt;b&gt;", "n" * 41, "Café"],
)
def test_a_name_that_is_not_plain_text_in_its_normal_form_is_refused(name: str) -> None:
    with pytest.raises(ApiError) as caught:
        clean_name(name)
    assert kind_of(caught.value) == "invalid_text"


class _Orig(Exception):
    def __init__(self, sqlstate: str | None, text: str) -> None:
        super().__init__(text)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("sqlstate", "text", "expected"),
    [
        ("23505", "duplicate key value violates unique constraint", True),
        (None, 'duplicate key value violates unique constraint "ai_tools_name_unique"', True),
        ("23514", 'new row violates check constraint "ai_tools_url_check"', False),
        ("23502", "null value in column", False),
        (None, "something else", False),
    ],
)
def test_only_a_repeated_name_is_taken_for_a_duplicate(
    sqlstate: str | None, text: str, expected: bool
) -> None:
    """A check that fails is a bug to be seen (a 500), not a "duplicate" in the audit log."""
    from sqlalchemy.exc import IntegrityError

    from pharos.aitools.service import _is_unique_violation

    assert _is_unique_violation(IntegrityError("UPDATE", {}, _Orig(sqlstate, text))) is expected
