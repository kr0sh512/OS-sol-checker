"""Pure parsing of a complete validated Git tree and safe CommonMark fragments."""
from __future__ import annotations

import re
import unicodedata
from hashlib import sha256
from html import escape
from urllib.parse import urlsplit

import nh3
from markdown_it import MarkdownIt

from ...domain.models import Hint, Task, TestCase, Topic
from .tree import MAX_EXPECTED_OUTPUT_BYTES, MAX_TEST_STDIN_BYTES, TreeEntry, validate_tree

REPO_ID = "https://github.com/kr0sh512/OS-tasks"
# This version is persisted in the snapshot envelope AND its identity. Changing
# Python's Unicode tables cannot silently change an existing snapshot's order.
SORT_UNICODE_VERSION = unicodedata.unidata_version
PARSER_VERSION = "catalog-v1"
_TEST_NAME = re.compile(r"([0-9]+)\.(in|out)\Z")
_SAFE_TAGS = {"p", "br", "hr", "pre", "code", "em", "strong", "s", "a", "blockquote",
              "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6"}


def natural_key(name: str, relative_path: str | None = None) -> tuple:
    normalized = " ".join(name.split()).casefold()
    parts: list[tuple] = []
    current: list[str] = []
    numeric: bool | None = None
    for char in normalized:
        is_number = unicodedata.category(char) == "Nd"
        if numeric is not None and is_number != numeric:
            parts.append(_key_part(current, numeric))
            current = []
        numeric = is_number
        current.append(char)
    if current:
        parts.append(_key_part(current, bool(numeric)))
    return tuple(parts), (relative_path if relative_path is not None else name).encode("utf-8")


def _key_part(chars: list[str], numeric: bool) -> tuple:
    if not numeric:
        return 1, "".join(chars)
    digits = "".join(str(unicodedata.decimal(char)) for char in chars).lstrip("0") or "0"
    # Decimal strings avoid both machine overflow and Python's int-string limit.
    return 0, len(digits), digits


def entity_id(relative_path: str) -> str:
    return sha256(REPO_ID.encode("utf-8") + b"\x00" + relative_path.encode("utf-8")).hexdigest()


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if tag != "a" or attribute != "href":
        return value
    if "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if value.startswith("//"):
        return None
    if parsed.scheme:
        return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else None
    return value if not parsed.netloc else None


def _inline_text(tokens) -> str:
    text: list[str] = []
    for token in tokens or []:
        if token.type in {"text", "code_inline"}:
            text.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            text.append(" ")
        elif token.children:
            text.append(_inline_text(token.children))
    return "".join(text)


def _without_line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    return line.removesuffix("\n")


def parse_markdown(source: str) -> tuple[str, list[Hint]]:
    parser = MarkdownIt("commonmark", {"html": False})
    # Never produce a browser resource request, even before sanitization.
    parser.renderer.rules["image"] = lambda tokens, index, options, env: escape(
        _inline_text(tokens[index].children))
    env: dict = {}
    tokens = parser.parse(source, env)
    # markdown-it normalizes LF, CRLF and lone CR. Preserve source endings:
    # only LF/CRLF may be removed when recognizing an exact separator.
    lines = re.findall(r"[^\r\n]*(?:\r\n|\n|\r|$)", source)
    segments: list[list] = [[]]
    for token in tokens:
        exact_break = (token.type == "hr" and token.level == 0 and token.map is not None
                       and token.map[1] == token.map[0] + 1
                       and _without_line_ending(lines[token.map[0]]) == "---")
        if exact_break:
            segments.append([])
        else:
            segments[-1].append(token)

    def render(segment: list) -> str:
        raw = parser.renderer.render(segment, parser.options, env)
        return nh3.clean(raw, tags=_SAFE_TAGS, attributes={"a": {"href", "title"}},
                         attribute_filter=_attribute_filter, url_schemes={"http", "https"},
                         link_rel="noopener noreferrer", strip_comments=True)

    hints: list[Hint] = []
    for index, segment in enumerate(segments[1:], 1):
        title = ""
        for position, token in enumerate(segment):
            if token.type == "heading_open" and token.level == 0:
                title = _inline_text(segment[position + 1].children).strip()
                segment = segment[:position] + segment[position + 3:]
                break
        hints.append(Hint(title=title or f"Подсказка {index}", html=render(segment)))
    return render(segments[0]), hints


