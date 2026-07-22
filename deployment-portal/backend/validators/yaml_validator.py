"""
yaml_validator.py — Deterministic YAML syntax and formatting checks.

Reports every issue as: Line <n> (<BLOCK>): <description> 💡 <hint>
Returns structured rows via ``validate_yaml_issues`` for AI remediation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml


class _DuplicateKeyError(Exception):
    """Raised when a YAML mapping has the same key twice at the same level."""


class _DupCheckLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys within the same mapping."""


def _construct_mapping(loader: _DupCheckLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    mapping: dict = {}
    dups: list[str] = []
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            dups.append(str(key))
        mapping[key] = loader.construct_object(value_node, deep=deep)
    if dups:
        raise _DuplicateKeyError(", ".join(dict.fromkeys(dups)))
    return mapping


_DupCheckLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)

_COMMENT = re.compile(r"^\s*#")
_KEY = re.compile(
    r"^(\s*)(?:-\s+)?"
    r"(?:"
    r"(?P<qkey>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^:#]+?)"
    r")\s*:\s*(?P<rest>.*)?$"
)
_LIST_ITEM = re.compile(r"^(\s*)-\s+(.*)$")
_BLOCK_SCALAR = re.compile(r"[>|][+-]?\d*\s*$")
# Inline `key:value` / `key:"value"` without whitespace after the mapping colon.
_INLINE_COLON_NO_SPACE = re.compile(
    r"^(\s*(?:-\s+)?"
    r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^:\s#][^:#]*?))"
    r":(?![\s|>])(?=\S)"
)
# `key :value` — space before colon, none after (e.g. `adf :1234`).
_SPACE_BEFORE_COLON = re.compile(
    r"^(\s*(?:-\s+)?(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^:\s#][^:#]*?))\s+:(?=\S)"
)
_ROOT_KEY = re.compile(r"^([A-Za-z0-9_-]+)\s*:", re.IGNORECASE)
# Indented identifier with no colon — invalid as a YAML key line.
_BARE_KEY = re.compile(r"^(\s+)([A-Za-z0-9_.-]+)\s*$")


@dataclass(frozen=True)
class YamlIssue:
    line: int
    description: str
    block: str = ""

    def to_row(self, *, hint: str = "") -> dict[str, Any]:
        return {
            "line": self.line,
            "block": self.block,
            "message": self.description,
            "hint": hint or _deterministic_hint(self.description, self.block),
        }

    def format(self, *, hint: str = "") -> str:
        row = self.to_row(hint=hint)
        return format_yaml_issue(row)


def format_yaml_issue(row: dict[str, Any]) -> str:
    """Format one issue row for the validation report UI."""
    line = int(row.get("line") or 0)
    block = str(row.get("block") or "").strip()
    message = str(row.get("message") or row.get("description") or "").strip()
    hint = str(row.get("hint") or "").strip()
    loc = f" ({block})" if block else ""
    text = f"Line {line}{loc}: {message}"
    if hint:
        text += f" 💡 {hint}"
    return text


def numbered_snippet(content: str, *, max_lines: int = 120) -> str:
    """Numbered YAML lines for Ollama (same line numbers as validator reports)."""
    rows: list[str] = []
    for i, line in enumerate(content.splitlines(), start=1):
        if i > max_lines:
            rows.append(f"... ({len(content.splitlines()) - max_lines} more lines)")
            break
        rows.append(f"{i}: {line}")
    return "\n".join(rows)


def validate_yaml(content: str, environment: str = "") -> tuple[bool, list[str]]:
    """Run all YAML checks. Returns (passed, messages)."""
    passed, rows = validate_yaml_issues(content, environment=environment)
    if passed:
        return True, ["Validation Passed"]
    return False, [format_yaml_issue(r) for r in rows]


