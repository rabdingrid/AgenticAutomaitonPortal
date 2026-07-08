"""
liquibase_validator.py — deterministic Liquibase pre-check for DB validation.

Validates a merged effective changelog (Common.sql + env-specific SQL) without
comparing to a prior release. Only changesets labeled Approved are validated;
Pending changesets are ignored.

Changeset headers must use `-- Changeset author:id` (space after `--`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

_LIQUIBASE_HEADER = re.compile(
    r"^\s*--\s*liquibase\s+formatted\s+sql\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Only `-- Changeset` (whitespace required after `--`) — not `--changeset`.
_CHANGESET_HEADER = re.compile(
    r"^\s*--\s+changeset\s+(\S+)(?:\s+(.*))?$",
    re.IGNORECASE | re.MULTILINE,
)
_INVALID_CHANGESET_HEADER = re.compile(
    r"^\s*--changeset\b",
    re.IGNORECASE | re.MULTILINE,
)
_LABEL_RE = re.compile(r"labels:\s*(\S+)", re.IGNORECASE)
_AUTHOR_ID_RE = re.compile(r"^([^:\s]+):([^:\s]+)$")

_DANGEROUS_SQL = (
    ("drop database", "DROP DATABASE"),
    ("drop table", "DROP TABLE"),
    ("truncate", "TRUNCATE"),
    ("delete from", "DELETE FROM"),
)

_SQL_KEYWORDS = re.compile(
    r"\b(ALTER|CREATE|INSERT|UPDATE|DELETE|DROP|TRUNCATE|GRANT|REVOKE|MERGE|"
    r"CALL|EXEC|EXECUTE|COMMENT|RENAME|SELECT)\b",
    re.IGNORECASE,
)
_DDL_START = re.compile(
    r"^\s*(ALTER|CREATE|DROP|INSERT|UPDATE|DELETE|TRUNCATE|GRANT|REVOKE|MERGE|CALL|EXEC)\b",
    re.IGNORECASE,
)
# Common Liquibase/SQL typos and malformed fragments.
_SQL_TYPO_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bADD\s+COLUM\b", re.IGNORECASE), "typo `ADD COLUM` — did you mean `ADD COLUMN`?"),
    (re.compile(r"\bVARCHR\b", re.IGNORECASE), "typo `VARCHR` — did you mean `VARCHAR`?"),
    (re.compile(r"\bVARCHAR\s*\(\s*\)", re.IGNORECASE), "empty `VARCHAR()` length"),
    (re.compile(r",\s*;", re.IGNORECASE), "trailing comma before semicolon"),
    (re.compile(r";\s*;", re.IGNORECASE), "duplicate semicolon"),
    (re.compile(r"\bFROM\s*,", re.IGNORECASE), "missing table name after `FROM`"),
    (re.compile(r"\bWHERE\s*;", re.IGNORECASE), "empty `WHERE` clause"),
    (re.compile(r"\bSET\s*,", re.IGNORECASE), "missing column assignment after `SET`"),
    (re.compile(r"\bWHRE\b", re.IGNORECASE), "typo `WHRE` — did you mean `WHERE`?"),
    (re.compile(r"(?<![<>=!])=\s+AND\b", re.IGNORECASE), "missing value before `AND`"),
]
_PARSE_DIALECTS = ("postgres", "mysql", "tsql")


def _sql_dialect() -> str:
    """SQL dialect for syntax validation (env VALIDATION_SQL_DIALECT, default mysql)."""
    d = (os.getenv("VALIDATION_SQL_DIALECT") or "mysql").strip().lower()
    return d if d in _PARSE_DIALECTS else "mysql"


def _require_statement_semicolon() -> bool:
    """Require every SQL statement in a changeset to end with `;` (default true)."""
    v = (os.getenv("VALIDATION_SQL_REQUIRE_SEMICOLON") or "true").strip().lower()
    return v in ("1", "true", "yes", "on")


def _trim_changeset_body(body: str) -> str:
    """Remove trailing `--liquibase formatted sql` lines absorbed from the next section."""
    return re.sub(
        r"(?:\n\s*--\s*liquibase\s+formatted\s+sql\s*)+$",
        "",
        body,
        flags=re.IGNORECASE,
    ).rstrip()


def _is_executable_sql_fragment(text: str) -> bool:
    """True when text contains SQL after stripping line/block comments."""
    return bool(_strip_sql_comments(text).strip())


def _split_sql_statements(body: str) -> list[tuple[str, int]]:
    """Split a changeset body into executable statements with start offsets."""
    try:
        import sqlparse
    except ImportError:
        stmt = body.strip()
        return [(stmt, 0)] if stmt and _is_executable_sql_fragment(stmt) else []

    out: list[tuple[str, int]] = []
    search_from = 0
    for raw in sqlparse.split(body):
        stmt = raw.strip()
        if not stmt or not _is_executable_sql_fragment(stmt):
            continue
        idx = body.find(stmt, search_from)
        if idx < 0:
            idx = search_from
        out.append((stmt, idx))
        search_from = idx + len(stmt)
    return out


@dataclass
class DbFinding:
    """One validation issue with file/line context for grouped reporting."""

    file: str
    line: int
    message: str
    changeset: str = ""
    changeset_line: int = 0
    severity: str = "error"
    hint: str = ""


@dataclass
class ParsedChangeset:
    author: str
    changeset_id: str
    full_id: str
    labels: list[str]
    header_line: str
    body: str
    line_number: int
    source_file: str = ""

    @property
    def is_approved(self) -> bool:
        return any(lbl.lower() == "approved" for lbl in self.labels)

    @property
    def is_pending(self) -> bool:
        return any(lbl.lower() == "pending" for lbl in self.labels) and not self.is_approved


def parse_changesets(content: str, *, source_file: str = "") -> list[ParsedChangeset]:
    """Split SQL content into changeset blocks (`-- Changeset` only)."""
    if not content or not content.strip():
        return []

    out: list[ParsedChangeset] = []
    for match in _CHANGESET_HEADER.finditer(content):
        author_id = match.group(1).strip()
        tail = (match.group(2) or "").strip()
        labels = [m.group(1) for m in _LABEL_RE.finditer(tail)]
        if not labels:
            labels = [m.group(1) for m in _LABEL_RE.finditer(match.group(0))]

        start = match.end()
        next_m = _CHANGESET_HEADER.search(content, start)
        end = next_m.start() if next_m else len(content)
        body = content[start:end].strip()
        body = _trim_changeset_body(body)

        am = _AUTHOR_ID_RE.match(author_id)
        author = am.group(1) if am else ""
        cid = am.group(2) if am else ""

        line_number = content[: match.start()].count("\n") + 1
        out.append(
            ParsedChangeset(
                author=author,
                changeset_id=cid,
                full_id=author_id if ":" in author_id else author_id,
                labels=labels,
                header_line=match.group(0).strip(),
                body=body,
                line_number=line_number,
                source_file=source_file,
            )
        )
    return out


def merge_db_contents(file_parts: list[tuple[str, str]]) -> str:
    """Concatenate SQL files into one effective changelog."""
    chunks: list[str] = []
    for fname, body in file_parts:
        if not body or not body.strip():
            continue
        chunks.append(f"/* --- {fname} --- */\n{body.strip()}\n")
    return "\n\n".join(chunks)


def _strip_sql_comments(sql: str) -> str:
    """Remove line and block comments for lightweight syntax heuristics."""
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql.strip()


def _body_is_commented_only(body: str) -> bool:
    """True when the changeset has text but no executable SQL after stripping comments."""
    if not body or not body.strip():
        return False
    return not _strip_sql_comments(body)


def _line_in_body(body: str, offset: int, header_line: int) -> int:
    """Map a character offset inside a changeset body to a 1-based file line."""
    return header_line + 1 + body[:offset].count("\n")


def _stmt_end_line(body: str, offset: int, stmt_text: str, header_line: int) -> int:
    """Line number where a statement ends (where `;` is expected)."""
    trimmed = stmt_text.rstrip()
    if not trimmed:
        return _line_in_body(body, offset, header_line)
    end_offset = offset + len(stmt_text) - len(stmt_text.lstrip()) + len(trimmed) - 1
    return _line_in_body(body, max(offset, end_offset), header_line)


_STMT_KIND = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|SELECT|ALTER|CREATE|DROP|TRUNCATE|MERGE|CALL|EXEC)\b",
    re.IGNORECASE,
)


def _statement_kind(stmt_text: str) -> str:
    m = _STMT_KIND.search(_strip_sql_comments(stmt_text))
    return m.group(1).upper() if m else "SQL"


def _canonical_message(msg: str) -> str:
    low = msg.lower()
    if "whre" in low:
        return "typo `WHRE` — did you mean `WHERE`?"
    if "semicolon" in low:
        return msg  # keep statement kind (UPDATE, INSERT, …)
    if "missing value" in low and "and" in low:
        return "missing value before `AND` (e.g. `column = AND`)"
    return msg


def _consolidate_issues(items: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Merge duplicate messages; combine line numbers when the same issue repeats."""
    buckets: dict[str, list[int]] = {}
    for line, msg in items:
        buckets.setdefault(_canonical_message(msg), []).append(line)
    out: list[tuple[int, str]] = []
    for cmsg, lines in buckets.items():
        uniq = sorted(set(lines))
        if len(uniq) == 1:
            out.append((uniq[0], cmsg))
        else:
            out.append((uniq[0], f"{cmsg} (also at lines {', '.join(str(l) for l in uniq[1:])})"))
    return sorted(out, key=lambda x: (x[0], x[1]))


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_parse_error(raw: str, *, stmt_text: str = "") -> str:
    """Turn sqlglot errors into short, human-readable messages."""
    s = _ANSI_RE.sub("", str(raw))
    s = s.replace("[4m", "").replace("[0m", "")
    s = re.sub(r"Line \d+, Col: \d+\.\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    probe = f"{s} {stmt_text}"

    if re.search(r"\bWHRE\b", probe, re.IGNORECASE):
        return "typo `WHRE` — did you mean `WHERE`?"
    if re.search(r"(?<![<>=!])=\s+AND\b", probe, re.IGNORECASE):
        return "missing value before `AND` (e.g. `column = AND`)"
    if "Required keyword: 'expression' missing" in s or "sqlglot.expressions" in s:
        return "Missing value in expression — check operands around AND/OR"
    if s.startswith("Invalid expression") or "Unexpected token" in s:
        return "Invalid SQL syntax — check keywords, parentheses, and commas"
    if s.startswith("SQL token error"):
        return s
    if len(s) > 140:
        s = s[:137] + "…"
    return s or "Invalid SQL syntax"


def _heuristic_body_issues(
    body: str, *, header_line: int,
) -> list[tuple[int, str]]:
    """Changeset-wide checks (quotes, parentheses)."""
    issues: list[tuple[int, str]] = []
    stripped = _strip_sql_comments(body)
    if not stripped:
        return issues

    normalized = stripped.replace("''", "")
    if normalized.count("'") % 2 != 0:
        issues.append((header_line + 1, "unbalanced single quotes in changeset"))

    depth = 0
    for i, ch in enumerate(stripped):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                issues.append((_line_in_body(body, i, header_line), "unbalanced `)`"))
                break
    if depth > 0:
        issues.append((header_line + 1, "unclosed `(` in changeset"))

    return issues


def _heuristic_statement_issues(
    stmt_text: str,
    body: str,
    offset: int,
    *,
    header_line: int,
) -> list[tuple[int, str]]:
    """Per-statement typo / pattern checks (avoids cross-statement false positives)."""
    issues: list[tuple[int, str]] = []
    stripped = _strip_sql_comments(stmt_text)
    if not stripped:
        return issues

    for pattern, msg in _SQL_TYPO_PATTERNS:
        m = pattern.search(stripped)
        if m:
            # Map match offset from stripped stmt back into body via stmt_text alignment.
            raw_m = pattern.search(stmt_text)
            pos = raw_m.start() if raw_m else m.start()
            issues.append((_line_in_body(body, offset + pos, header_line), msg))

    return issues


def _heuristic_sql_issues(
    body: str, *, header_line: int, source_file: str,
) -> list[tuple[int, str]]:
    """Legacy wrapper — body + per-statement heuristics."""
    issues = _heuristic_body_issues(body, header_line=header_line)
    stripped = _strip_sql_comments(body)
    if stripped and not _SQL_KEYWORDS.search(stripped):
        issues.append((header_line + 1, "no recognizable SQL statement"))
    for stmt_text, offset in _split_sql_statements(body):
        issues.extend(_heuristic_statement_issues(
            stmt_text, body, offset, header_line=header_line,
        ))
    return issues


def _sqlglot_statement_issues(
    stmt_text: str,
    body: str,
    offset: int,
    *,
    header_line: int,
) -> list[tuple[int, str]]:
    """Parse one statement with sqlglot."""
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import ParseError, TokenError
    except ImportError:
        return []

    dialect = _sql_dialect()
    issues: list[tuple[int, str]] = []
    stmt_line = _line_in_body(body, offset, header_line)

    parse_text = stmt_text.rstrip().rstrip(";").strip()
    if not parse_text:
        return issues

    try:
        parsed = sqlglot.parse(parse_text, read=dialect)
        stmts = [s for s in parsed if s is not None]
        if not stmts:
            issues.append((stmt_line, "empty or unparseable statement"))
            return issues
        for node in stmts:
            if isinstance(node, exp.Command) and _DDL_START.match(node.sql()):
                snippet = node.sql()[:70].strip()
                issues.append((
                    stmt_line,
                    f"invalid SQL near `{snippet}` (check parentheses, keywords, and semicolons)",
                ))
    except TokenError as e:
        msg = str(e).replace("Error tokenizing", "SQL token error")
        issues.append((stmt_line, _clean_parse_error(msg, stmt_text=stmt_text)))
    except ParseError as e:
        issues.append((stmt_line, _clean_parse_error(str(e), stmt_text=stmt_text)))

    if dialect == "tsql" and "`" in stmt_text:
        issues.append((stmt_line, "backtick identifiers are not valid in T-SQL — use [brackets]"))
    if dialect == "postgres" and "`" in stmt_text:
        issues.append((
            stmt_line,
            "backtick identifiers are not valid in PostgreSQL — use double quotes",
        ))
    return issues


def _sqlglot_issues(
    body: str, *, header_line: int, source_file: str,
) -> list[tuple[int, str]]:
    """Parse each SQL statement with sqlglot. Returns (line, message) pairs."""
    issues: list[tuple[int, str]] = []
    require_semi = _require_statement_semicolon()

    for stmt_text, offset in _split_sql_statements(body):
        end_line = _stmt_end_line(body, offset, stmt_text, header_line)
        if require_semi and not stmt_text.rstrip().endswith(";"):
            kind = _statement_kind(stmt_text)
            issues.append((end_line, f"missing semicolon (`;`) at end of {kind} statement"))
            continue
        issues.extend(_sqlglot_statement_issues(
            stmt_text, body, offset, header_line=header_line,
        ))
    return issues


def _sql_syntax_issues(
    body: str, *, header_line: int, source_file: str,
) -> list[tuple[int, str]]:
    """Heuristic + sqlglot validation for an Approved changeset SQL body."""
    issues: list[tuple[int, str]] = []
    issues.extend(_heuristic_body_issues(body, header_line=header_line))

    statements = _split_sql_statements(body)
    stripped_all = _strip_sql_comments(body)
    if stripped_all and not _SQL_KEYWORDS.search(stripped_all):
        issues.append((header_line + 1, "no recognizable SQL statement"))

    require_semi = _require_statement_semicolon()
    for stmt_text, offset in statements:
        end_line = _stmt_end_line(body, offset, stmt_text, header_line)
        stmt_blocking = False

        if require_semi and not stmt_text.rstrip().endswith(";"):
            kind = _statement_kind(stmt_text)
            issues.append((end_line, f"missing semicolon (`;`) at end of {kind} statement"))
            stmt_blocking = True

        for line, msg in _heuristic_statement_issues(
            stmt_text, body, offset, header_line=header_line,
        ):
            issues.append((line, msg))
            stmt_blocking = True

        if not stmt_blocking:
            issues.extend(_sqlglot_statement_issues(
                stmt_text, body, offset, header_line=header_line,
            ))

    return _consolidate_issues(issues)


def validate_merged_changelog(
    merged_content: str,
    *,
    validated_files: list[str],
    environment: str,
    file_parts: list[tuple[str, str]] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Run Liquibase pre-checks. Returns compact checks (issues only) + meta."""
    errors: list[DbFinding] = []
    warnings: list[DbFinding] = []
    meta: dict[str, Any] = {
        "validated_files": validated_files,
        "environment": environment,
        "approved_count": 0,
        "pending_skipped": 0,
        "changeset_ids": [],
        "approved_changesets": [],
        "pending_changesets": [],
    }

    if not validated_files:
        errors.append(DbFinding(file="", line=0, message="No SQL files found for this environment."))

    if not merged_content or not merged_content.strip():
        errors.append(DbFinding(file="", line=0, message="No SQL content in the selected file(s)."))
        return _compact_checks(errors, warnings, meta), meta

    for fname, content in (file_parts or []):
        for m in _INVALID_CHANGESET_HEADER.finditer(content):
            line = content[: m.start()].count("\n") + 1
            errors.append(DbFinding(
                file=fname,
                line=line,
                message="use `-- Changeset author:id` (space after `--`), not `--changeset`.",
            ))

    if not _LIQUIBASE_HEADER.search(merged_content):
        errors.append(DbFinding(file="", line=0, message="Missing `--liquibase formatted sql` header."))

    parts = file_parts or [("", merged_content)]
    all_parsed: list[ParsedChangeset] = []
    for fname, content in parts:
        all_parsed.extend(parse_changesets(content, source_file=fname))

    if not all_parsed and merged_content.strip():
        if not _INVALID_CHANGESET_HEADER.search(merged_content):
            errors.append(DbFinding(file="", line=0, message="No `-- Changeset author:id` headers found."))

    approved: list[ParsedChangeset] = []
    pending: list[ParsedChangeset] = []
    rejected: list[ParsedChangeset] = []

    for cs in all_parsed:
        if cs.is_approved:
            approved.append(cs)
        elif cs.is_pending:
            pending.append(cs)
        else:
            rejected.append(cs)

    meta["pending_skipped"] = len(pending)
    meta["approved_count"] = len(approved)
    meta["pending_changesets"] = [f"{cs.author}:{cs.changeset_id}" for cs in pending if cs.author]
    meta["approved_changesets"] = [
        {"id": f"{cs.author}:{cs.changeset_id}", "file": cs.source_file, "line": cs.line_number}
        for cs in approved
    ]

    for cs in rejected:
        label_str = ", ".join(cs.labels) if cs.labels else "no label"
        errors.append(DbFinding(
            file=cs.source_file,
            line=cs.line_number,
            changeset=f"{cs.author}:{cs.changeset_id}" if cs.author else cs.full_id,
            changeset_line=cs.line_number,
            message=(
                f"not Approved (labels: {label_str}) — add `labels:Approved` or `labels:Pending` to skip."
            ),
        ))

    if not approved and not rejected and pending:
        warnings.append(DbFinding(
            file="",
            line=0,
            message=f"{len(pending)} Pending changeset(s) only — nothing Approved to deploy.",
            severity="warn",
        ))

    for cs in approved:
        cs_key = f"{cs.author}:{cs.changeset_id}"
        if not cs.author or not cs.changeset_id:
            errors.append(DbFinding(
                file=cs.source_file,
                line=cs.line_number,
                changeset=cs_key,
                changeset_line=cs.line_number,
                message="invalid format — expected `author:id`.",
            ))
            continue

        if not cs.body.strip():
            errors.append(DbFinding(
                file=cs.source_file,
                line=cs.line_number,
                changeset=cs_key,
                changeset_line=cs.line_number,
                message="empty changeset (no SQL below header).",
            ))
            continue

        if _body_is_commented_only(cs.body):
            warnings.append(DbFinding(
                file=cs.source_file,
                line=cs.line_number,
                changeset=cs_key,
                changeset_line=cs.line_number,
                message="all SQL is commented out — no executable statements",
                severity="warn",
            ))
            continue

        for line_no, msg in _sql_syntax_issues(
            cs.body,
            header_line=cs.line_number,
            source_file=cs.source_file or "changelog",
        ):
            errors.append(DbFinding(
                file=cs.source_file or "changelog",
                line=line_no,
                message=msg,
                changeset=cs_key,
                changeset_line=cs.line_number,
            ))

        body_low = cs.body.lower()
        for needle, label in _DANGEROUS_SQL:
            if needle in body_low:
                warnings.append(DbFinding(
                    file=cs.source_file,
                    line=cs.line_number,
                    changeset=cs_key,
                    changeset_line=cs.line_number,
                    message=f"{label} — review before deploy",
                    severity="warn",
                ))

    seen: dict[str, ParsedChangeset] = {}
    for cs in approved:
        key = f"{cs.author}:{cs.changeset_id}"
        if key in seen:
            prev = seen[key]
            errors.append(DbFinding(
                file=cs.source_file,
                line=cs.line_number,
                changeset=key,
                changeset_line=cs.line_number,
                message=(
                    f"duplicate changeset ID — also declared at {prev.source_file}:{prev.line_number}"
                ),
            ))
        else:
            seen[key] = cs

    meta["changeset_ids"] = list(seen.keys())
    meta["file_summaries"] = _per_file_summaries(file_parts or [], all_parsed)
    meta["syntax_errors"] = [
        {
            "file": e.file,
            "line": e.line,
            "message": e.message,
            "changeset": e.changeset,
            "changeset_line": e.changeset_line,
            "severity": e.severity,
        }
        for e in errors
    ]
    meta["syntax_warnings"] = [
        {
            "file": w.file,
            "line": w.line,
            "message": w.message,
            "changeset": w.changeset,
            "changeset_line": w.changeset_line,
            "severity": w.severity,
        }
        for w in warnings
    ]
    return _compact_checks(errors, warnings, meta), meta


def _snippet_around_line(content: str, line: int, *, context: int = 2) -> str:
    """A few source lines around a reported line for AI / developer context."""
    if not content or line < 1:
        return ""
    lines = content.splitlines()
    idx = line - 1
    start = max(0, idx - context)
    end = min(len(lines), idx + context + 1)
    out: list[str] = []
    for n in range(start, end):
        mark = ">>" if n == idx else "  "
        out.append(f"{mark} {n + 1}: {lines[n]}")
    return "\n".join(out)


def build_ai_syntax_contexts(
    file_parts: list[tuple[str, str]],
    errors: list[DbFinding],
) -> list[dict[str, Any]]:
    """Build per-changeset context for AI syntax explanations."""
    content_by_file = dict(file_parts)
    by_cs: dict[str, list[DbFinding]] = {}
    for err in errors:
        if err.changeset and err.severity == "error":
            by_cs.setdefault(err.changeset, []).append(err)

    contexts: list[dict[str, Any]] = []
    for cs_key in sorted(by_cs, key=lambda k: (by_cs[k][0].file, by_cs[k][0].changeset_line)):
        items = by_cs[cs_key]
        anchor = items[0]
        content = content_by_file.get(anchor.file, "")
        findings: list[dict[str, Any]] = []
        for item in sorted(items, key=lambda x: x.line):
            findings.append({
                "line": item.line,
                "message": item.message,
                "snippet": _snippet_around_line(content, item.line),
            })
        contexts.append({
            "changeset": cs_key,
            "file": anchor.file,
            "header_line": anchor.changeset_line,
            "findings": findings,
        })
    return contexts


def apply_ai_hints(errors: list[DbFinding], hints: dict[str, str]) -> None:
    """Attach AI fix hints to findings per changeset (in-place)."""
    for err in errors:
        if err.changeset and err.changeset in hints:
            err.hint = hints[err.changeset]


def rebuild_db_checks_with_hints(
    meta: dict[str, Any],
    hints: dict[str, str],
) -> list[dict[str, str]]:
    """Re-format DB check detail after AI hints are applied."""
    errors = [
        DbFinding(**{**e, "hint": hints.get(e.get("changeset", ""), "")})
        for e in meta.get("syntax_errors", [])
    ]
    warnings = [DbFinding(**w) for w in meta.get("syntax_warnings", [])]
    return _compact_checks(errors, warnings, meta)


def _per_file_summaries(
    file_parts: list[tuple[str, str]],
    all_parsed: list[ParsedChangeset],
) -> list[str]:
    """One-line summary per SQL file actually validated."""
    lines: list[str] = []
    for fname, content in file_parts:
        parsed = [cs for cs in all_parsed if cs.source_file == fname]
        approved = [f"{cs.author}:{cs.changeset_id}" for cs in parsed if cs.is_approved]
        pending_n = sum(1 for cs in parsed if cs.is_pending)
        if approved:
            id_list = ", ".join(approved[:6])
            if len(approved) > 6:
                id_list += "…"
            line = f"{fname}: {len(approved)} Approved ({id_list})"
        elif parsed:
            line = f"{fname}: no Approved changesets"
        elif content.strip():
            line = f"{fname}: no `-- Changeset` headers found"
        else:
            line = f"{fname}: empty file"
        if pending_n:
            line += f", {pending_n} Pending skipped"
        lines.append(line)
    return lines


def _format_findings_grouped(findings: list[DbFinding]) -> str:
    """Group findings by changeset, then list line-level messages."""
    by_cs: dict[str, list[DbFinding]] = {}
    other: list[DbFinding] = []
    for f in findings:
        if f.changeset:
            by_cs.setdefault(f.changeset, []).append(f)
        else:
            other.append(f)

    lines: list[str] = []
    sorted_cs = sorted(
        by_cs.items(),
        key=lambda kv: (kv[1][0].file, kv[1][0].changeset_line or kv[1][0].line),
    )
    for cs_key, items in sorted_cs:
        anchor = items[0]
        hdr_line = anchor.changeset_line or anchor.line
        lines.append(f"  ▸ {cs_key}")
        lines.append(f"     {anchor.file}  ·  `-- Changeset` at line {hdr_line}")
        hints_shown: set[str] = set()
        for item in sorted(items, key=lambda x: x.line):
            lines.append(f"     L{item.line:<4}  {item.message}")
        hint = next((i.hint for i in items if i.hint), "")
        if hint and cs_key not in hints_shown:
            lines.append(f"     💡 {hint}")
            hints_shown.add(cs_key)
        lines.append("")

    for item in sorted(other, key=lambda x: (x.file, x.line)):
        loc = f"{item.file}:{item.line}" if item.file and item.line else (item.file or "changelog")
        lines.append(f"  {loc}: {item.message}")

    return "\n".join(lines).rstrip()


def _format_db_report(
    errors: list[DbFinding],
    warnings: list[DbFinding],
    file_summaries: list[str],
) -> str:
    """Multi-line DB validation report for the UI."""
    parts: list[str] = []
    if file_summaries:
        parts.append(" · ".join(file_summaries))

    if errors:
        if parts:
            parts.append("")
        parts.append("Errors (by changeset):")
        parts.append(_format_findings_grouped(errors))

    if warnings:
        if parts:
            parts.append("")
        parts.append("Warnings (by changeset):")
        parts.append(_format_findings_grouped(warnings))

    return "\n".join(parts).strip()


def _compact_checks(
    errors: list[DbFinding],
    warnings: list[DbFinding],
    meta: dict[str, Any],
) -> list[dict[str, str]]:
    """Build a single compact check row for the UI."""
    file_lines = meta.get("file_summaries") or []

    if errors:
        detail = _format_db_report(errors, [], file_lines)
        return [{"name": "DB validation", "status": "fail", "detail": detail, "findings": meta.get("syntax_errors") or []}]

    if not file_lines:
        return [{"name": "DB validation", "status": "fail", "detail": "No SQL files validated."}]

    if warnings:
        detail = _format_db_report([], warnings, file_lines)
        return [{"name": "DB validation", "status": "warn", "detail": detail}]

    approved = meta.get("changeset_ids") or []
    detail = " · ".join(file_lines)
    if not approved and meta.get("pending_skipped"):
        return [{"name": "DB validation", "status": "warn", "detail": detail + " — nothing Approved to deploy."}]

    return [{"name": "DB validation", "status": "pass", "detail": detail + " — OK."}]
