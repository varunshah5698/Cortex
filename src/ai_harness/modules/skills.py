"""Skills: reusable workflows loaded on demand, never in the always-on budget.

A skill is a directory with a `SKILL.md` in it. Its frontmatter carries a `name` and a
`description`; the body is the procedure. Only the name and the description are advertised
to the model — the body is read only when the model asks for it. That is the whole point:
a skill costs nothing until it is invoked, which is why it is the right form for
instruction that applies to some runs and not others.

The contract is Harness²'s (`harness2/core/tree.py::_check_skills`), and every rule in it
exists because the failure is silent:

* `name:` must match the directory. A mismatch is dropped at load with no error, so the
  skill simply never exists and nothing says why.
* `description:` is required for the same reason — it is the only text the model sees
  before deciding to open the skill.
* A skill must be named in the always-on text, or the model never knows to invoke it. An
  untriggered skill is dead weight that still looks like work.

Validation returns every problem at once rather than the first, because a model fixing one
error per turn wastes the loop it is in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SKILL_DIR = "skills"
SKILL_FILE = "SKILL.md"
SCRIPTS_DIR = "scripts"

# The frontmatter is the first block between `---` fences. Bounded so a file that is all
# prose cannot be mistaken for frontmatter, and so a scan is cheap.
FRONTMATTER_CHARS = 2000

_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.M)
_DESCRIPTION = re.compile(r"^description:\s*(.+?)\s*$", re.M)
_FENCE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_SKILL_PATH = re.compile(rf"^{SKILL_DIR}/([a-z0-9][a-z0-9_\-]*)/{SKILL_FILE}$")
_SKILL_SCRIPT = re.compile(rf"^{SKILL_DIR}/([a-z0-9][a-z0-9_\-]*)/{SCRIPTS_DIR}/([A-Za-z0-9_.\-]+)$")


@dataclass(frozen=True)
class Skill:
    """One skill as the model sees it before opening it: a name and a description."""

    name: str
    description: str
    path: str

    def advertised(self) -> str:
        """The one line that goes in the prompt. The body is deliberately absent."""
        return f"- {self.name}: {self.description} (open it with the `skill` tool)"


def frontmatter(text: str) -> str:
    """The frontmatter block, or "" when the file does not open with one."""
    match = _FENCE.match(text[:FRONTMATTER_CHARS])
    return match.group(1) if match else ""


def body(text: str) -> str:
    """The procedure: everything after the frontmatter."""
    match = _FENCE.match(text[:FRONTMATTER_CHARS])
    if not match:
        return text.strip()
    return text[match.end() :].strip()


def parse(name: str, path: str, text: str) -> Skill:
    """A skill from its file text. Missing fields come back empty, never invented."""
    fm = frontmatter(text)
    described = _DESCRIPTION.search(fm)
    return Skill(
        name=name,
        description=(described.group(1) if described else "").strip(),
        path=path,
    )


def discover(files: dict[str, str]) -> list[Skill]:
    """Every skill in a tree, sorted by name.

    `files` is the run's file map (path -> content), so this reads what the run actually
    has rather than what a directory happens to contain.
    """
    found: list[Skill] = []
    for path, text in sorted(files.items()):
        match = _SKILL_PATH.match(path)
        if not match:
            continue
        found.append(parse(match.group(1), path, str(text or "")))
    return found


def validate(files: dict[str, str], always_on_text: str) -> list[str]:
    """Every problem with the tree's skills, in one list.

    Empty means the tree is fine. Each message names the file and the fix, because the
    model reading it is the one who has to make the change.
    """
    errors: list[str] = []
    skills = discover(files)

    for skill in skills:
        text = str(files.get(skill.path) or "")
        fm = frontmatter(text)

        if not fm:
            errors.append(
                f"{skill.path} has no frontmatter block; it needs `---`, `name: {skill.name}`, "
                f"`description: ...`, `---`, or the skill is dropped silently at load"
            )
            continue

        declared = _NAME.search(fm)
        if not declared:
            errors.append(
                f"{skill.path} needs frontmatter `name: {skill.name}` matching its directory, "
                f"or the skill is dropped silently at load"
            )
        elif declared.group(1) != skill.name:
            errors.append(
                f"{skill.path} declares `name: {declared.group(1)}` but lives in "
                f"{SKILL_DIR}/{skill.name}/; the directory name is what a trigger line matches, "
                f"so make it `name: {skill.name}`"
            )

        if not skill.description:
            errors.append(
                f"{skill.path} needs a `description:` in its frontmatter; it is the only text "
                f"the model sees before deciding whether to open the skill"
            )

        if skill.name not in always_on_text:
            errors.append(
                f"skill '{skill.name}' is never named in the always-on text, so the model will "
                f"not know to invoke it — an untriggered skill is dead weight. Add a line like: "
                f'"When <the situation this skill handles>, load the `{skill.name}` skill."'
            )

    errors.extend(_check_script_refs(files))
    return errors


def _check_script_refs(files: dict[str, str]) -> list[str]:
    """A skill that invokes a helper must ship it, at the skill's own scripts/ path."""
    errors: list[str] = []
    shipped = {path for path in files if _SKILL_SCRIPT.match(path)}
    for path, text in sorted(files.items()):
        if not _SKILL_PATH.match(path):
            continue
        for ref in re.findall(r"scripts/([A-Za-z0-9_.\-]+\.py)", str(text or "")):
            if not any(item.endswith(f"/{SCRIPTS_DIR}/{ref}") for item in shipped):
                errors.append(
                    f"{path} invokes `scripts/{ref}` but that file is not in the harness; "
                    f"ship it at {path.rsplit('/', 1)[0]}/{SCRIPTS_DIR}/{ref}"
                )
    return errors


def census(files: dict[str, str]) -> dict[str, int]:
    """What the tree's skills actually are, for a metrics line."""
    skills = discover(files)
    return {
        "skills": len(skills),
        "described": sum(1 for skill in skills if skill.description),
        "skill_scripts": sum(1 for path in files if _SKILL_SCRIPT.match(path)),
    }


def catalog(files: dict[str, str]) -> str:
    """The advertised list: one line per skill, body withheld.

    This is what goes in the prompt. A tree with no skills returns "", so a prompt that
    interpolates it does not grow a heading over nothing.
    """
    skills = [skill for skill in discover(files) if skill.description]
    if not skills:
        return ""
    return "\n".join(skill.advertised() for skill in skills)


def open_skill(files: dict[str, str], name: str) -> str:
    """The skill's procedure, for the `skill` tool. Raises LookupError when absent."""
    wanted = name.strip().strip("`").strip()
    for skill in discover(files):
        if skill.name == wanted:
            text = str(files.get(skill.path) or "")
            procedure = body(text)
            if not procedure:
                raise LookupError(f"skill '{wanted}' has a frontmatter block but no procedure below it")
            return f"Skill `{wanted}` — {skill.description}\n\n{procedure}"
    known = ", ".join(skill.name for skill in discover(files)) or "none"
    raise LookupError(f"no skill named '{wanted}'; this run has: {known}")