def parse_catalog(entries: list[TreeEntry]) -> list[Topic]:
    tree = validate_tree(entries)
    children: dict[str, list[TreeEntry]] = {}
    substantive: set[str] = set()
    for path, entry in tree.items():
        parent, _, name = path.rpartition("/")
        children.setdefault(parent, []).append(entry)
        if not entry.is_directory and name != ".gitkeep":
            parts = path.split("/")
            for end in range(1, len(parts)):
                substantive.add("/".join(parts[:end]))
        # A malformed task.md type is still a visible candidate.
        if name == "task.md" and path.count("/") == 2:
            substantive.add(parent)
    topics: list[Topic] = []
    for topic_entry in sorted(children.get("", []), key=lambda entry: natural_key(entry.path)):
        if not topic_entry.is_directory:
            continue
        topic_path = topic_entry.path
        topic = Topic(id=entity_id(topic_path), title=topic_path)
        for task_entry in sorted(children.get(topic_path, []),
                                 key=lambda entry: natural_key(entry.path.rsplit("/", 1)[-1], entry.path)):
            if not task_entry.is_directory or task_entry.path not in substantive:
                continue
            path = task_entry.path
            task = Task(id=entity_id(path), topic_id=topic.id,
                        title=path.rsplit("/", 1)[-1], relative_path=path)
            reasons: list[str] = []
            description = tree.get(f"{path}/task.md")
            if description is None or description.is_directory:
                reasons.append("Отсутствует обычный файл task.md")
            else:
                try:
                    task.description_html, task.hints = parse_markdown(description.data.decode("utf-8"))
                except UnicodeDecodeError:
                    reasons.append("task.md не является UTF-8")
            test_root = tree.get(f"{path}/tests")
            pairs: dict[str, dict[str, TreeEntry]] = {}
            if test_root is None or not test_root.is_directory:
                reasons.append("Отсутствует каталог tests")
            else:
                for entry in children.get(test_root.path, []):
                    name = entry.path.rsplit("/", 1)[-1]
                    if name == ".gitkeep" and not entry.is_directory:
                        continue
                    match = _TEST_NAME.fullmatch(name)
                    if entry.is_directory or match is None:
                        reasons.append("В tests есть вложенный каталог или посторонний файл")
                        continue
                    number, kind = match.groups()
                    pairs.setdefault(number, {})[kind] = entry
                if not pairs:
                    reasons.append("Нет полных пар тестов")
                for number in sorted(pairs, key=natural_key):
                    pair = pairs[number]
                    if pair.keys() != {"in", "out"}:
                        reasons.append("Есть неполная пара тестов")
                        continue
                    if len(pair["in"].data) > MAX_TEST_STDIN_BYTES:
                        reasons.append("Вход теста превышает 1048576 байт")
                    if len(pair["out"].data) > MAX_EXPECTED_OUTPUT_BYTES:
                        reasons.append("Ожидаемый вывод превышает 65536 байт")
                    task.tests.append(TestCase(id=number, input_path=f"tree/{pair['in'].path}",
                                               expected_path=f"tree/{pair['out'].path}"))
                if not task.tests and "Нет полных пар тестов" not in reasons:
                    reasons.append("Нет полных пар тестов")
            if reasons:
                task.availability = False
                task.reason = "; ".join(dict.fromkeys(reasons))
            topic.tasks.append(task)
        if topic.tasks:
            topics.append(topic)
    return topics
