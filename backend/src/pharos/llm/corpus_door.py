"""The second door that makes `PublicArticleText`: an entry of an evaluation corpus (M4-09).

ADR-0015 point 2 allows one query to decide what is class A. The evaluation has no database, so it
needs a door of its own, and a door that trusted its caller would let any code turn any string into
"public article text". So this one checks everything itself: the same limits and plain-text rule
as feed text, a plain http(s) address that is not inside a network, no credentials. Only
`pharos.evaluation.runner` may import it (import contract `corpus-door-has-one-user`).
"""

from pharos.ingest.links import safe_link
from pharos.ingest.sourceconfig import ConfigError, refuse_internal_host
from pharos.ingest.text import clean_text
from pharos.llm.errors import NotPublicArticle
from pharos.llm.types import PublicArticleText, mint_public_text

MAX_TITLE = 300
MAX_EXCERPT = 500


def check_public_fields(title: object, excerpt: object, url: object) -> str:
    """The address of an entry that is fit to be public text, or NotPublicArticle. Text must already
    be plain: what the feed cleaner would change is refused, not silently changed."""
    if not isinstance(title, str) or not isinstance(excerpt, str):
        raise NotPublicArticle()
    if not title:
        raise NotPublicArticle()
    # The cleaner also cuts to the limit, so text over a limit differs from its cleaned form.
    if clean_text(title, MAX_TITLE) != title or clean_text(excerpt, MAX_EXCERPT) != excerpt:
        raise NotPublicArticle()
    link = safe_link(url)
    if link is None:
        raise NotPublicArticle()
    try:
        refuse_internal_host(link, ())
    except ConfigError:
        raise NotPublicArticle() from None
    return link


def public_text_from_corpus(
    title: str, excerpt: str, canonical_url: str, language: str | None
) -> PublicArticleText:
    link = check_public_fields(title, excerpt, canonical_url)
    return mint_public_text(title, excerpt, link, language)
