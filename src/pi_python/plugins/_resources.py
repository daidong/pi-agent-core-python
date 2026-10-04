"""Skills, prompt templates and agent definitions, read the way Pi's coding agent reads them.

Discovery, validation, the system-prompt listing of skills, ``/skill:name`` expansion and
prompt-template argument substitution follow pinned Pi (``skills.ts``,
``prompt-templates.ts``, ``agent-session.ts`` and the subagent example's ``agents.ts``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import ModelInfo
from ..provider import Provider
from ._frontmatter import parse_frontmatter

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
_SKIPPED_DIRS = {"node_modules", "__pycache__"}


@dataclass(frozen=True)
class Skill:
    """A skill: a SKILL.md file whose description is listed in the system prompt."""

    name: str
    description: str
    path: Path
    plugin: str = ""
    disable_model_invocation: bool = False

    @property
    def base_dir(self) -> Path:
        return self.path.parent


@dataclass(frozen=True)
class PromptTemplate:
    """A Markdown prompt that ``/name args`` expands into a user message."""

    name: str
    description: str
    content: str
    path: Path | None = None
    plugin: str = ""
    argument_hint: str | None = None


@dataclass
class AgentDefinition:
    """A subagent the main agent can delegate to through the ``subagent`` tool.

    `tools` names the tools the subagent may use; None gives it every tool of the main
    agent except ``subagent`` itself. `model` defaults to the main agent's model and
    `provider` to its provider; `options` (for example a reasoning level) start empty.
    """

    name: str
    description: str
    system_prompt: str = ""
    tools: list[str] | None = None
    model: str | ModelInfo | None = None
    options: dict[str, Any] = field(default_factory=dict)
    provider: Provider | None = None
    path: Path | None = None
    plugin: str = ""


def _validate_name(name: str) -> list[str]:
    errors = []
    if len(name) > MAX_NAME_LENGTH:
        errors.append(f"name exceeds {MAX_NAME_LENGTH} characters ({len(name)})")
    if not re.fullmatch(r"[a-z0-9-]+", name):
        errors.append("name contains invalid characters (must be lowercase a-z, 0-9, hyphens only)")
    if name.startswith("-") or name.endswith("-"):
        errors.append("name must not start or end with a hyphen")
    if "--" in name:
        errors.append("name must not contain consecutive hyphens")
    return errors


def _read(path: Path, diagnostics: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        diagnostics.append(f"{path}: {exc}")
        return None


def load_skill_file(path: Path, plugin: str, diagnostics: list[str]) -> Skill | None:
    declared = path.name == "SKILL.md"
    text = _read(path, diagnostics)
    if text is None:
        return None
    try:
        frontmatter, _ = parse_frontmatter(text)
    except ValueError as exc:
        if declared:
            diagnostics.append(f"{path}: {exc}")
        return None
    raw_description = frontmatter.get("description")
    description = raw_description if isinstance(raw_description, str) else ""
    has_description = description.strip() != ""
    if not declared and not has_description:
        return None
    if not has_description:
        diagnostics.append(f"{path}: description is required")
    elif len(description) > MAX_DESCRIPTION_LENGTH:
        diagnostics.append(
            f"{path}: description exceeds {MAX_DESCRIPTION_LENGTH} characters ({len(description)})"
        )
    named = frontmatter.get("name")
    name = named if isinstance(named, str) and named else path.parent.name
    diagnostics.extend(f"{path}: {error}" for error in _validate_name(name))
    if not has_description:
        return None
    return Skill(
        name,
        description,
        path.resolve(),
        plugin,
        frontmatter.get("disable-model-invocation") is True,
    )


def load_skills(path: Path, plugin: str, diagnostics: list[str]) -> list[Skill]:
    """Skills under `path`: a SKILL.md or .md file, or a directory searched as Pi does."""
    if path.is_file():
        if path.suffix != ".md":
            diagnostics.append(f"{path}: skill path is not a markdown file")
            return []
        skill = load_skill_file(path, plugin, diagnostics)
        return [skill] if skill else []
    if not path.is_dir():
        diagnostics.append(f"{path}: skill path does not exist")
        return []
    return _skills_in(path, plugin, diagnostics, include_root_files=True, seen=set())


def _skills_in(
    directory: Path,
    plugin: str,
    diagnostics: list[str],
    *,
    include_root_files: bool,
    seen: set[Path],
) -> list[Skill]:
    # A directory with SKILL.md is one skill; otherwise direct .md files at the top level
    # and SKILL.md files in subdirectories, recursively. Order is by name, for stable output.
    real = directory.resolve()
    if real in seen:  # a symlink back to a directory already searched
        return []
    seen.add(real)
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        diagnostics.append(f"{directory}: {exc}")
        return []
    marker = directory / "SKILL.md"
    if marker.is_file():
        skill = load_skill_file(marker, plugin, diagnostics)
        return [skill] if skill else []
    skills: list[Skill] = []
    for entry in entries:
        if entry.name.startswith(".") or entry.name in _SKIPPED_DIRS:
            continue
        if entry.is_dir():
            skills.extend(
                _skills_in(entry, plugin, diagnostics, include_root_files=False, seen=seen)
            )
        elif include_root_files and entry.is_file() and entry.suffix == ".md":
            skill = load_skill_file(entry, plugin, diagnostics)
            if skill:
                skills.append(skill)
    return skills


def load_prompt_templates(path: Path, plugin: str, diagnostics: list[str]) -> list[PromptTemplate]:
    """Templates from one .md file, or the .md files directly in a directory."""
    if path.is_dir():
        files = sorted((p for p in path.iterdir() if p.suffix == ".md" and p.is_file()), key=str)
    elif path.is_file() and path.suffix == ".md":
        files = [path]
    else:
        diagnostics.append(f"{path}: prompt template path does not exist")
        return []
    templates = []
    for file in files:
        text = _read(file, diagnostics)
        if text is None:
            continue
        try:
            frontmatter, body = parse_frontmatter(text)
        except ValueError as exc:
            diagnostics.append(f"{file}: {exc}")
            continue
        description = frontmatter.get("description")
        if not isinstance(description, str) or not description:
            first = next((line for line in body.split("\n") if line.strip()), "")
            description = first[:60] + ("..." if len(first) > 60 else "")
        hint = frontmatter.get("argument-hint")
        templates.append(
            PromptTemplate(
                file.stem,
                description,
                body,
                file.resolve(),
                plugin,
                hint if isinstance(hint, str) else None,
            )
        )
    return templates


def _tool_list(value: Any) -> list[str] | None:
    """Pi accepts ``tools: read, bash`` and ``tools: [read, bash]``."""
    raw = value if isinstance(value, list) else value.split(",") if isinstance(value, str) else []
    tools = [t.strip() for t in raw if isinstance(t, str) and t.strip()]
    return tools or None


def load_agent_definitions(
    path: Path, plugin: str, diagnostics: list[str]
) -> list[AgentDefinition]:
    """Agent definitions from one .md file, or the .md files directly in a directory."""
    if path.is_dir():
        files = sorted((p for p in path.iterdir() if p.suffix == ".md" and p.is_file()), key=str)
    elif path.is_file() and path.suffix == ".md":
        files = [path]
    else:
        diagnostics.append(f"{path}: agent path does not exist")
        return []
    agents = []
    for file in files:
        text = _read(file, diagnostics)
        if text is None:
            continue
        try:
            frontmatter, body = parse_frontmatter(text)
        except ValueError as exc:
            diagnostics.append(f"{file}: {exc}")
            continue
        name, description = frontmatter.get("name"), frontmatter.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            diagnostics.append(f"{file}: an agent needs a name and a description")
            continue
        model = frontmatter.get("model")
        agents.append(
            AgentDefinition(
                name,
                description,
                body,
                _tool_list(frontmatter.get("tools")),
                model if isinstance(model, str) else None,
                path=file.resolve(),
                plugin=plugin,
            )
        )
    return agents


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_skills_for_prompt(skills: list[Skill], read_tool: str = "read_skill") -> str:
    """Pi's ``<available_skills>`` listing; skills that disable model invocation are left out."""
    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""
    lines = [
        "The following skills provide specialized instructions for specific tasks.",
        f"Use the {read_tool} tool to load a skill's file when the task matches its description.",
        f"When a skill file references a relative path, read it with {read_tool} and that"
        " relative path; for other tools, resolve it against the skill directory (the parent"
        " of SKILL.md).",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines += [
            "  <skill>",
            f"    <name>{_escape_xml(skill.name)}</name>",
            f"    <description>{_escape_xml(skill.description)}</description>",
            f"    <location>{_escape_xml(str(skill.path))}</location>",
            "  </skill>",
        ]
    lines.append("</available_skills>")
    return "\n".join(lines)


def skill_block(skill: Skill) -> str:
    """Pi's expansion of a skill's instructions, as used by ``/skill:name``."""
    _, body = parse_frontmatter(skill.path.read_text(encoding="utf-8"))
    return (
        f'<skill name="{skill.name}" location="{skill.path}">\n'
        f"References are relative to {skill.base_dir}.\n\n{body.strip()}\n</skill>"
    )


def expand_skill_command(text: str, skills: list[Skill]) -> str:
    """``/skill:name args`` becomes the skill block followed by the arguments."""
    if not text.startswith("/skill:"):
        return text
    space = text.find(" ")
    name = text[7:] if space == -1 else text[7:space]
    args = "" if space == -1 else text[space + 1 :].strip()
    skill = next((s for s in skills if s.name == name), None)
    if skill is None:
        return text
    block = skill_block(skill)
    return f"{block}\n\n{args}" if args else block


def parse_command_args(text: str) -> list[str]:
    """Split arguments like a shell: whitespace separates, quotes group."""
    args: list[str] = []
    current, quote = "", ""
    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                current += char
        elif char in "\"'":
            quote = char
        elif char.isspace():
            if current:
                args.append(current)
                current = ""
        else:
            current += char
    if current:
        args.append(current)
    return args


_PLACEHOLDER = re.compile(
    r"\$\{(\d+|ARGUMENTS|@):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(ARGUMENTS|@|\d+)"
)


def substitute_args(content: str, args: list[str]) -> str:
    """Pi's placeholders: ``$1``, ``$@``, ``$ARGUMENTS``, ``${N:-default}``, ``${@:N}``,
    ``${@:N:L}``. Values are not substituted again."""
    joined = " ".join(args)

    def replace(match: re.Match[str]) -> str:
        target, default, start, length, simple = match.groups()
        if target:
            value = joined if target in {"@", "ARGUMENTS"} else _arg(args, int(target) - 1)
            return value or default
        if start:
            first = max(int(start) - 1, 0)
            if length:
                return " ".join(args[first : first + int(length)])
            return " ".join(args[first:])
        if simple in {"ARGUMENTS", "@"}:
            return joined
        return _arg(args, int(simple) - 1)

    return _PLACEHOLDER.sub(replace, content)


def _arg(args: list[str], index: int) -> str:
    return args[index] if 0 <= index < len(args) else ""


def expand_prompt_template(text: str, templates: list[PromptTemplate]) -> str:
    """``/name args`` becomes the template with its arguments; other text is unchanged."""
    match = re.fullmatch(r"/(\S+)(?:\s+([\s\S]*))?", text)
    if not match:
        return text
    template = next((t for t in templates if t.name == match.group(1)), None)
    if template is None:
        return text
    return substitute_args(template.content, parse_command_args(match.group(2) or ""))
