"""Which language a person sees: their own choice, the browser, then the installation default."""

from typing import Final

SUPPORTED_LOCALES: Final = ("en", "az", "tr", "ru")
_MAX_HEADER = 512


def _from_accept_language(header: str | None) -> str | None:
    if not header:
        return None
    candidates: list[tuple[float, int, str]] = []
    for position, part in enumerate(header[:_MAX_HEADER].split(",")):
        tag, _, params = part.strip().partition(";")
        primary = tag.strip().lower().split("-")[0]
        if primary not in SUPPORTED_LOCALES:
            continue
        quality = 1.0
        for param in params.split(";"):
            name, _, value = param.strip().partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        if quality > 0:
            candidates.append((-quality, position, primary))
    return min(candidates)[2] if candidates else None


def resolve_locale(user_locale: str | None, accept_language: str | None, default: str) -> str:
    """The person's saved language wins, then the best supported one in `Accept-Language`, then
    the installation default (`PHAROS_DEFAULT_LOCALE`)."""
    if user_locale in SUPPORTED_LOCALES:
        return str(user_locale)
    return _from_accept_language(accept_language) or default
