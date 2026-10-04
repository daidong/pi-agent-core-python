"""Frontmatter for skills, prompt templates and agent definitions.

Pi reads the frontmatter with a full YAML parser. These files use a small part of YAML,
so this module parses that part without a dependency: mappings, block and flow lists,
plain, quoted and block (``|`` / ``>``) scalars, and comments. Anything else raises
ValueError, which the loader reports as a warning for that file.
"""

from __future__ import annotations

import re
from typing import Any

# Frontmatter is a few lines of metadata. These bounds keep a hostile or broken file from
# stalling or crashing the loader; exceeding them is reported like any other bad file.
MAX_FRONTMATTER_CHARS = 64 * 1024
MAX_DEPTH = 64
_INT = re.compile(r"[-+]?[0-9]+")
_FLOAT = re.compile(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
}


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Pi's split: the YAML between a leading ``---`` and the next ``\\n---``, and the body."""
    normalized = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---"):
        return None, normalized
    end = normalized.find("\n---", 3)
    if end == -1:
        return None, normalized
    return normalized[4:end], normalized[end + 4 :].strip()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return the frontmatter mapping (empty when absent) and the body."""
    source, body = split_frontmatter(text)
    if not source:
        return {}, body
    value = parse_yaml(source)
    if value is None:
        return {}, body
    if not isinstance(value, dict):
        raise ValueError("frontmatter must be a mapping")
    return value, body


def parse_yaml(source: str) -> Any:
    if len(source) > MAX_FRONTMATTER_CHARS:
        raise ValueError(f"frontmatter is longer than {MAX_FRONTMATTER_CHARS} characters")
    lines = source.split("\n")
    if lines[-1] == "":
        lines.pop()  # the text after a final newline is not a line
    parser = _Parser(lines)
    parser.skip_blank()
    if parser.done():
        return None
    value = parser.block(parser.indent())
    parser.skip_blank()
    if not parser.done():
        raise ValueError(f"unexpected content on line {parser.index + 1}")
    return value


def _strip_comment(text: str) -> str:
    """Drop a `` #`` comment from a plain scalar."""
    match = re.search(r"(^|\s)#", text)
    return (text[: match.start()] if match else text).rstrip()


def _scalar(text: str) -> Any:
    if text in {"", "~", "null", "Null", "NULL"}:
        return None
    if text in {"true", "True", "TRUE"}:
        return True
    if text in {"false", "False", "FALSE"}:
        return False
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text) and any(c.isdigit() for c in text):
        return float(text)
    return text


def _double_quoted(text: str) -> tuple[str, str]:
    """Parse a double-quoted string at the start of `text`; return it and the rest."""
    out: list[str] = []
    i = 1
    while i < len(text):
        char = text[i]
        if char == '"':
            return "".join(out), text[i + 1 :]
        if char == "\\":
            i += 1
            if i >= len(text):
                break
            code = text[i]
            width = {"x": 2, "u": 4, "U": 8}.get(code)
            if width:
                digits = text[i + 1 : i + 1 + width]
                if len(digits) != width:
                    raise ValueError("bad escape in double-quoted string")
                out.append(chr(int(digits, 16)))
                i += width
            elif code in _ESCAPES:
                out.append(_ESCAPES[code])
            else:
                raise ValueError(f"unknown escape \\{code}")
        else:
            out.append(char)
        i += 1
    raise ValueError("unterminated double-quoted string")


def _single_quoted(text: str) -> tuple[str, str]:
    out: list[str] = []
    i = 1
    while i < len(text):
        if text[i] == "'":
            if text[i + 1 : i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), text[i + 1 :]
        out.append(text[i])
        i += 1
    raise ValueError("unterminated single-quoted string")


def _quoted(text: str) -> tuple[str, str]:
    return _double_quoted(text) if text[0] == '"' else _single_quoted(text)


