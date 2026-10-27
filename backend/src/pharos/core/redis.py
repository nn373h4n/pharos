"""Redis connection helpers."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def url_for_database(url: str, database: int) -> str:
    """The same server and credentials, another numbered database.

    redis-py lets a `?db=` query parameter override the database in the path, so it is dropped;
    otherwise a URL ending in `?db=0` would put sessions into the database Celery uses. Socket URLs
    carry no database number and are refused.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("redis", "rediss"):
        raise ValueError("only redis:// and rediss:// URLs are supported")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "db"
    ]
    return urlunsplit(parts._replace(path=f"/{database}", query=urlencode(query)))
