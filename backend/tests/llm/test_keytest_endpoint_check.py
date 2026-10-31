"""The protocol of "test the key", the look at an endpoint, and the gateway's check of the key."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from pharos.llm import keytest
from pharos.llm.budget import BudgetUnavailable
from pharos.llm.endpoint import EndpointCheck
from pharos.llm.errors import ProviderRejected
from pharos.llm.litellm_provider import LiteLLMProvider
from pharos.llm.provider import FakeProvider, ProviderAnswer
from pharos.llm.types import Stage

from .support import ORG, Harness, MemoryBudget

TEST = uuid.UUID("22222222-2222-4222-8222-222222222222")


def good(**changes: Any) -> str:
    body: dict[str, Any] = {
        "test_id": str(TEST),
        "outcome": "ok",
        "checked_at": datetime(2026, 10, 31, 12, 0, tzinfo=UTC).isoformat(),
    }
    body.update(changes)
    return json.dumps(body)


class TestTheStrictReader:
    def test_the_workers_answer_is_read(self) -> None:
        result = keytest.parse_result(good(), test_id=TEST)
        assert result is not None and result.outcome == "ok"
        assert keytest.parse_result(good(), test_id=None) is not None  # the "last" record

    @pytest.mark.parametrize(
        "outcome",
        ["ok", "disabled", "not_configured", "rejected", "failed", "budget", "paced", "paused"],
    )
    def test_every_outcome_of_the_protocol_is_accepted(self, outcome: str) -> None:
        assert keytest.parse_result(good(outcome=outcome), test_id=TEST) is not None

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "not json",
            "[]",
            good(outcome="the key is sk-ant-secret"),  # not one of the words
            good(extra="field"),
            good(test_id="not-a-uuid"),
            good(test_id=str(uuid.uuid4())),  # another test's answer
            good(checked_at="yesterday"),
            good(checked_at=12345),
            "x" * 2000,
            f'{{"test_id": "{TEST}", "outcome": "ok"}}',  # a field is missing
        ],
    )
    def test_anything_else_is_not_an_answer(self, raw: str | None) -> None:
        assert keytest.parse_result(raw, test_id=TEST) is None

    def test_the_keys_of_the_two_sides_do_not_overlap(self) -> None:
        org = uuid.uuid4()
        assert keytest.marker_key(TEST).startswith("pharos:api:")
        assert keytest.cooldown_key(org).startswith("pharos:api:")
        assert keytest.result_key(TEST).startswith("pharos:worker:")
        assert keytest.last_key(org).startswith("pharos:worker:")


class Resolver:
    def __init__(self, *addresses: str, error: Exception | None = None) -> None:
        self.addresses = addresses
        self.error = error
        self.calls = 0

    async def __call__(self, host: str, port: int, **_: Any) -> list[tuple[Any, ...]]:
        self.calls += 1
        if self.error:
            raise self.error
        return [(2, 1, 6, "", (address, port)) for address in self.addresses]


async def checked(
    monkeypatch: pytest.MonkeyPatch,
    resolver: Resolver,
    url: str,
    check: EndpointCheck | None = None,
) -> None:
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    await (check or EndpointCheck()).ensure_public(url)


@pytest.mark.anyio
class TestTheEndpoint:
    async def test_a_name_that_resolves_to_public_addresses_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await checked(
            monkeypatch,
            Resolver("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
            "https://api.example.com/v1",
        )

    @pytest.mark.parametrize(
        "addresses",
        [
            ("127.0.0.1",),
            ("10.0.0.5",),
            ("169.254.169.254",),
            ("192.168.1.1",),
            ("::1",),
            ("fd00::1",),
            ("93.184.216.34", "10.0.0.5"),
            ("::ffff:127.0.0.1",),
            (),
        ],
    )
    async def test_a_name_that_leads_inside_the_network_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
    ) -> None:
        with pytest.raises(ProviderRejected):
            await checked(monkeypatch, Resolver(*addresses), "https://api.example.com/v1")

    @pytest.mark.parametrize("error", [OSError("no such name"), TimeoutError()])
    async def test_a_name_that_cannot_be_resolved_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        with pytest.raises(ProviderRejected):
            await checked(monkeypatch, Resolver(error=error), "https://api.example.com/v1")

    async def test_a_url_without_a_host_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ProviderRejected):
            await checked(monkeypatch, Resolver("93.184.216.34"), "https:///v1")

    async def test_a_good_look_is_remembered_for_a_minute_and_a_bad_one_is_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        check = EndpointCheck()
        good_names = Resolver("93.184.216.34")
        await checked(monkeypatch, good_names, "https://api.example.com/v1", check)
        await checked(monkeypatch, good_names, "https://api.example.com/v1", check)
        assert good_names.calls == 1
        bad_names = Resolver("10.0.0.5")
        for _ in range(2):
            with pytest.raises(ProviderRejected):
                await checked(monkeypatch, bad_names, "https://other.example.com/v1", check)
        assert bad_names.calls == 2

    async def test_the_provider_looks_before_it_sends_and_does_not_send_if_the_look_fails(
        self,
    ) -> None:
        sent: list[int] = []

        class Refusing:
            async def ensure_public(self, url: str) -> None:
                raise ProviderRejected()

        provider = LiteLLMProvider(
            "azure", SecretStr("k"), base_url="https://x.example.com", endpoint=Refusing()
        )  # type: ignore[arg-type]
        provider._litellm = type(
            "L", (), {"acompletion": staticmethod(lambda **_: sent.append(1))}
        )()
        with pytest.raises(ProviderRejected):
            await provider.complete(
                model="m", system="s", user="u", max_tokens=5, deadline_seconds=1
            )
        assert sent == []


@pytest.mark.anyio
class TestCheckingTheKey:
    async def test_a_working_key_is_ok_and_the_call_is_booked_and_recorded_without_an_article(
        self,
    ) -> None:
        harness = Harness(FakeProvider("ok"))
        assert await harness.gateway.check_key(ORG) == "ok"
        assert harness.provider is not None
        [call] = harness.provider.calls
        assert "ARTICLE" not in str(call["user"]) and call["max_tokens"] == 20
        [record] = harness.usage.records
        assert (record.stage, record.outcome) == ("key_test", "ok")
        assert harness.budget.settled and harness.budget.total == harness.budget.settled[0]

    async def test_a_budget_store_that_does_not_answer_is_a_failure_not_a_crash(self) -> None:
        class Down(MemoryBudget):
            async def pace(self, org, per_minute):  # type: ignore[no-untyped-def]
                raise BudgetUnavailable()

        harness = Harness(FakeProvider("ok"), budget=Down())
        assert await harness.gateway.check_key(ORG) == "failed"
        assert harness.provider is not None and harness.provider.calls == []

    async def test_it_says_when_nothing_is_configured(self) -> None:
        assert await Harness(None).gateway.check_key(ORG) == "disabled"
        harness = Harness(FakeProvider("ok"), models={Stage.INSIGHT: "m"})
        assert await harness.gateway.check_key(ORG) == "not_configured"
        assert harness.provider is not None and harness.provider.calls == []

    async def test_a_refused_key_is_rejected_and_costs_nothing(self) -> None:
        async def refuse(model: str, system: str, user: str) -> ProviderAnswer:
            raise ProviderRejected()

        harness = Harness(FakeProvider(script=refuse))
        assert await harness.gateway.check_key(ORG) == "rejected"
        assert harness.budget.total == 0 and [r.outcome for r in harness.usage.records] == [
            "failed"
        ]

    async def test_a_timeout_keeps_its_booking_and_a_clean_error_gives_it_back(self) -> None:
        slow = Harness(FakeProvider(timeout=True))
        assert await slow.gateway.check_key(ORG) == "failed"
        assert slow.budget.total == slow.budget.reserved[0]
        down = Harness(FakeProvider(fail=True))
        assert await down.gateway.check_key(ORG) == "failed"
        assert down.budget.total == 0

    async def test_the_cap_the_pace_and_a_resting_stage_are_reported_not_raised(self) -> None:
        assert (
            await Harness(FakeProvider("ok"), budget=MemoryBudget(cap=10)).gateway.check_key(ORG)
            == "budget"
        )
        assert (
            await Harness(
                FakeProvider("ok"), budget=MemoryBudget(calls_per_minute=0)
            ).gateway.check_key(ORG)
            == "paced"
        )
        resting = Harness(FakeProvider(fail=True))
        for _ in range(5):
            await resting.gateway.check_key(ORG)
        assert await resting.gateway.check_key(ORG) == "paused"

    async def test_nothing_is_retried(self) -> None:
        harness = Harness(FakeProvider(fail=True), max_retries=4)
        await harness.gateway.check_key(ORG)
        assert harness.provider is not None and len(harness.provider.calls) == 1