def validate_yaml_issues(content: str, environment: str = "") -> tuple[bool, list[dict[str, Any]]]:
    """Run all YAML checks. Returns (passed, issue rows with line/block/message/hint)."""
    lines = content.splitlines()
    blocks = _root_blocks_by_line(lines)
    issues: list[YamlIssue] = []

    issues.extend(_with_blocks(_check_tabs(lines), blocks))
    issues.extend(_with_blocks(_check_missing_colon(lines), blocks))
    issues.extend(_with_blocks(_check_unclosed_quotes(lines), blocks))
    issues.extend(_with_blocks(_check_duplicate_keys_linebased(lines, blocks), blocks))
    issues.extend(_with_blocks(_check_missing_values(lines), blocks))
    issues.extend(_with_blocks(_check_colon_spacing(lines), blocks))
    issues.extend(_check_env_promotion_blocks(lines, environment))
    issues.extend(_with_blocks(_check_syntax_and_duplicate_keys(content, lines), blocks))
    issues.extend(_with_blocks(_check_indentation(lines, blocks), blocks))
    issues.extend(_with_blocks(_check_block_scalars(lines), blocks))
    issues.extend(_with_blocks(_check_list_alignment(lines, blocks), blocks))

    if not issues:
        return True, []

    seen: set[tuple[int, str]] = set()
    unique: list[YamlIssue] = []
    for issue in sorted(issues, key=lambda i: (i.line, i.description)):
        key = (issue.line, issue.description)
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)

    return False, _consolidate_issues(_filter_cascade_issues(unique, lines, blocks), lines)


def _prev_content_line(lines: list[str], line_no: int) -> int:
    for j in range(line_no - 2, -1, -1):
        if _COMMENT.match(lines[j]) or not lines[j].strip():
            continue
        return j + 1
    return 0


def _filter_cascade_issues(
    issues: list[YamlIssue],
    lines: list[str],
    blocks: dict[int, str],
) -> list[YamlIssue]:
    """Drop indent errors caused by a missing colon on the previous line."""
    missing_colon_lines = {
        i.line for i in issues if "missing a colon" in i.description.lower()
    }
    if not missing_colon_lines:
        return issues

    indent_markers = (
        "inconsistent indentation step",
        "indentation must be deeper",
        "child key must be indented",
        "wrong indentation",
        "invalid yaml syntax",
    )
    kept: list[YamlIssue] = []
    for issue in issues:
        low = issue.description.lower()
        if not any(m in low for m in indent_markers):
            kept.append(issue)
            continue
        prev = _prev_content_line(lines, issue.line)
        if prev in missing_colon_lines and blocks.get(issue.line) == blocks.get(prev):
            continue
        kept.append(issue)
    return kept


def _root_blocks_by_line(lines: list[str]) -> dict[int, str]:
    """Map each line number to the current root env block (INTEG-ADD, COMMON-*, etc.)."""
    current = ""
    by_line: dict[int, str] = {}
    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            by_line[i] = current
            continue
        if not _leading_ws(line):
            m = _ROOT_KEY.match(line.strip())
            if m:
                current = m.group(1)
        by_line[i] = current
    return by_line


def _with_blocks(issues: list[YamlIssue], blocks: dict[int, str]) -> list[YamlIssue]:
    out: list[YamlIssue] = []
    for issue in issues:
        block = issue.block or blocks.get(issue.line, "")
        out.append(YamlIssue(issue.line, issue.description, block=block))
    return out


def _extract_key_from_line(line: str) -> str:
    """Best-effort key name from a `key: value` line."""
    body = line.split("#", 1)[0].rstrip()
    m = _KEY.match(body)
    if not m:
        return ""
    raw = (m.group("qkey") or "").strip()
    if raw.startswith(("'", '"')) and len(raw) >= 2:
        return raw[1:-1]
    return raw


def _line_has_child(lines: list[str], line_no: int) -> bool:
    """True when a later non-comment line is indented deeper than ``line_no``."""
    if line_no < 1 or line_no > len(lines):
        return False
    base = _indent_cols(_leading_ws(lines[line_no - 1]))
    for j in range(line_no, len(lines)):
        nxt = lines[j]
        if _COMMENT.match(nxt) or not nxt.strip():
            continue
        return _indent_cols(_leading_ws(nxt)) > base
    return False


