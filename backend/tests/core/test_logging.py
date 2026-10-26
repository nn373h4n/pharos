import io
import json
import logging
import sys
from collections.abc import Iterator

import pytest

from pharos.core.logging import (
    MASK,
    JsonFormatter,
    MaskingFilter,
    configure_logging,
    mask_secrets,
    org_id_var,
    request_id_var,
)

# Obviously fake values that still match the shapes the masker looks for.
SECRET = "Zq81fakeSecretValue"
FAKE_API_KEY = "sk-" + "FAKEKEY1234" * 3
FAKE_JWT = ".".join(["eyJ" + "a" * 12, "b" * 12, "c" * 12])
# Built at run time so that no private-key-looking block sits in the repository.
_DASHES = "-" * 5
FAKE_PEM = (
    f"{_DASHES}BEGIN RSA PRIVATE KEY{_DASHES}\n{SECRET}\n{_DASHES}END RSA PRIVATE KEY{_DASHES}"
)
FAKE_GOOGLE_KEY = "AIza" + "Ab1" * 12

MUST_BE_MASKED = [
    f"calling provider with {FAKE_API_KEY}",
    f"Authorization: Bearer {FAKE_API_KEY}",
    f"Authorization: Basic {SECRET}",
    f"Authorization: Token {SECRET}",
    f"Proxy-Authorization: Basic {SECRET}",
    f"password={SECRET}",
    f'{{"api_key": "{SECRET}"}}',
    f'{{"client_secret": "{SECRET}"}}',
    f'{{"access_token": "{SECRET}", "id_token": "{FAKE_JWT}"}}',
    f'{{"db_password": "{SECRET}"}}',
    f"x-api-key: {SECRET}",
    f"token: {SECRET}",
    f'{{"password" : "{SECRET} {SECRET}"}}',
    f"Cookie: sessionid={SECRET}; csrftoken={SECRET}",
    f"Set-Cookie: sid={SECRET}; HttpOnly; Secure",
    f"postgresql://pharos_app:{SECRET}@db:5432/pharos",
    f"redis://:{SECRET}@cache:6379/0",
    f"id_token {FAKE_JWT}",
    FAKE_PEM,
    f"GET /api/x?api_key={SECRET} HTTP/1.1",
    f"GET /callback?code={SECRET}&state={SECRET}x HTTP/1.1",
    f"key {FAKE_GOOGLE_KEY}",
]

MUST_STAY_READABLE = [
    "fetched 25 items from source 'rss-example' in 120 ms",
    "max_tokens=4096",
    "tokens_in=120 tokens_out=300",
    "token_budget_left=50",
    "password_reset_sent=true",
    "secret_rotation_due=2026-12-01",
    "GET /api/feed?limit=20&cursor=abc HTTP/1.1",
]


@pytest.mark.parametrize("text", MUST_BE_MASKED)
def test_mask_secrets_removes_secret_values(text: str) -> None:
    masked = mask_secrets(text)
    for secret in (SECRET, FAKE_API_KEY, FAKE_JWT, FAKE_GOOGLE_KEY, "Zm9vYmFy"):
        assert secret not in masked, masked
    assert MASK in masked


@pytest.mark.parametrize("text", MUST_STAY_READABLE)
def test_mask_secrets_keeps_ordinary_text_and_counters(text: str) -> None:
    assert mask_secrets(text) == text


def test_mask_secrets_keeps_the_key_name_and_quotes() -> None:
    assert mask_secrets(f"password={SECRET}") == f"password={MASK}"
    assert mask_secrets(f'{{"api_key": "{SECRET}"}}') == f'{{"api_key": "{MASK}"}}'


@pytest.fixture
def log_output() -> Iterator[tuple[logging.Logger, io.StringIO]]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(MaskingFilter())
    logger = logging.getLogger("pharos.test.logging")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, stream
    logger.handlers.clear()


