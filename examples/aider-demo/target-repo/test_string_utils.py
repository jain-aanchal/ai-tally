# SPDX-License-Identifier: Apache-2.0
"""Tests Aider has to make pass during the make aider-demo run."""

import pytest

from string_utils import is_palindrome, reverse_words, word_count


def test_reverse_words_basic():
    assert reverse_words("hello world") == "world hello"


def test_reverse_words_collapses_whitespace():
    assert reverse_words("  one  two   three  ") == "three two one"


def test_is_palindrome_alphanumeric():
    assert is_palindrome("A man, a plan, a canal: Panama") is True
    assert is_palindrome("hello") is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", {}),
        ("a a b", {"a": 2, "b": 1}),
        ("Hello, hello, World!", {"hello": 2, "world": 1}),
    ],
)
def test_word_count(text, expected):
    assert word_count(text) == expected