def _flow_list(text: str) -> list[Any]:
    """A one-line ``[a, "b", c]`` list of scalars."""
    inner = text[1:]
    items: list[Any] = []
    while True:
        inner = inner.lstrip()
        if inner.startswith("]"):
            if _strip_comment(inner[1:]):
                raise ValueError("unexpected text after flow list")
            return items
        if not inner:
            raise ValueError("unterminated flow list")
        if inner[0] in "\"'":
            value, inner = _quoted(inner)
            items.append(value)
        elif inner[0] in "[{":
            raise ValueError("nested flow collections are not supported")
        else:
            match = re.match(r"[^,\]]*", inner)
            assert match is not None
            items.append(_scalar(match.group(0).strip()))
            inner = inner[match.end() :]
        inner = inner.lstrip()
        if inner.startswith(","):
            inner = inner[1:]
        elif not inner.startswith("]"):
            raise ValueError("expected , or ] in flow list")


class _Parser:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.index = 0
        self.depth = 0

    def done(self) -> bool:
        return self.index >= len(self.lines)

    @staticmethod
    def _blank(line: str) -> bool:
        stripped = line.strip()
        return not stripped or stripped.startswith("#")

    def skip_blank(self) -> None:
        while not self.done() and self._blank(self.lines[self.index]):
            self.index += 1

    def indent(self) -> int:
        line = self.lines[self.index]
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ValueError(f"tab indentation on line {self.index + 1}")
        return len(line) - len(line.lstrip(" "))

    def block(self, indent: int) -> Any:
        # An explicit limit: Python's recursion limit would raise RecursionError, and PyPy
        # can overflow its native stack before reaching it.
        if self.depth >= MAX_DEPTH:
            raise ValueError(f"frontmatter is nested more than {MAX_DEPTH} levels deep")
        self.depth += 1
        try:
            text = self.lines[self.index].strip()
            if text == "-" or text.startswith("- "):
                return self.sequence(indent)
            return self.mapping(indent)
        finally:
            self.depth -= 1

    def sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            self.skip_blank()
            if self.done() or self.indent() != indent:
                return items
            line = self.lines[self.index]
            text = line.strip()
            if not (text == "-" or text.startswith("- ")):
                return items
            rest = text[1:].lstrip()
            if not rest:
                self.index += 1
                items.append(self.nested(indent, allow_same=False))
                continue
            # "- key: value" starts a mapping indented to the item's content.
            child = indent + (len(text) - len(rest))
            self.lines[self.index] = " " * child + rest
            if rest[0] in "|>":
                self.index += 1
                items.append(self.block_scalar(rest, indent))
            elif _key(rest) is not None:
                items.append(self.mapping(child))
            else:
                items.append(self.inline(rest, indent))

    def mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            self.skip_blank()
            if self.done():
                return result
            current = self.indent()
            if current < indent:
                return result
            if current > indent:
                raise ValueError(f"unexpected indentation on line {self.index + 1}")
            text = self.lines[self.index].strip()
            if text.startswith("- "):
                return result
            parsed = _key(text)
            if parsed is None:
                raise ValueError(f"expected 'key: value' on line {self.index + 1}")
            key, rest = parsed
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            self.index += 1
            if not rest:
                result[key] = self.nested(indent, allow_same=True)
            elif rest[0] in "|>":
                result[key] = self.block_scalar(rest, indent)
            else:
                self.index -= 1
                result[key] = self.inline(rest, indent)

    def nested(self, indent: int, *, allow_same: bool) -> Any:
        """The value under an empty ``key:`` or ``-``: a deeper block, or null."""
        self.skip_blank()
        if self.done():
            return None
        child = self.indent()
        text = self.lines[self.index].strip()
        is_item = text == "-" or text.startswith("- ")
        if child > indent and not is_item and _key(text) is None:
            return self.inline(text, indent)  # a scalar or flow list on its own line
        if child > indent or (allow_same and child == indent and is_item):
            return self.block(child)
        return None

    def inline(self, text: str, indent: int) -> Any:
        """A value written after ``key:`` or ``-`` on the current line."""
        self.index += 1
        if text[0] in "\"'":
            value, rest = _quoted(text)
            if _strip_comment(rest):
                raise ValueError(f"unexpected text after quoted string on line {self.index}")
            return value
        if text[0] == "[":
            # Scan each line once; rescanning the joined text would be quadratic.
            scanner = _FlowScanner()
            parts = [text]
            closed = scanner.feed(text)
            while not closed and not self.done():
                line = self.lines[self.index].strip()
                self.index += 1
                parts.append(line)
                closed = scanner.feed(" " + line)
            return _flow_list(" ".join(parts))
        if text[0] == "{":
            if _strip_comment(text[1:].lstrip()) == "}":
                return {}
            raise ValueError("flow mappings are not supported")
        if text[0] in "&*!%@`":
            raise ValueError(f"unsupported YAML syntax {text[0]!r} on line {self.index}")
        parts = [_strip_comment(text)]
        # A plain scalar continues on more-indented lines; blank lines become newlines.
        while not self.done():
            line = self.lines[self.index]
            if not line.strip():
                ahead = self.index + 1
                while ahead < len(self.lines) and not self.lines[ahead].strip():
                    ahead += 1
                if ahead >= len(self.lines) or _indent_of(self.lines[ahead]) <= indent:
                    break
                parts.append("\n" * (ahead - self.index))
                self.index = ahead
                continue
            if _indent_of(line) <= indent or line.strip().startswith("#"):
                break
            parts.append(_strip_comment(line.strip()))
            self.index += 1
        if len(parts) == 1:
            return _scalar(parts[0])
        joined = parts[0]
        for previous, part in zip(parts, parts[1:]):
            joined += part if previous.startswith("\n") or part.startswith("\n") else " " + part
        return joined

    def block_scalar(self, header: str, indent: int) -> str:
        match = re.fullmatch(r"([|>])([-+]?)([1-9]?)([-+]?)", _strip_comment(header))
        if not match or (match.group(2) and match.group(4)):
            raise ValueError(f"bad block scalar header {header!r}")
        style, chomp = match.group(1), match.group(2) or match.group(4)
        lines: list[str] = []
        while not self.done():
            line = self.lines[self.index]
            if line.strip() and _indent_of(line) <= indent:
                break
            lines.append(line)
            self.index += 1
        content = [line for line in lines if line.strip()]
        if match.group(3):
            width = indent + int(match.group(3))
        else:
            width = _indent_of(content[0]) if content else indent + 1
        body: list[str] = []
        for line in lines:
            if not line.strip():
                body.append("")
            elif line[:width].strip():
                raise ValueError("block scalar line is less indented than its first line")
            else:
                body.append(line[width:])
        while body and not body[-1]:
            body.pop()
        trailing = len(lines) - len(body)
        if not body:
            return ""
        if style == "|":
            text = "\n".join(body)
        else:
            # Folding: a single break between plain lines becomes a space, each blank
            # line a newline; breaks around more-indented lines are kept.
            text, previous, empties = "", None, 0
            for line in body:
                if not line:
                    empties += 1
                    continue
                if previous is None:
                    text = "\n" * empties + line
                else:
                    special = line.startswith((" ", "\t")) or previous.startswith((" ", "\t"))
                    if empties:
                        text += "\n" * (empties + special) + line
                    else:
                        text += ("\n" if special else " ") + line
                previous, empties = line, 0
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (trailing + 1)
        return text + "\n"