def _extract_bare_key(line: str) -> str:
    """Key name from a line that has no colon (``reports`` instead of ``reports:``)."""
    m = _BARE_KEY.match(line.split("#", 1)[0].rstrip())
    return m.group(2) if m else ""


def _is_root_block_line(line: str) -> bool:
    if _COMMENT.match(line) or not line.strip() or _leading_ws(line):
        return False
    return bool(_ROOT_KEY.match(line.strip()))


def _classify_issue(description: str) -> str:
    low = description.lower()
    if "missing a colon" in low or "missing colon" in low:
        return "missing_colon"
    if "unclosed" in low and "quote" in low:
        return "unclosed_quote"
    if "has no value" in low:
        return "missing_value"
    if "duplicate key" in low:
        return "duplicate_key"
    if "missing space after colon" in low or "space before colon" in low:
        return "colon"
    if (
        "inconsistent indentation step" in low
        or "child key must be indented" in low
        or "indentation must be deeper" in low
    ):
        return "indent"
    if "same hierarchy level" in low:
        return "sibling"
    if "invalid yaml syntax" in low:
        return "syntax"
    if "tab" in low:
        return "tab"
    if "section found" in low:
        return "env_block"
    return "other"


def _build_simple_issue(
    line_no: int,
    block: str,
    line_text: str,
    types: set[str],
    *,
    descriptions: list[str] | None = None,
) -> tuple[str, str]:
    """One plain message + hint per line (no duplicate rule noise)."""
    key = _extract_key_from_line(line_text) or _extract_bare_key(line_text)
    desc_blob = " ".join(descriptions or [])

    if "missing_colon" in types:
        return (
            f"Key `{key}` is missing a colon.",
            f"Change `{key}` to `{key}:`.",
        )

    if "unclosed_quote" in types:
        return (
            "Unclosed double quote in value.",
            f"Close the string on this line — e.g. `{key}: \"true\"`."
            if key
            else "Close the opening `\"` on this line.",
        )

    if "missing_value" in types:
        return (
            f"Key `{key}` has no value.",
            f"Add a value after `{key}:` or remove the key.",
        )

    if "duplicate_key" in types:
        first_m = re.search(r"first at line (\d+)", desc_blob, re.I)
        first = first_m.group(1) if first_m else ""
        hint = (
            f"Remove this duplicate or rename it (first `{key}` at line {first})."
            if first
            else f"Remove or rename the duplicate `{key}` entry."
        )
        return (f"Duplicate key `{key}`.", hint)

    has_colon = "colon" in types
    has_indent = "indent" in types

    if has_colon and has_indent:
        raw_val = line_text.split("#", 1)[0].split(":", 1)[-1].strip() if ":" in line_text else ""
        example = f"`{key}: {raw_val}`" if key and raw_val else f"`{key}: <value>`"
        return (
            f"Wrong indentation and missing space after colon on `{key}`.",
            f"Align with sibling keys and write {example} (space required after `:`).",
        )

    if has_colon:
        raw_val = line_text.split("#", 1)[0].split(":", 1)[-1].strip() if ":" in line_text else ""
        if key and raw_val:
            return (
                f"Missing space after colon on `{key}`.",
                f"Change to `{key}: {raw_val}`.",
            )
        return (
            "Missing space after colon.",
            "Use `key: value` with a space after `:`.",
        )

    if has_indent:
        return (
            "Wrong indentation.",
            "Align this key with siblings under the same parent (2 or 4 spaces per level).",
        )

    if "tab" in types:
        return (
            "Tab character used for indentation.",
            "Replace tabs with spaces.",
        )

    if "syntax" in types:
        return (
            "Invalid YAML.",
            "Fix indentation, colons, and quotes on this line.",
        )

    return (
        "YAML formatting problem.",
        "Fix indentation and `key: value` formatting on this line.",
    )


