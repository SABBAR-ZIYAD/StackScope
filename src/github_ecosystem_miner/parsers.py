from __future__ import annotations

import configparser
import re
import tomllib

REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def name_from_requirement(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("-r", "-c", "--", "-f", "-i")):
        return None
    egg = re.search(r"#egg=([A-Za-z0-9._-]+)", stripped)
    if egg:
        return egg.group(1)
    if not stripped.startswith(("-e", "git+", "http", ".", "/")):
        stripped = stripped.split("#", 1)[0].strip()
    if stripped.startswith(("-e", "git+", "http", ".", "/")):
        return None
    match = REQUIREMENT_NAME.match(stripped)
    return match.group(1) if match else None


def parse_requirements(text: str) -> list[str]:
    return [
        name
        for name in (name_from_requirement(line) for line in text.splitlines())
        if name is not None
    ]


def parse_pyproject(text: str) -> list[str]:
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return []
    names: list[str] = []
    project = document.get("project") or {}
    requirements = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        requirements.extend(group or [])
    for group in (document.get("dependency-groups") or {}).values():
        requirements.extend(item for item in (group or []) if isinstance(item, str))
    names.extend(
        name
        for name in (
            name_from_requirement(requirement)
            for requirement in requirements
            if isinstance(requirement, str)
        )
        if name is not None
    )
    poetry = (document.get("tool") or {}).get("poetry") or {}
    poetry_tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    poetry_tables.extend(
        (group or {}).get("dependencies") or {} for group in (poetry.get("group") or {}).values()
    )
    for table in poetry_tables:
        names.extend(table.keys())
    return names


def parse_setup_py(text: str) -> list[str]:
    """Read only literal strings from dependency containers; never execute setup.py."""
    names: list[str] = []
    for key in ("install_requires", "extras_require", "tests_require"):
        for match in re.finditer(key + r"\s*=\s*([\[{])", text):
            opening = match.group(1)
            closing = "]" if opening == "[" else "}"
            depth = 0
            cursor = match.end() - 1
            while cursor < len(text):
                if text[cursor] == opening:
                    depth += 1
                elif text[cursor] == closing:
                    depth -= 1
                    if depth == 0:
                        break
                cursor += 1
            body = text[match.end() : cursor]
            for literal in re.finditer(r"""(["'])([^"'\n]+)\1""", body):
                if re.match(r"\s*:", body[literal.end() :]):
                    continue
                name = name_from_requirement(literal.group(2))
                if name:
                    names.append(name)
    return names


def parse_setup_cfg(text: str) -> list[str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return []
    names: list[str] = []
    if parser.has_option("options", "install_requires"):
        names.extend(parse_requirements(parser.get("options", "install_requires")))
    if parser.has_section("options.extras_require"):
        for _, value in parser.items("options.extras_require"):
            names.extend(parse_requirements(value))
    return names


def parse_environment_yml(text: str) -> list[str]:
    names: list[str] = []
    in_dependencies = False
    for line in text.splitlines():
        if re.match(r"^\S[^:]*:", line):
            in_dependencies = line.startswith("dependencies:")
            continue
        if not in_dependencies:
            continue
        match = re.match(r"^\s*-\s+([^\s#]+)", line)
        if not match or match.group(1).endswith(":"):
            continue
        specification = match.group(1).split("::")[-1]
        name = re.split(r"[=<>!~\[]", specification, maxsplit=1)[0]
        if name:
            names.append(name)
    return names


PARSERS = {
    "requirements.txt": parse_requirements,
    "pyproject.toml": parse_pyproject,
    "setup.py": parse_setup_py,
    "setup.cfg": parse_setup_cfg,
    "environment.yml": parse_environment_yml,
}


def looks_frozen_requirements(text: str) -> bool:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-"))
    ]
    if len(lines) < 30:
        return False
    pinned = sum(bool(re.match(r"^[A-Za-z0-9._-]+(?:\[[^]]+\])?==[^=]", line)) for line in lines)
    return pinned / len(lines) >= 0.90
