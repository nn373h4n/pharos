"""The fixed choices of a profile. Codes are stored; the web app translates them."""

from typing import Final

AI_LEVELS: Final = ("beginner", "intermediate", "advanced")

# Generic topics people may want help with. Adding one is a code change plus four translations,
# on purpose: free text would bring untrusted content into a field that steers recommendations.
INTEREST_CODES: Final = (
    "writing",
    "summarizing",
    "translation",
    "data_analysis",
    "reporting",
    "research",
    "automation",
    "coding",
    "meetings",
    "planning",
    "customer_support",
    "learning",
)
MAX_INTERESTS: Final = len(INTEREST_CODES)
