"""A lone surrogate in a link, a title or an identifier must not fail a whole fetch (M3-09 review).

JSON can carry `"\\ud800"`. Such a string cannot be written as UTF-8, so anything that tried to hash
or store it would raise, the run would end as `error:unexpected` and the same feed would fail again
on every fetch. The link check refuses it; the text cleaning already drops it.
"""

import json
from datetime import UTC, datetime

import pytest

from pharos.ingest.connectors import FetchState
from pharos.ingest.connectors.github import GithubConnector
from pharos.ingest.connectors.hackernews import HackerNewsConnector
from pharos.ingest.links import safe_link
from pharos.ingest.text import clean_identifier, clean_text
from pharos.net.addresses import AddressPolicy
from pharos.net.safe_http import SafeHttpClient
from tests.net.fakes import PUBLIC, FakeResolver, Script, response

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.anyio
LONE = "\ud800"
HOSTS: dict = {"api.github.com": [PUBLIC], "hn.algolia.com": [PUBLIC]}


def client(body: object) -> SafeHttpClient:
    answer = response(200, json.dumps(body).encode())  # json.dumps writes the surrogate as \ud800
    return SafeHttpClient(
        AddressPolicy(),
        resolver=FakeResolver(dict(HOSTS)),
        backend=Script(answer),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "url",
    [
        f"https://a.example/{LONE}x",
        f"https://a.example/?q={LONE}",
        f"https://a{LONE}.example/",
        f"https://a.example/#{LONE}",
        "https://a.example/\udfff",
    ],
)
def test_a_link_with_a_lone_surrogate_is_refused(url: str) -> None:
    assert safe_link(url) is None


def test_the_text_cleaning_drops_lone_surrogates() -> None:
    assert clean_text(f"a{LONE}b", 50) == "ab"
    assert clean_identifier(f"id{LONE}1", 50) == "id1"


async def test_a_release_with_a_surrogate_in_its_link_is_skipped_and_the_rest_are_read() -> None:
    releases = [
        {"id": 1, "tag_name": "v1", "html_url": f"https://github.com/a/b/releases/{LONE}"},
        {
            "id": 2,
            "tag_name": f"v2{LONE}",
            "html_url": "https://github.com/a/b/releases/v2",
            "body": f"n{LONE}",
        },
    ]
    outcome = await GithubConnector().fetch({"repo": "a/b"}, FetchState(), client(releases), NOW)
    assert [e.external_id for e in outcome.entries] == ["2"]
    assert outcome.entries[0].title == "a/b: v2" and outcome.entries[0].excerpt == "n"
    assert outcome.entries_skipped == 1


async def test_a_story_with_a_surrogate_in_its_link_points_to_the_discussion_instead() -> None:
    hits = {"hits": [{"objectID": "7", "title": f"T{LONE}", "url": f"https://x.example/{LONE}"}]}
    connector = HackerNewsConnector()
    outcome = await connector.fetch(
        {"query": "sap", "min_points": 1}, FetchState(), client(hits), NOW
    )
    [only] = outcome.entries
    assert only.url == "https://news.ycombinator.com/item?id=7" and only.title == "T"
    only.content_sha256.encode()  # everything stored can be written down
    only.url.encode()
