"""Guard: HMC-shaped values in src/ and tests/ must match captured evidence (issue #1202).

The committed vocabularies under ``tests/fixtures/live/vocabulary/`` record what real
HMCs returned (``scripts/live_capture_export.py`` derives them from a read-only sweep).
This guard fails when:

1. a literal value of an XML element whose values are closed by a schema enum is
   neither in that enum nor observed in any capture. It reads XML leaves in string
   literals (``<PartitionState>running</PartitionState>``) and, in Python, comparisons
   and dict entries keyed by the element (``x.get("PartitionState") == "running"``);
2. a REST read path in ``src/hmcpctl`` has no captured endpoint;
3. an ``ls*`` command in ``src/hmcpctl`` has no captured command with the same
   selecting flags (``-r``, ``--rsubtype``, ``--level``, ``-o``) and ``-m`` presence.

A Python line holding a value that is deliberately not an HMC answer (a test of
the unknown branch) ends with ``# live-vocabulary: allow <reason>``; an exemption
without a reason, or one that suppresses nothing, fails the guard.

Violations not yet fixed are listed in ``allowlist.json`` beside the vocabularies,
each with a reason citing #1202. An entry that matches no violation fails the guard
too, so the list only shrinks. ``--write-allowlist`` regenerates it from the tree,
keeping the reasons of entries that still apply.

Usage:
    python scripts/check_live_vocabulary.py [--root <path>] [--write-allowlist]

Exits 0 when clean, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import itertools
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
VOCABULARY_DIR = Path("tests/fixtures/live/vocabulary")
ALLOWLIST = VOCABULARY_DIR / "allowlist.json"
ISSUE = "#1202"
#: An inline exemption: a deliberately non-HMC value on this line, with its reason.
EXEMPTION = re.compile(r"#\s*live-vocabulary:\s*allow\b(.*)$")
#: Captured fixtures are evidence already; the pipeline's own tests hold deliberately
#: uncaptured values to prove the guard and the exporter reject them.
NOT_SCANNED = (
    "tests/fixtures/live/",
    "tests/scripts/test_check_live_vocabulary.py",
    "tests/scripts/test_live_capture_export.py",
)

LEAF = re.compile(
    r"<(?:[\w.-]+:)?([A-Za-z][\w.]*)(?:\s[^>]*)?>([^<]*)</(?:[\w.-]+:)?\1>"
)
KEY_FLAGS = ("-r", "--rsubtype", "--level", "-o")
_CASE_METHODS = {"lower": str.lower, "casefold": str.lower, "upper": str.upper}
#: A client method that reads: `_get`, `_web_get`, `get_uom`, `get_metrics_feed`, ...
#: Bare `get` is a mapping lookup, not a request.
_GET_HELPER = re.compile(r"^_?(?:\w+_)?get_\w+$|^_(?:\w+_)?get$|^raw_get$")
#: Client helpers that build a read path from a literal resource type: the path
#: each builds, over its parameters in positional order.
_HELPER_PATHS = {
    "list_uom": ("/rest/api/uom/{resource_type}", ("resource_type",)),
    "get_uom": ("/rest/api/uom/{resource_type}/{{}}", ("resource_type", "uuid")),
    "list_child": (
        "/rest/api/uom/{parent_type}/{{}}/{child_type}",
        ("parent_type", "parent_uuid", "child_type"),
    ),
    "get_quick_property": (
        "/rest/api/uom/{resource_type}/{{}}/quick/{property_name}",
        ("resource_type", "uuid", "property_name"),
    ),
    "search_uom": (
        "/rest/api/uom/{resource_type}/search/({property_name}=={{}})",
        ("resource_type", "property_name", "property_value"),
    ),
}


@dataclass(frozen=True)
class Violation:
    kind: str  # "literal" | "rest-path" | "cli-command"
    file: str
    subject: str
    value: str
    line: int = 0

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.kind, self.file, self.subject, self.value)


@dataclass
class Evidence:
    allowed: dict[str, set[str]]
    bindings: dict[str, str]
    endpoints: list[str]
    commands: list[str]


def load_evidence(root: Path) -> Evidence:
    """Every vocabulary and enum list, unioned: a value any firmware answered is valid."""
    directory = root / VOCABULARY_DIR
    types: dict[str, set[str]] = {}
    bindings: dict[str, set[str]] = {}
    for path in sorted(directory.glob("enums-*.json")):
        enum_list = json.loads(path.read_text(encoding="utf-8"))
        for name, values in enum_list["types"].items():
            types.setdefault(name, set()).update(values)
        # An enum list may bind elements itself, as the documented job statuses do.
        for element, enum in enum_list.get("elements", {}).items():
            bindings.setdefault(element, set()).add(enum)
    # A stated binding overrides one a vocabulary inferred from its literals: `Status` is
    # shared by jobs and VNIC backing devices, and a capture of only the latter infers
    # their enum, which would admit its values as job statuses (#1498).
    stated = set(bindings)
    vocabularies = [
        p
        for p in sorted(directory.glob("*.json"))
        if not p.name.startswith("enums-") and p.name != ALLOWLIST.name
    ]
    if not vocabularies:
        raise FileNotFoundError(f"no vocabulary in {directory}")
    observed: dict[str, set[str]] = {}
    endpoints: list[str] = []
    commands: list[str] = []
    for path in vocabularies:
        vocabulary = json.loads(path.read_text(encoding="utf-8"))
        rest = vocabulary["rest"]
        for element, values in rest["values"].items():
            observed.setdefault(element, set()).update(
                v for v in values if v[:1] != "<"
            )
        for element, enum in rest["element_enums"].items():
            if element not in stated:
                bindings.setdefault(element, set()).add(enum)
        endpoints += [e["path"] for e in rest["endpoints"] if e["method"] == "GET"]
        commands += [c["command"] for c in vocabulary["cli"]["commands"]]
    allowed = {
        element: observed.get(element, set()).union(*(types.get(e, ()) for e in enums))
        for element, enums in bindings.items()
    }
    named = {element: " or ".join(sorted(enums)) for element, enums in bindings.items()}
    return Evidence(allowed, named, endpoints, commands)


# --- literal values -----------------------------------------------------------


def _string_text(node: ast.AST) -> str | None:
    """A string literal's text, an f-string's placeholders rendered as ``{}``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
            else "{}"
            for v in node.values
        )
    return None


