from pathlib import Path

import pytest

from os_sol_checker.adapters.catalog.parser import (entity_id, natural_key, parse_catalog, parse_markdown)
from os_sol_checker.adapters.catalog.tree import (MAX_EXPECTED_OUTPUT_BYTES, MAX_TEST_STDIN_BYTES,
                                                  TreeEntry)


def task_tree(extra: list[TreeEntry] | None = None) -> list[TreeEntry]:
    return [TreeEntry("Тема", "040000"), TreeEntry("Тема/1", "040000"),
            TreeEntry("Тема/1/task.md", "100644", b"# Task\n"),
            TreeEntry("Тема/1/tests", "040000"),
            TreeEntry("Тема/1/tests/1.in", "100644"),
            TreeEntry("Тема/1/tests/1.out", "100644"), *(extra or [])]


def test_upstream_example_fixture_has_two_empty_pairs_and_no_placeholder_tasks():
    root = Path(__file__).parents[1] / "fixtures" / "catalog" / "upstream"
    entries = [TreeEntry(path.relative_to(root).as_posix(),
                         "040000" if path.is_dir() else "100644",
                         b"" if path.is_dir() else path.read_bytes()) for path in root.rglob("*")]
    topics = parse_catalog(entries)
    assert [topic.title for topic in topics] == ["1. Пример первой темы"]
    task, = topics[0].tasks
    assert task.availability
    assert [test.id for test in task.tests] == ["1", "2"]
    assert [hint.title for hint in task.hints] == ["Название подсказки", "Название второй подсказки"]
    assert task.title == "1"


def test_natural_order_unicode_digits_whitespace_and_byte_ties():
    names = ["Тема 10", "Тема\u202f2", "Тема\u00a02", "ТЕМА 2", "Тема 02", "Тема ٢"]
    expected = ["ТЕМА 2", "Тема 02", "Тема ٢", "Тема\u00a02", "Тема\u202f2", "Тема 10"]
    assert sorted(names, key=natural_key) == expected
    assert sorted(["10", "2", "01", "1"], key=natural_key) == ["01", "1", "2", "10"]
    assert natural_key("9" * 5000) > natural_key("1" * 4999)
    assert sorted(["a", "2"], key=natural_key) == ["2", "a"]


def test_identity_preserves_exact_unicode_and_ignores_description():
    before = parse_catalog(task_tree())[0].tasks[0]
    entries = task_tree()
    entries[2] = TreeEntry(entries[2].path, "100644", b"Changed")
    after = parse_catalog(entries)[0].tasks[0]
    assert before.id == after.id == entity_id("Тема/1")
    assert entity_id("Тема/1") != entity_id("Тема/01")
    assert entity_id("e\u0301/1") != entity_id("é/1")


def test_numeric_pairing_is_exact_and_gaps_are_legal():
    entries = task_tree([TreeEntry("Тема/1/tests/01.in", "100644"),
                         TreeEntry("Тема/1/tests/01.out", "100644"),
                         TreeEntry("Тема/1/tests/10.in", "100644", b"\xff"),
                         TreeEntry("Тема/1/tests/10.out", "100644")])
    task = parse_catalog(entries)[0].tasks[0]
    assert task.availability
    assert [case.id for case in task.tests] == ["01", "1", "10"]
    entries.pop(5)
    assert not parse_catalog(entries)[0].tasks[0].availability


@pytest.mark.parametrize("entry", [TreeEntry("Тема/1/tests/junk", "100644"),
                                   TreeEntry("Тема/1/tests/2.IN", "100644"),
                                   TreeEntry("Тема/1/tests/subdirectory", "040000"),
                                   TreeEntry("Тема/1/tests/2.in", "100644")])
def test_bad_test_entries_disable_the_whole_task(entry):
    task = parse_catalog(task_tree([entry]))[0].tasks[0]
    assert task.availability is False
    assert task.reason


def test_gitkeep_only_is_hidden_but_invalid_real_tasks_remain_visible():
    entries = [TreeEntry("Topic", "040000"), TreeEntry("Topic/empty", "040000"),
               TreeEntry("Topic/empty/nested", "040000"),
               TreeEntry("Topic/empty/nested/.gitkeep", "100644"),
               TreeEntry("Topic/broken", "040000"), TreeEntry("Topic/broken/data", "100644")]
    task, = parse_catalog(entries)[0].tasks
    assert task.title == "broken"
    assert not task.availability and "task.md" in task.reason and "tests" in task.reason


@pytest.mark.parametrize("replacement", [TreeEntry("Тема/1/task.md", "100644", b"\xff"),
                                        TreeEntry("Тема/1/task.md", "040000")])
def test_invalid_markdown_is_local_to_task(replacement):
    entries = task_tree()
    entries[2] = replacement
    task = parse_catalog(entries)[0].tasks[0]
    assert not task.availability
    assert "task.md" in task.reason


@pytest.mark.parametrize(("kind", "limit"), [("in", MAX_TEST_STDIN_BYTES),
                                              ("out", MAX_EXPECTED_OUTPUT_BYTES)])
def test_transport_limits_accept_boundary_and_disable_overflow(kind, limit):
    entries = task_tree()
    position = 4 if kind == "in" else 5
    entries[position] = TreeEntry(entries[position].path, "100644", b"x" * limit)
    assert parse_catalog(entries)[0].tasks[0].availability
    entries[position] = TreeEntry(entries[position].path, "100644", b"x" * (limit + 1))
    assert not parse_catalog(entries)[0].tasks[0].availability


@pytest.mark.parametrize("source", ["```\n---\n```\n", "    ---\n", "Setext\n---\n",
                                    "> ---\n", "- item\n\n  ---\n", "- - -\n", "***\n",
                                    " ---\n", "--- \n", "inline ---\n", "---\r"])
def test_only_exact_root_thematic_break_splits_hints(source):
    _, hints = parse_markdown(source)
    assert hints == []


def test_top_level_breaks_preserve_empty_segments_and_exact_crlf():
    description, hints = parse_markdown("---\r\n\r\n---\r\n")
    assert description == ""
    assert [hint.title for hint in hints] == ["Подсказка 1", "Подсказка 2"]
    assert [hint.html for hint in hints] == ["", ""]


def test_hint_heading_plain_text_and_removal_preserves_preceding_content():
    description, hints = parse_markdown("Description\n\n---\n\nBefore\n\n## A *bold* [link](https://example.org) `code`\n\nAfter\n")
    hint, = hints
    assert "Description" in description
    assert hint.title == "A bold link code"
    assert "Before" in hint.html and "After" in hint.html and "<h2>" not in hint.html
    _, hints = parse_markdown("---\n\n> # Nested\n\n#\n\nBody\n")
    assert hints[0].title == "Подсказка 1"
    assert "Nested" in hints[0].html and "Body" in hints[0].html


def test_html_and_resources_cannot_control_hint_ui_or_execute():
    html, hints = parse_markdown("<script>alert(1)</script>\n\n<details open><summary>x</summary></details>\n\n"
                                "![ALT](https://tracker.invalid/img)\n\n"
                                "[bad](//tracker.invalid) [good](https://example.org) [relative](/task/1)\n\n"
                                "---\n\n# Hint <img onerror=alert(1)>\n")
    assert "<script" not in html and "<details" not in html and "<summary" not in html
    assert "<img" not in html and 'href="//tracker.invalid' not in html
    assert 'href="https://example.org"' in html and 'href="/task/1"' in html
    assert "ALT" in html
    assert "<img" in hints[0].title  # plain text, escaped by the template, never trusted HTML