class _FlowScanner:
    """Tracks whether a flow list that may span lines has closed, one segment at a time."""

    def __init__(self) -> None:
        self.depth = 0
        self.quote = ""
        self.escaped = False
        self.previous = ""
        self.comment = False  # a comment inside a flow list is not supported

    def feed(self, segment: str) -> bool:
        if self.comment:
            return False
        for char in segment:
            if self.quote:
                if self.escaped:
                    self.escaped = False
                elif char == "\\" and self.quote == '"':
                    self.escaped = True
                elif char == self.quote:
                    self.quote = ""
            elif char in "\"'":
                self.quote = char
            elif char == "#" and self.previous in ("", " ", "\t"):
                self.comment = True
                return False
            elif char in "[{":
                self.depth += 1
            elif char in "]}":
                self.depth -= 1
                if self.depth == 0:
                    return True
            self.previous = char
        return False


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _key(text: str) -> tuple[str, str] | None:
    """Split ``key: rest``; return None when the line is not a mapping entry."""
    if text[0] in "\"'":
        try:
            key, rest = _quoted(text)
        except ValueError:
            return None
        if not rest.startswith(":") or (len(rest) > 1 and rest[1] not in " \t"):
            return None
        return key, rest[1:].strip()
    match = re.match(r"([^#\s][^:]*?|[^#\s]*?):(?:[ \t]+|$)", text)
    if not match or match.group(1) == "":
        return None
    return match.group(1).rstrip(), text[match.end() :].strip()