def _consolidate_issues(issues: list[YamlIssue], lines: list[str]) -> list[dict[str, Any]]:
    """Collapse multiple rule hits on the same line into one clear message."""
    by_line: dict[int, list[YamlIssue]] = {}
    for issue in issues:
        by_line.setdefault(issue.line, []).append(issue)

    rows: list[dict[str, Any]] = []
    for line_no in sorted(by_line.keys()):
        group = by_line[line_no]
        block = next((i.block for i in group if i.block), "")
        line_text = lines[line_no - 1] if 1 <= line_no <= len(lines) else ""

        types = {_classify_issue(i.description) for i in group}
        if "env_block" in types:
            desc = group[0].description
            rows.append({
                "line": line_no,
                "block": block,
                "message": desc,
                "hint": _deterministic_hint(desc, block),
            })
            continue

        # Drop parser/sibling noise when we already have a concrete formatting error.
        concrete = {
            "missing_colon", "unclosed_quote", "missing_value",
            "duplicate_key", "colon", "indent",
        }
        if types & concrete:
            types -= {"syntax", "sibling"}

        msg, hint = _build_simple_issue(
            line_no, block, line_text, types,
            descriptions=[i.description for i in group],
        )
        rows.append({
            "line": line_no,
            "block": block,
            "message": msg,
            "hint": hint,
        })
    return rows


def _deterministic_hint(description: str, block: str = "") -> str:
    """Fallback fix hint when Ollama is unavailable."""
    low = description.lower()
    block_ref = f" under `{block}`" if block else ""
    if "missing space after colon" in low:
        return f"Add a space after `:`{block_ref} — use `key: value`, not `key:value`."
    if "space before colon" in low:
        return f"Remove the space before `:` and add one after it{block_ref} — e.g. `adf: 1234`."
    if "inconsistent indentation step (1 spaces)" in low:
        return (
            f"Use 2 or 4 spaces per indent level{block_ref} — align with sibling keys "
            "(match indentation used in UAT-ADD / PROD-ADD blocks)."
        )
    if "inconsistent indentation step" in low:
        return f"Use consistent 2- or 4-space indentation steps{block_ref}."
    if "invalid yaml syntax" in low:
        return f"Fix YAML syntax on this line{block_ref} — check colons, quotes, and indentation."
    if "duplicate key" in low:
        return f"Remove or rename the duplicate key{block_ref}."
    if "no " in low and " section found" in low:
        return f"Add a root `{block or 'INTEG'}-ADD` or `COMMON-*` block for this deploy environment."
    if "tabs are not allowed" in low or "tab character" in low:
        return "Replace tabs with spaces for indentation."
    if "indentation must be deeper" in low:
        return f"Indent this line further than its parent key{block_ref}."
    if "child key must be indented" in low:
        return f"Add 2 or 4 more spaces so this key is nested under its parent{block_ref}."
    if "same hierarchy level" in low:
        return f"Align sibling keys to the same column{block_ref}."
    return ""


def _leading_ws(line: str) -> str:
    m = re.match(r"^(\s*)", line)
    return m.group(1) if m else ""


def _indent_cols(ws: str) -> int:
    return len(ws.replace("\t", "    "))


def _check_missing_colon(lines: list[str]) -> list[YamlIssue]:
    """Detect ``key`` lines that should be ``key:`` before a nested block."""
    out: list[YamlIssue] = []
    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            continue
        body = line.split("#", 1)[0].rstrip()
        if not _BARE_KEY.match(body) or _KEY.match(body):
            continue
        if not _line_has_child(lines, i):
            continue
        key = _extract_bare_key(line)
        if key:
            out.append(YamlIssue(i, f"Key `{key}` is missing a colon after the key name."))
    return out


def _check_unclosed_quotes(lines: list[str]) -> list[YamlIssue]:
    """Detect lines with an odd number of unescaped double quotes."""
    out: list[YamlIssue] = []
    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            continue
        body = line.split("#", 1)[0]
        if body.count('"') % 2 == 1:
            out.append(YamlIssue(i, "Unclosed double quote in value."))
    return out