def _last_record(stream: io.StringIO) -> dict[str, object]:
    record: dict[str, object] = json.loads(stream.getvalue().strip().splitlines()[-1])
    return record


def test_log_message_and_arguments_are_masked(
    log_output: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = log_output
    logger.info("provider call failed for key %s", FAKE_API_KEY)
    record = _last_record(stream)
    assert FAKE_API_KEY not in stream.getvalue()
    assert record["message"] == f"provider call failed for key {MASK}"
    assert record["level"] == "INFO"


def test_extra_fields_are_masked_by_key_and_by_value(
    log_output: tuple[logging.Logger, io.StringIO],
) -> None:
    logger, stream = log_output
    logger.info(
        "configured",
        extra={
            "api_key": FAKE_API_KEY,
            "client_secret": SECRET,
            "headers": {
                "Authorization": f"Basic {SECRET}",
                "Cookie": f"sid={SECRET}",
                "X-Api-Key": SECRET,
                "accept": "json",
            },
            "url": f"redis://:{SECRET}@cache:6379/0",
            "attempts": 3,
            "tokens_in": 120,
            "max_tokens": 4096,
        },
    )
    record = _last_record(stream)
    assert FAKE_API_KEY not in stream.getvalue()
    assert SECRET not in stream.getvalue()
    assert record["api_key"] == MASK
    assert record["client_secret"] == MASK
    assert record["headers"] == {
        "Authorization": MASK,
        "Cookie": MASK,
        "X-Api-Key": MASK,
        "accept": "json",
    }
    # Counters are not secrets and stay readable.
    assert (record["attempts"], record["tokens_in"], record["max_tokens"]) == (3, 120, 4096)


def test_exception_text_is_masked(log_output: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = log_output
    try:
        raise RuntimeError(f"upstream rejected {FAKE_API_KEY}")
    except RuntimeError:
        logger.exception("request failed")
    record = _last_record(stream)
    assert FAKE_API_KEY not in stream.getvalue()
    assert "RuntimeError" in str(record["exception"])


def test_request_and_org_ids_are_included(log_output: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = log_output
    request_token = request_id_var.set("req-123")
    org_token = org_id_var.set("org-456")
    try:
        logger.info("hello")
    finally:
        request_id_var.reset(request_token)
        org_id_var.reset(org_token)
    record = _last_record(stream)
    assert record["request_id"] == "req-123"
    assert record["org_id"] == "org-456"


def test_ids_are_null_outside_a_request(log_output: tuple[logging.Logger, io.StringIO]) -> None:
    logger, stream = log_output
    logger.info("startup")
    record = _last_record(stream)
    assert record["request_id"] is None
    assert record["org_id"] is None


@pytest.fixture
def restore_logging() -> Iterator[None]:
    names = ["", "uvicorn", "uvicorn.error", "uvicorn.access"]
    saved = [
        (logging.getLogger(n), list(logging.getLogger(n).handlers), logging.getLogger(n).propagate)
        for n in names
    ]
    level = logging.getLogger().level
    yield
    for logger, handlers, propagate in saved:
        logger.handlers[:] = handlers
        logger.propagate = propagate
    logging.getLogger().setLevel(level)


@pytest.mark.usefixtures("restore_logging")
def test_uvicorn_loggers_go_through_the_masking_json_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Uvicorn installs its own plain handlers with propagate=False; reproduce that.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.addHandler(logging.StreamHandler(sys.stdout))
        logger.propagate = False

    configure_logging("INFO")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", f"/cb?code={SECRET}&state=x", "1.1", 200
    )
    logging.getLogger("uvicorn.error").error(
        "Exception in ASGI application: postgresql://u:%s@h/db", SECRET
    )

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2  # one line each, so no logger printed through a second handler
    assert SECRET not in "".join(lines)
    assert [json.loads(line)["logger"] for line in lines] == ["uvicorn.access", "uvicorn.error"]