def _string_nodes(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Every string literal once: an f-string's constant parts are not yielded again."""
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            inside.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        text = _string_text(node)
        if text is not None and id(node) not in inside:
            yield node, text


def _strings(tree: ast.AST) -> Iterator[tuple[str, int]]:
    for node, text in _string_nodes(tree):
        yield text, getattr(node, "lineno", 0)


def _element_access(node: ast.AST) -> tuple[str, Any] | None:
    """``("PartitionState", case)`` for ``x.get("PartitionState")`` and its wrappings."""
    case = None
    while isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _CASE_METHODS
            and not node.args
        ):
            case = case or _CASE_METHODS[func.attr]
            node = func.value
        elif isinstance(func, ast.Attribute) and func.attr == "strip" and not node.args:
            node = func.value
        elif isinstance(func, ast.Name) and func.id == "str" and len(node.args) == 1:
            node = node.args[0]
        elif isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                return key.value, case
            return None
        else:
            return None
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value, case
    return None


def _literals(node: ast.AST) -> list[str] | None:
    """The plain string values *node* spells; an XML or multi-line string is not one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return None if re.search(r"[<\n]", node.value) else [node.value]
    if isinstance(node, ast.Tuple | ast.List | ast.Set) and node.elts:
        values = [_literals(e) for e in node.elts]
        if all(v is not None and len(v) == 1 for v in values):
            return [v[0] for v in values if v]
    return None


def _assigned_element(name: str) -> str:
    """The element an assignment target or keyword names: itself, or `Status` for a job.

    `PartitionState="down"` names its element outright; `wait_job_status = "running"`
    names a job's `Status`. Other snake_case names are hmcpctl's own parameters, whose
    values are CLI or API forms rather than XML values, so they are not mapped.
    """
    return "Status" if name.lower().endswith("job_status") else name


def _assignments(node: ast.AST) -> Iterator[tuple[str, ast.expr]]:
    """(name, value) for `x = v`, `obj.x = v` and `f(x=v)`."""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                yield target.attr, node.value
            elif isinstance(target, ast.Name):
                yield target.id, node.value
    elif isinstance(node, ast.keyword) and node.arg:
        yield node.arg, node.value


def _python_comparisons(tree: ast.AST) -> Iterator[tuple[str, str, int, Any]]:
    """(element, value, line, case) for comparisons, dict entries and assignments.

    Each is keyed by an element: `x.get("PartitionState") == "running"`,
    `{"PartitionState": "running"}`, `PartitionState="running"`.
    """
    for node in ast.walk(tree):
        for name, value in _assignments(node):
            for literal in _literals(value) or ():
                yield _assigned_element(name), literal, value.lineno, None
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for left, right in itertools.pairwise(operands):
                for side, other in ((left, right), (right, left)):
                    access = _element_access(side)
                    values = _literals(other)
                    if access and values:
                        for value in values:
                            yield access[0], value, node.lineno, access[1]
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                values = _literals(value) if isinstance(value, ast.Constant) else None
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and values
                ):
                    yield key.value, values[0], value.lineno, None


def _leaf_violations(
    text: str, first_line: int, file: str, evidence: Evidence
) -> Iterator[Violation]:
    for match in LEAF.finditer(text):
        element, value = match.group(1), match.group(2).strip()
        allowed = evidence.allowed.get(element)
        if allowed is not None and value and "{" not in value and value not in allowed:
            line = first_line + text.count("\n", 0, match.start())
            yield Violation("literal", file, element, value, line)


def literal_violations(file: str, text: str, evidence: Evidence) -> list[Violation]:
    if not file.endswith(".py"):
        return list(_leaf_violations(text, 1, file, evidence))
    tree = ast.parse(text, filename=file)
    found = [
        v
        for string, line in _strings(tree)
        for v in _leaf_violations(string, line, file, evidence)
    ]
    for element, value, line, case in _python_comparisons(tree):
        allowed = evidence.allowed.get(element)
        if allowed is None:
            continue
        if case is not None:
            allowed = {case(a) for a in allowed}
        if value not in allowed:
            found.append(Violation("literal", file, element, value, line))
    return found


# --- REST read paths ------------------------------------------------------------


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _issues_get(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if _GET_HELPER.match(name):
                return True
            if name == "_request" and node.args and _string_text(node.args[0]) == "GET":
                return True
    return False


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            ids.add(id(body[0].value))
    return ids


def _returned_values(function: ast.AST) -> set[int]:
    """Nodes *function* returns, directly or through a name it assigns and returns."""
    returns = [
        n.value for n in ast.walk(function) if isinstance(n, ast.Return) and n.value
    ]
    names = {r.id for r in returns if isinstance(r, ast.Name)}
    assigned = [
        n.value
        for n in ast.walk(function)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)
    ]
    return {id(n) for n in [*returns, *assigned]}


def _helper_path(call: ast.Call) -> str | None:
    """The read path a client helper call builds, when its type arguments are literal."""
    if not isinstance(call.func, ast.Attribute) or call.func.attr not in _HELPER_PATHS:
        return None
    template, parameters = _HELPER_PATHS[call.func.attr]
    arguments = dict(zip(parameters, call.args, strict=False))
    arguments.update((k.arg, k.value) for k in call.keywords if k.arg in parameters)
    texts = {}
    for name in re.findall(r"(?<!\{)\{(\w+)\}", template):
        text = _string_text(arguments[name]) if name in arguments else None
        if text is None or "{" in text:
            return None
        texts[name] = text
    return template.format(**texts)


def rest_templates(file: str, text: str) -> list[tuple[str, int]]:
    """Read path templates in *file*: placeholders become ``{}``."""
    tree = ast.parse(text, filename=file)
    docstrings = _docstring_ids(tree)
    found: list[tuple[str, int]] = []
    for function in _functions(tree):
        reads = _issues_get(function)
        returned = _returned_values(function)
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                path = _helper_path(node)
                if path:
                    found.append((path, node.lineno))
        for node, path in _string_nodes(function):
            if (
                not path.startswith("/rest/api/")
                or id(node) in docstrings
                or not (reads or id(node) in returned)
                or "/do/" in path
                or "Logon" in path
            ):
                continue
            found.append((path, getattr(node, "lineno", 0)))
    return sorted(set(found), key=lambda item: (item[1], item[0]))


def _path_pattern(template: str) -> re.Pattern[str]:
    parts = re.split(r"\{[^}]*\}", template)
    pattern = ".+?".join(re.escape(p) for p in parts)
    if template.endswith("/"):
        pattern += ".*"
    if "?" not in template:
        pattern += r"(?:\?.*)?"
    return re.compile(pattern)


def path_is_captured(template: str, endpoints: Sequence[str]) -> bool:
    pattern = _path_pattern(template)
    return any(pattern.fullmatch(endpoint) for endpoint in endpoints)


# --- CLI commands -----------------------------------------------------------------


def command_signature(
    command: str,
) -> tuple[str, tuple[tuple[str, str], ...], bool] | None:
    """(name, ((flag, value), ...), has -m); ``{}`` stands for any value."""
    try:
        words = shlex.split(command.replace("{}", "__ANY__"))
    except ValueError:
        return None
    if not words or not re.fullmatch(r"ls[a-z]+", words[0]):
        return None
    flags = tuple(
        (flag, words[words.index(flag) + 1].replace("__ANY__", "{}"))
        for flag in KEY_FLAGS
        if flag in words[:-1]
    )
    return words[0], flags, "-m" in words


def _signatures_match(source: tuple[Any, ...], captured: tuple[Any, ...]) -> bool:
    name, flags, has_m = source
    c_name, c_flags, c_has_m = captured
    if name != c_name or has_m != c_has_m:
        return False
    c = dict(c_flags)
    if set(c) != {flag for flag, _ in flags}:
        return False
    return all(
        value == "{}" or "{}" in value or c[flag] == value for flag, value in flags
    )


def cli_templates(file: str, text: str) -> list[tuple[str, int]]:
    tree = ast.parse(text, filename=file)
    found = []
    for string, line in _strings(tree):
        words = string.split()
        if (
            len(words) >= 2
            and re.fullmatch(r"ls[a-z]+", words[0])
            and words[1][0] == "-"
        ):
            found.append((string.strip(), line))
    return found


def command_is_captured(template: str, commands: Sequence[str]) -> bool:
    source = command_signature(template)
    if source is None:
        return True
    captured = (command_signature(c) for c in commands)
    return any(c is not None and _signatures_match(source, c) for c in captured)


# --- driver -------------------------------------------------------------------


def tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "src", "tests"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.split()


def _file_violations(file: str, text: str, evidence: Evidence) -> list[Violation]:
    found = literal_violations(file, text, evidence)
    if file.startswith("src/hmcpctl/") and file.endswith(".py"):
        for template, line in rest_templates(file, text):
            if not path_is_captured(template, evidence.endpoints):
                found.append(Violation("rest-path", file, template, "", line))
        for template, line in cli_templates(file, text):
            if not command_is_captured(template, evidence.commands):
                found.append(Violation("cli-command", file, template, "", line))
    return found


def apply_exemptions(
    file: str, text: str, violations: list[Violation]
) -> tuple[list[Violation], list[str]]:
    """Drop violations on a line carrying ``# live-vocabulary: allow <reason>``.

    An exemption with no reason, or one on a line with no violation, is an error, so
    an exemption cannot outlive the value it was written for.
    """
    exempt: dict[int, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        match = EXEMPTION.search(line)
        if match:
            exempt[number] = match.group(1).strip()
    kept = [v for v in violations if v.line not in exempt]
    used = {v.line for v in violations}
    errors = [
        f"{file}:{n}: live-vocabulary exemption has no reason"
        for n, reason in exempt.items()
        if not reason
    ]
    errors += [
        f"{file}:{n}: stale live-vocabulary exemption suppresses nothing; delete it"
        for n in exempt
        if n not in used
    ]
    return kept, errors


def collect_violations(
    root: Path, files: Sequence[str], evidence: Evidence
) -> tuple[list[Violation], list[str]]:
    """Violations not exempted inline, and errors in the inline exemptions."""
    found: list[Violation] = []
    errors: list[str] = []
    for file in files:
        if file.startswith(NOT_SCANNED) or not file.endswith((".py", ".xml", ".json")):
            continue
        text = (root / file).read_text(encoding="utf-8")
        kept, problems = apply_exemptions(
            file, text, _file_violations(file, text, evidence)
        )
        found += kept
        errors += problems
    return found, errors


def _default_reason(violation: Violation, evidence: Evidence) -> str:
    if violation.kind == "literal":
        enum = evidence.bindings[violation.subject]
        return (
            f"{violation.subject}={violation.value!r} is neither captured nor in {enum}; "
            f"awaiting its {ISSUE} domain fix"
        )
    what = "GET" if violation.kind == "rest-path" else "command"
    return f"no capture of this {what} yet; add it to the read-only sweep ({ISSUE})"


def _entry(violation: Violation, reason: str) -> dict[str, str]:
    return {
        "kind": violation.kind,
        "file": violation.file,
        "subject": violation.subject,
        "value": violation.value,
        "reason": reason,
    }


def _entry_key(entry: dict[str, str]) -> tuple[str, str, str, str]:
    return (entry["kind"], entry["file"], entry["subject"], entry.get("value", ""))


def write_allowlist(
    path: Path, violations: Sequence[Violation], evidence: Evidence
) -> int:
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    reasons = {_entry_key(e): e["reason"] for e in previous}
    entries = {}
    for v in violations:
        entries[v.key] = _entry(v, reasons.get(v.key) or _default_reason(v, evidence))
    ordered = [entries[k] for k in sorted(entries)]
    path.write_text(
        json.dumps(ordered, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(ordered)} allowlist entries to {path}")
    return 0


def check(root: Path, violations: Sequence[Violation]) -> list[str]:
    path = root / ALLOWLIST
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    allowed = {_entry_key(e) for e in entries}
    errors = [
        f"{ALLOWLIST}: entry {e['kind']} {e['file']} {e['subject']!r} has no reason citing {ISSUE}"
        for e in entries
        if ISSUE not in e.get("reason", "")
    ]
    seen = set()
    for v in sorted(violations, key=lambda v: (v.file, v.line)):
        seen.add(v.key)
        if v.key in allowed:
            continue
        if v.kind == "literal":
            detail = (
                f"{v.subject}={v.value!r} is neither captured nor a schema enum value"
            )
        elif v.kind == "rest-path":
            detail = f"GET {v.subject} has no captured endpoint"
        else:
            detail = f"`{v.subject}` has no captured command"
        errors.append(f"{v.file}:{v.line}: {detail}")
    errors += [
        f"{ALLOWLIST}: stale entry {e['kind']} {e['file']} {e['subject']!r} {e.get('value', '')!r}"
        " matches nothing; delete it"
        for e in entries
        if _entry_key(e) not in seen
    ]
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=_REPO_ROOT)
    parser.add_argument("--write-allowlist", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    evidence = load_evidence(root)
    violations, exemption_errors = collect_violations(
        root, tracked_files(root), evidence
    )
    if args.write_allowlist:
        return write_allowlist(root / ALLOWLIST, violations, evidence)
    errors = exemption_errors + check(root, violations)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(
            f"\n{len(errors)} live-vocabulary problem(s). Use the captured value; or capture "
            "the call (docs/live-testing.md, 'Capturing an HMC's vocabulary'); or, for a "
            f"value a {ISSUE} fix will change, run `python scripts/check_live_vocabulary.py "
            "--write-allowlist` and give the entry its reason; for a value that is "
            "deliberately not an HMC answer, end its line with "
            "`# live-vocabulary: allow <reason>`.",
            file=sys.stderr,
        )
        return 1
    print("live vocabulary: every checked value and read is captured or allowlisted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