def _check_duplicate_keys_linebased(lines: list[str], blocks: dict[int, str]) -> list[YamlIssue]:
    """Find duplicate keys at the same level without requiring a full YAML parse."""
    out: list[YamlIssue] = []
    stack: list[tuple[int, dict[str, int]]] = [(-1, {})]

    for i, line in enumerate(lines, start=1):
        if _is_root_block_line(line):
            stack = [(-1, {})]

        if _COMMENT.match(line) or not line.strip():
            continue

        body = line.split("#", 1)[0].rstrip()
        m = _KEY.match(body)
        if not m:
            continue

        key = _extract_key_from_line(line)
        if not key:
            continue

        ind = _indent_cols(_leading_ws(line))
        while len(stack) > 1 and ind <= stack[-1][0]:
            stack.pop()

        scope = stack[-1][1]
        if key in scope:
            out.append(
                YamlIssue(
                    i,
                    f"Duplicate key `{key}` (first at line {scope[key]}).",
                )
            )
        else:
            scope[key] = i

        stack.append((ind, {}))

    return out


def _check_missing_values(lines: list[str]) -> list[YamlIssue]:
    """Flag `key:` lines with no inline value and no indented child block."""
    out: list[YamlIssue] = []
    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            continue
        m = _KEY.match(line.split("#", 1)[0].rstrip())
        if not m:
            continue
        rest = (m.group("rest") or "").strip()
        if rest or _BLOCK_SCALAR.search(line):
            continue
        key = _extract_key_from_line(line)
        if not key or _line_has_child(lines, i):
            continue
        out.append(YamlIssue(i, f"Key `{key}` has no value."))
    return out


def _check_colon_spacing(lines: list[str]) -> list[YamlIssue]:
    """Require a space after `:` before an inline scalar value."""
    out: list[YamlIssue] = []
    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            continue
        body = line.split("#", 1)[0].rstrip()
        if _INLINE_COLON_NO_SPACE.match(body):
            out.append(
                YamlIssue(
                    i,
                    "Missing space after colon before value — use `key: value` format "
                    "(e.g. `external-order-detail-computation-required-countries: \"MYS,SGP\"`).",
                )
            )
        elif _SPACE_BEFORE_COLON.search(body):
            out.append(
                YamlIssue(
                    i,
                    "Space before colon and missing space after — use `key: value`, not `key :value`.",
                )
            )
    return out


def _normalize_env(environment: str) -> str:
    env = (environment or "").strip().upper()
    aliases = {"INT": "INTEG", "INTEGRATION": "INTEG"}
    return aliases.get(env, env)


def _check_env_promotion_blocks(lines: list[str], environment: str) -> list[YamlIssue]:
    """Require at least one root `{ENV}-*` block or a `COMMON-*` block."""
    env = _normalize_env(environment)
    if not env:
        return []

    has_common = False
    has_env = False
    for line in lines:
        if _COMMENT.match(line) or not line.strip():
            continue
        if _leading_ws(line):
            continue
        m = _ROOT_KEY.match(line.strip())
        if not m:
            continue
        key = m.group(1).upper()
        if key.startswith("COMMON-"):
            has_common = True
        elif key.startswith(f"{env}-"):
            has_env = True

    if has_common or has_env:
        return []

    return [
        YamlIssue(
            1,
            f"No {env}-* or COMMON-* section found — no YAML changes to promote to {env}.",
            block="",
        )
    ]


def _check_tabs(lines: list[str]) -> list[YamlIssue]:
    out: list[YamlIssue] = []
    for i, line in enumerate(lines, start=1):
        ws = _leading_ws(line)
        if "\t" in ws:
            out.append(YamlIssue(i, "Tabs are not allowed for indentation — use spaces only."))
        elif "\t" in line:
            out.append(YamlIssue(i, "Tab character found — use spaces only for indentation."))
    return out


def _check_syntax_and_duplicate_keys(content: str, lines: list[str]) -> list[YamlIssue]:
    out: list[YamlIssue] = []
    try:
        list(yaml.load_all(content, Loader=_DupCheckLoader))
    except _DuplicateKeyError as exc:
        line = _duplicate_key_line(content, str(exc))
        out.append(YamlIssue(line, f"Duplicate key `{exc}` within the same mapping."))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else 1
        problem = getattr(exc, "problem", None) or str(exc)
        out.append(YamlIssue(line, f"Invalid YAML syntax — {problem}."))
    return out


