# SPDX-License-Identifier: Apache-2.0
"""A tiny string-utils module used by the make aider-demo fixture.

Three functions, deliberately under-implemented so Aider has something concrete
(and bounded) to fix. The accompanying tests in test_string_utils.py describe
the intended behavior — this is what the agent reads to figure out what to do.
"""


def reverse_words(s: str) -> str:
    """Return s with the order of whitespace-separated words reversed.

    Leading/trailing whitespace is collapsed and a single space joins the words.
    """
    # TODO: implement
    return s


def is_palindrome(s: str) -> bool:
    """Return True iff s is a palindrome, ignoring case and non-alphanumerics."""
    # TODO: implement
    return False


def word_count(s: str) -> dict[str, int]:
    """Return a mapping word -> count for whitespace-separated words in s.

    Words are compared case-insensitively and stripped of surrounding
    punctuation. Empty input returns an empty dict.
    """
    # TODO: implement
    return {}
