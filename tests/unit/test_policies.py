import unicodedata

from os_sol_checker.domain.policies import preview


def test_preview_bounds_raw_bytes_and_neutralizes_terminal_and_bidi_controls():
    value = "\x1b[31m\u202eПример\n".encode()
    result = preview(value, limit=len(value) - 2)
    assert result.truncated
    assert result.byte_size == len(value)
    assert not any(unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\t"
                   for char in result.text)
    assert "\\u001b" in result.text and "\\u202e" in result.text
