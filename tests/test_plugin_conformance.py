"""Plugin resources must match the retained output of the pinned upstream functions.

`reference/plugin-runner.ts` runs Pi's own substituteArgs, parseCommandArgs,
expandPromptTemplate, parseFrontmatter, loadSkillsFromDir, formatSkillsForPrompt and
loadPromptTemplates on `compat/plugin-cases.json`; `scripts/plugin_conformance.py`
refreshes the retained output.
"""

import json
import os
from pathlib import Path

import pytest

from pi_python.plugins._frontmatter import parse_frontmatter
from pi_python.plugins._resources import (
    PromptTemplate,
    expand_prompt_template,
    format_skills_for_prompt,
    load_prompt_templates,
    load_skills,
    parse_command_args,
    substitute_args,
)

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "compat/plugin-cases.json").read_text(encoding="utf-8"))
UPSTREAM = json.loads((ROOT / "compat/results/plugins.upstream.json").read_text(encoding="utf-8"))
# Validation messages are ported word for word; YAML syntax errors are worded differently.
VALIDATION = ("name ", "description ")


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def python_results():
    diagnostics: list[str] = []
    skills = load_skills(ROOT / CASES["skills_dir"], "fixtures", diagnostics)
    prompts = load_prompt_templates(ROOT / CASES["prompts_dir"], "fixtures", [])
    frontmatter = []
    for text in CASES["frontmatter"]:
        try:
            data, body = parse_frontmatter(text)
            frontmatter.append({"frontmatter": data, "body": body})
        except ValueError:
            frontmatter.append({"error": True})
    return {
        "substitute": [substitute_args(content, args) for content, args in CASES["substitute"]],
        "parse_args": [parse_command_args(text) for text in CASES["parse_args"]],
        "expand": [
            expand_prompt_template(
                case["text"],
                [PromptTemplate(t["name"], "", t["content"]) for t in case["templates"]],
            )
            for case in CASES["expand"]
        ],
        "frontmatter": frontmatter,
        "skills": {
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "path": relative(s.path),
                    "disable_model_invocation": s.disable_model_invocation,
                }
                for s in skills
            ],
            "diagnostics": diagnostics,
        },
        # The retained upstream output was produced with "/" separators.
        "skills_prompt": format_skills_for_prompt(skills)
        .replace(str(ROOT), "<ROOT>")
        .replace(os.sep, "/"),
        "prompts": [
            {
                "name": t.name,
                "description": t.description,
                "argument_hint": t.argument_hint,
                "content": t.content,
                "path": relative(t.path),
            }
            for t in prompts
        ],
    }


RESULTS = python_results()


@pytest.mark.parametrize("key", ["substitute", "parse_args", "expand", "frontmatter", "prompts"])
def test_matches_upstream(key):
    assert RESULTS[key] == UPSTREAM[key]


def test_skill_discovery_matches_upstream():
    order = lambda items: sorted(items, key=lambda s: s["path"])  # noqa: E731
    assert order(RESULTS["skills"]["skills"]) == order(UPSTREAM["skills"]["skills"])
    ours = sorted(RESULTS["skills"]["diagnostics"])
    theirs = sorted(
        f"{ROOT / d['path']}: {d['message']}"
        if d["message"].startswith(VALIDATION)
        else f"{ROOT / d['path']}: "
        for d in UPSTREAM["skills"]["diagnostics"]
    )
    assert len(ours) == len(theirs)
    assert all(mine.startswith(expected) for mine, expected in zip(ours, theirs))


def test_skill_listing_matches_upstream_apart_from_the_read_instruction():
    # Pi tells the model to use its `read` tool; this library gives it `read_skill`.
    ours = RESULTS["skills_prompt"]
    theirs = UPSTREAM["skills_prompt"]
    assert ours[ours.index("<available_skills>") :] == theirs[theirs.index("<available_skills>") :]
    assert ours.startswith("The following skills provide specialized instructions")