def _duplicate_key_line(content: str, key: str) -> int:
    """Best-effort line number for a duplicated key (first repeat)."""
    seen = False
    for i, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(rf"(?<![\w.-]){re.escape(key)}\s*:", line):
            if seen:
                return i
            seen = True
    return 1


def _check_indentation(lines: list[str], blocks: dict[int, str]) -> list[YamlIssue]:
    """Consistent spaces and parent/child hierarchy (scoped per root block)."""
    out: list[YamlIssue] = []
    stack: list[tuple[int, str]] = [(0, "root")]

    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            continue

        ws = _leading_ws(line)
        indent = _indent_cols(ws)

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        parent_indent = stack[-1][0]
        if indent <= parent_indent and parent_indent > 0:
            out.append(
                YamlIssue(
                    i,
                    "Indentation must be deeper than the parent key — this line changes the YAML structure.",
                )
            )

        if indent > parent_indent:
            step = indent - parent_indent
            if step not in (2, 4):
                out.append(
                    YamlIssue(
                        i,
                        f"Inconsistent indentation step ({step} spaces) — use 2 or 4 spaces per level.",
                    )
                )

        m_key = _KEY.match(line)
        if m_key:
            key_indent = _indent_cols(m_key.group(1) or "")
            if parent_indent and key_indent <= parent_indent:
                out.append(
                    YamlIssue(
                        i,
                        "Child key must be indented more than its parent key.",
                    )
                )
            stack.append((key_indent, "map"))
            continue

        m_list = _LIST_ITEM.match(line)
        if m_list:
            list_indent = _indent_cols(m_list.group(1) or "")
            if parent_indent and list_indent <= parent_indent:
                out.append(
                    YamlIssue(
                        i,
                        "List item must be indented more than its parent block.",
                    )
                )
            remainder = (m_list.group(2) or "").strip()
            if remainder and ":" in remainder.split("#", 1)[0]:
                stack.append((list_indent + 2, "list-map"))
            else:
                stack.append((list_indent, "list"))

    return out


def _check_list_alignment(lines: list[str], blocks: dict[int, str]) -> list[YamlIssue]:
    """List items (-) at the same level should share indentation."""
    out: list[YamlIssue] = []
    groups: dict[tuple[str, int, ...], set[int]] = {}

    path: list[int] = []
    stack: list[tuple[int, str]] = [(0, "root")]
    root = ""

    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            continue
        root = blocks.get(i, root)
        indent = _indent_cols(_leading_ws(line))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
            if path:
                path.pop()

        m_list = _LIST_ITEM.match(line)
        if not m_list:
            m_key = _KEY.match(line)
            if m_key:
                stack.append((_indent_cols(m_key.group(1) or ""), "map"))
                path.append(_indent_cols(m_key.group(1) or ""))
            continue

        list_indent = _indent_cols(m_list.group(1) or "")
        key = (root, *path, stack[-1][0])
        groups.setdefault(key, set()).add(list_indent)
        if len(groups[key]) > 1:
            out.append(
                YamlIssue(
                    i,
                    "List items (-) at the same level are not aligned to the same indentation.",
                )
            )

    return out


def _check_block_scalars(lines: list[str]) -> list[YamlIssue]:
    """Content under | or > must be indented deeper than the block indicator."""
    out: list[YamlIssue] = []
    block_base: int | None = None

    for i, line in enumerate(lines, start=1):
        if _COMMENT.match(line) or not line.strip():
            continue

        m = _KEY.match(line)
        if m and _BLOCK_SCALAR.search(line):
            block_base = _indent_cols(m.group(1) or "")
            continue

        if block_base is not None:
            indent = _indent_cols(_leading_ws(line))
            if indent <= block_base:
                out.append(
                    YamlIssue(
                        i,
                        "Multiline block content must be indented more than the `|` or `>` indicator.",
                    )
                )
                block_base = None
                if m and _BLOCK_SCALAR.search(line):
                    block_base = _indent_cols(m.group(1) or "")
                continue

    return out
