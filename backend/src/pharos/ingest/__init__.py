"""Fetching content from outside sources and turning it into clean, bounded records (ADR-0006,
ADR-0009, ADR-0012).

Everything in a feed is untrusted: it is reduced to plain text with fixed length limits, links are
checked, and nothing here ever treats feed text as an instruction, a template or code.
"""
