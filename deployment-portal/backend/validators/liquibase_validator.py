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
    r"^[^\S\n]*--\s+changeset\s+(\S+)(?:\s+(.*))?$",
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
    r"CALL|EXEC|EXECUTE|COMMENT|RENAME|SELECT|"
    r"ALTR|INSET|ISERT|SELEC|UPDTE|DELET|WHRE)\b",
    re.IGNORECASE,
)
_DDL_START = re.compile(
    r"^\s*(ALTER|CREATE|DROP|INSERT|UPDATE|DELETE|TRUNCATE|GRANT|REVOKE|MERGE|CALL|EXEC|"
    r"ALTR|INSET|ISERT|SELEC|UPDTE|DELET)\b",
    re.IGNORECASE,
)
# Statement openers for re-splitting bodies that omit `;` between statements.
# SELECT is intentionally excluded — Liquibase often uses INSERT…SELECT as one
# statement; splitting on SELECT creates false "missing semicolon" noise.
_STMT_OPENER = re.compile(
    r"^[^\S\n]*(ALTER|CREATE|INSERT|UPDATE|DELETE|DROP|TRUNCATE|MERGE|CALL|EXEC|"
    r"ALTR|INSET|ISERT|UPDTE|DELET)\b",
    re.IGNORECASE,
)
# Common Liquibase/SQL typos and malformed fragments.
_SQL_TYPO_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bALTR\b", re.IGNORECASE), "typo `ALTR` — did you mean `ALTER`?"),
    (re.compile(r"\bINSET\b", re.IGNORECASE), "typo `INSET` — did you mean `INSERT`?"),
    (re.compile(r"\bISERT\b", re.IGNORECASE), "typo `ISERT` — did you mean `INSERT`?"),
    (re.compile(r"\bSELEC\b", re.IGNORECASE), "typo `SELEC` — did you mean `SELECT`?"),
    (re.compile(r"\bUPDTE\b", re.IGNORECASE), "typo `UPDTE` — did you mean `UPDATE`?"),
    (re.compile(r"\bDELET\b", re.IGNORECASE), "typo `DELET` — did you mean `DELETE`?"),
    (re.compile(r"\bWHRE\b", re.IGNORECASE), "typo `WHRE` — did you mean `WHERE`?"),
    (re.compile(r"\bADD\s+COLUM\b", re.IGNORECASE), "typo `ADD COLUM` — did you mean `ADD COLUMN`?"),
    (re.compile(r"\bVARCHR\b", re.IGNORECASE), "typo `VARCHR` — did you mean `VARCHAR`?"),
    (re.compile(r"\bVARCHAR\s*\(\s*\)", re.IGNORECASE), "empty `VARCHAR()` length"),
    # (`Key` `Value`) — missing comma between column names
    (
        re.compile(r"`[^`]+`\s+`[^`]+`"),
        "missing comma between column names — use `ColA`, `ColB`",
    ),
    (re.compile(r",\s*;", re.IGNORECASE), "trailing comma before semicolon"),
    (re.compile(r";\s*;", re.IGNORECASE), "duplicate semicolon"),
    (re.compile(r"\bFROM\s*,", re.IGNORECASE), "missing table name after `FROM`"),
    (re.compile(r"\bWHERE\s*;", re.IGNORECASE), "empty `WHERE` clause"),
    (re.compile(r"\bSET\s*,", re.IGNORECASE), "missing column assignment after `SET`"),
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
    """Split a changeset body into executable statements with start offsets.

    Uses sqlparse when available, then further splits on statement-opener lines
    so a missing `;` between ALTER and INSERT still yields two statements.
    """
    chunks: list[tuple[str, int]] = []
    try:
        import sqlparse
    except ImportError:
        stmt = body.strip()
        if stmt and _is_executable_sql_fragment(stmt):
            chunks = [(stmt, 0)]
    else:
        search_from = 0
        for raw in sqlparse.split(body):
            stmt = raw.strip()
            if not stmt or not _is_executable_sql_fragment(stmt):
                continue
            idx = body.find(stmt, search_from)
            if idx < 0:
                idx = search_from
            chunks.append((stmt, idx))
            search_from = idx + len(stmt)

    if not chunks and body.strip() and _is_executable_sql_fragment(body):
        chunks = [(body.strip(), 0)]

    out: list[tuple[str, int]] = []
    for stmt, base_off in chunks:
        out.extend(_split_on_statement_openers(body, stmt, base_off))
    return out


def _split_on_statement_openers(
    body: str, stmt: str, base_off: int,
) -> list[tuple[str, int]]:
    """Split one sqlparse chunk when a new SQL keyword starts a later line."""
    rel = body.find(stmt, base_off)
    if rel < 0:
        rel = base_off
    region = body[rel: rel + len(stmt)]

    starts: list[int] = []
    pos = 0
    for line in region.splitlines(keepends=True):
        if not line.strip().startswith("--") and _STMT_OPENER.match(line):
            starts.append(pos)
        pos += len(line)

    if len(starts) <= 1:
        return [(stmt, rel)]

    pieces: list[tuple[str, int]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(region)
        local = region[start:end]
        piece = local.strip()
        if piece and _is_executable_sql_fragment(piece):
            pad = len(local) - len(local.lstrip())
            pieces.append((piece, rel + start + pad))
    return pieces or [(stmt, rel)]


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


def _changeset_display_id(cs: "ParsedChangeset") -> str:
    """Stable UI label for a changeset — never blank / bare `:`."""
    if cs.author and cs.changeset_id:
        return f"{cs.author}:{cs.changeset_id}"
    raw = (cs.full_id or "").strip()
    if raw and raw != ":":
        return raw
    return "(invalid author:id)"


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
    """Map a character offset inside a changeset body to a 1-based file line.

    ``header_line`` is the 1-based line of the ``-- Changeset`` header. Body text
    starts on the next line, so offset 0 → header_line + 1.
    """
    if offset < 0:
        offset = 0
    if offset > len(body):
        offset = len(body)
    return header_line + 1 + body[:offset].count("\n")


def _first_executable_offset(text: str) -> int:
    """Offset of the first non-comment, non-blank content in ``text``."""
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("--") and not stripped.startswith("/*"):
            # Prefer the first non-whitespace char on this line.
            lead = len(line) - len(line.lstrip()) if line.strip() else 0
            return pos + lead
        pos += len(line)
    return 0


def _stmt_start_line(body: str, offset: int, stmt_text: str, header_line: int) -> int:
    """File line of the first executable SQL in a statement (skips leading comments)."""
    local = _first_executable_offset(stmt_text)
    return _line_in_body(body, offset + local, header_line)


def _stmt_end_line(body: str, offset: int, stmt_text: str, header_line: int) -> int:
    """Line number where a statement ends (where `;` is expected)."""
    trimmed = stmt_text.rstrip()
    if not trimmed:
        return _stmt_start_line(body, offset, stmt_text, header_line)
    end_offset = offset + len(stmt_text) - len(stmt_text.lstrip()) + len(trimmed) - 1
    return _line_in_body(body, max(offset, end_offset), header_line)


_STMT_KIND = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|SELECT|ALTER|CREATE|DROP|TRUNCATE|MERGE|CALL|EXEC|"
    r"INSET|ISERT|ALTR|SELEC|UPDTE|DELET)\b",
    re.IGNORECASE,
)
_STMT_KIND_CANON = {
    "ALTR": "ALTER",
    "INSET": "INSERT",
    "ISERT": "INSERT",
    "SELEC": "SELECT",
    "UPDTE": "UPDATE",
    "DELET": "DELETE",
}


def _statement_kind(stmt_text: str) -> str:
    m = _STMT_KIND.search(_strip_sql_comments(stmt_text))
    if not m:
        return "SQL"
    raw = m.group(1).upper()
    return _STMT_KIND_CANON.get(raw, raw)


def _canonical_message(msg: str) -> str:
    low = msg.lower()
    if low.startswith("typo `"):
        return msg
    if "semicolon" in low:
        return msg  # keep statement kind (UPDATE, INSERT, …)
    if "missing value" in low and "and" in low:
        return "missing value before `AND` (e.g. `column = AND`)"
    return msg


def _deterministic_hint_from_message(message: str, line: int = 0) -> str:
    """Build a fix hint from deterministic typo messages (preferred over AI)."""
    m = re.match(r"typo `([^`]+)` — did you mean `([^`]+)`\?", message.strip())
    if m:
        return f"Change `{m.group(1)}` to `{m.group(2)}`."
    low = message.lower()
    if "missing comma between column" in low:
        return "Change (`ColA` `ColB`) to (`ColA`, `ColB`)."
    if "missing semicolon" in low:
        return "End the statement with `;` before the next SQL keyword."
    return ""


def _finding_topic(message: str) -> str:
    """Coarse topic so AI polish cannot overwrite a typo with a comma finding."""
    low = (message or "").lower()
    if any(k in low for k in ("typo", "misspelled", "keyword", "did you mean", "incorrect keyword")):
        return "typo"
    if "semicolon" in low:
        return "semicolon"
    if "comma" in low:
        return "comma"
    if "quote" in low:
        return "quote"
    if "parenthes" in low:
        return "paren"
    return "other"


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
    if not _strip_sql_comments(stmt_text):
        return issues

    # Search the original statement text so offsets map to real file lines
    # (comment lines are kept for numbering; matches inside `--` comments are skipped).
    for pattern, msg in _SQL_TYPO_PATTERNS:
        for m in pattern.finditer(stmt_text):
            if _offset_in_line_comment(stmt_text, m.start()):
                continue
            issues.append((_line_in_body(body, offset + m.start(), header_line), msg))

    return issues


def _offset_in_line_comment(text: str, offset: int) -> bool:
    """True when ``offset`` sits on a `-- ...` comment portion of its line."""
    line_start = text.rfind("\n", 0, offset) + 1
    prefix = text[line_start:offset]
    return "--" in prefix


def _scan_body_typos(body: str, *, header_line: int) -> list[tuple[int, str]]:
    """Scan the full changeset body for typos (independent of statement splitting)."""
    issues: list[tuple[int, str]] = []
    if not body or not body.strip():
        return issues
    for pattern, msg in _SQL_TYPO_PATTERNS:
        for m in pattern.finditer(body):
            if _offset_in_line_comment(body, m.start()):
                continue
            issues.append((_line_in_body(body, m.start(), header_line), msg))
    return issues


def _has_recognizable_sql(text: str) -> bool:
    """True when text looks like SQL (valid keywords only — typos handled by Ollama)."""
    stripped = _strip_sql_comments(text)
    if not stripped:
        return False
    return bool(_SQL_KEYWORDS.search(stripped))


def _needs_ai_typo_review(message: str) -> bool:
    """Parser messages that may be clarified by Ollama keyword-typo detection."""
    low = (message or "").lower()
    if low.startswith("typo `"):
        return False
    triggers = (
        "invalid sql syntax",
        "no recognizable sql statement",
        "empty or unparseable statement",
        "invalid sql near",
        "sql token error",
        "missing value in expression",
    )
    return any(t in low for t in triggers)


def _extract_changeset_snippet(content: str, header_line: int, *, max_lines: int = 40) -> str:
    """Return numbered executable SQL lines for one changeset (for Ollama typo review)."""
    if not content or header_line < 1:
        return ""
    lines = content.splitlines()
    start = header_line  # skip `-- Changeset` header line itself
    end = start
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped.lower().startswith("-- changeset"):
            break
        end += 1
    end = min(end, start + max_lines)
    # Trim trailing blank lines so the snippet ends on real SQL — a trailing blank
    # line otherwise makes the reviewer hallucinate an "empty statement" error.
    while end > start and not lines[end - 1].strip():
        end -= 1
    if start >= len(lines) or end <= start:
        return ""
    return "\n".join(f"{start + i + 1}: {lines[start + i]}" for i in range(end - start))


def _heuristic_sql_issues(
    body: str, *, header_line: int, source_file: str,
) -> list[tuple[int, str]]:
    """Legacy wrapper — body + per-statement heuristics."""
    issues = _heuristic_body_issues(body, header_line=header_line)
    stripped = _strip_sql_comments(body)
    if stripped and not _has_recognizable_sql(body):
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
    stmt_line = _stmt_start_line(body, offset, stmt_text, header_line)

    parse_text = stmt_text.rstrip().rstrip(";").strip()
    # Drop leading comments so sqlglot doesn't see author/header noise as SQL.
    parse_text = _strip_sql_comments(parse_text).strip()
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
    # Always scan the full body for typos so ALTR/INSET/etc. are found even when
    # statement splitting or sqlglot is unavailable.
    issues.extend(_scan_body_typos(body, header_line=header_line))

    statements = _split_sql_statements(body)
    stripped_all = _strip_sql_comments(body)
    # Only report "no recognizable SQL" when there truly are no statements — otherwise
    # a single mistyped leading keyword (e.g. SELEC) would add this misleading noise on
    # top of the specific per-statement error.
    if stripped_all and not statements and not _has_recognizable_sql(body):
        issues.append((
            _line_in_body(body, _first_executable_offset(body), header_line),
            "no recognizable SQL statement",
        ))

    require_semi = _require_statement_semicolon()
    typo_lines = {line for line, _ in issues}
    for stmt_text, offset in statements:
        end_line = _stmt_end_line(body, offset, stmt_text, header_line)
        start_line = _stmt_start_line(body, offset, stmt_text, header_line)
        stmt_blocking = False

        if require_semi and not stmt_text.rstrip().endswith(";"):
            kind = _statement_kind(stmt_text)
            issues.append((end_line, f"missing semicolon (`;`) at end of {kind} statement"))
            stmt_blocking = True

        # Skip per-statement typo re-scan — already covered by _scan_body_typos.
        # Still run sqlglot when no concrete typo/semicolon issue on this statement.
        has_typo_here = any(
            start_line <= ln <= end_line for ln in typo_lines
        )
        if stmt_blocking or has_typo_here:
            continue
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
    defer_sql_syntax: bool = False,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Run Liquibase pre-checks. Returns compact checks (issues only) + meta.

    When ``defer_sql_syntax`` is True, executable SQL syntax is left for Ollama
    (see ``review_db_sql_syntax`` in validation) — only Liquibase structure is checked here.
    """
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
    meta["pending_changesets"] = [_changeset_display_id(cs) for cs in pending]
    meta["approved_changesets"] = [
        {"id": _changeset_display_id(cs), "file": cs.source_file, "line": cs.line_number}
        for cs in approved
    ]

    for cs in rejected:
        label_str = ", ".join(cs.labels) if cs.labels else "no label"
        errors.append(DbFinding(
            file=cs.source_file,
            line=cs.line_number,
            changeset=_changeset_display_id(cs),
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
        cs_key = _changeset_display_id(cs)
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

        if not defer_sql_syntax:
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
        key = _changeset_display_id(cs)
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


def build_ai_sql_review_contexts(
    file_parts: list[tuple[str, str]],
    meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build numbered SQL snippets for every Approved changeset (Ollama syntax review)."""
    content_by_file = dict(file_parts)
    contexts: list[dict[str, Any]] = []
    for entry in meta.get("approved_changesets") or []:
        cs_key = entry.get("id") or ""
        fname = entry.get("file") or ""
        header_line = int(entry.get("line") or 0)
        if not cs_key or not fname or not header_line:
            continue
        # Malformed headers already get an "invalid format" error — skip AI syntax review.
        if cs_key == "(invalid author:id)":
            continue
        content = content_by_file.get(fname, "")
        parsed_here = [
            p for p in parse_changesets(content, source_file=fname)
            if _changeset_display_id(p) == cs_key
        ]
        if parsed_here and _body_is_commented_only(parsed_here[0].body):
            continue
        snippet = _extract_changeset_snippet(content, header_line, max_lines=120)
        if not snippet.strip():
            continue
        contexts.append({
            "changeset": cs_key,
            "file": fname,
            "header_line": header_line,
            "sql_snippet": snippet,
        })
    return contexts


def _syntax_errors_to_meta(errors: list[DbFinding]) -> list[dict[str, Any]]:
    """Serialize error findings for meta, preserving hints so they survive re-writes."""
    return [
        {
            "file": e.file,
            "line": e.line,
            "message": e.message,
            "changeset": e.changeset,
            "changeset_line": e.changeset_line,
            "severity": e.severity,
            **({"hint": e.hint} if e.hint else {}),
        }
        for e in errors if e.severity == "error"
    ]


def _fallback_sql_hint(message: str, line: int, changeset: str) -> str:
    """Best-effort hint for deterministic findings (Ollama unavailable / out of budget)."""
    det = _deterministic_hint_from_message(message, line)
    if det:
        return det
    if changeset and changeset != "(invalid author:id)":
        return f"Check the SQL in changeset {changeset}."
    return "Check the SQL in this changeset."


def append_deterministic_sql_syntax_for_changesets(
    file_parts: list[tuple[str, str]],
    errors: list[DbFinding],
    warnings: list[DbFinding],
    meta: dict[str, Any],
    changeset_keys: set[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Run sqlglot/heuristic checks only for specific changesets (Ollama fallback)."""
    if not changeset_keys:
        return _compact_checks(errors, warnings, meta), meta

    all_parsed: list[ParsedChangeset] = []
    for fname, content in file_parts:
        all_parsed.extend(parse_changesets(content, source_file=fname))

    for cs in all_parsed:
        cs_key = _changeset_display_id(cs)
        if cs_key not in changeset_keys:
            continue
        if not cs.is_approved or _body_is_commented_only(cs.body) or not cs.body.strip():
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
                hint=_fallback_sql_hint(msg, line_no, cs_key),
            ))

    meta["syntax_errors"] = _syntax_errors_to_meta(errors)
    return _compact_checks(errors, warnings, meta), meta


def changeset_has_syntax_issues(file_parts: list[tuple[str, str]], cs_key: str) -> bool:
    """Quick sqlglot check — used to re-prompt Ollama when it missed an error."""
    for fname, content in file_parts:
        for cs in parse_changesets(content, source_file=fname):
            if _changeset_display_id(cs) != cs_key:
                continue
            if _body_is_commented_only(cs.body):
                return False
            return bool(_sql_syntax_issues(
                cs.body, header_line=cs.line_number, source_file=cs.source_file or "changelog",
            ))
    return False


def append_deterministic_sql_syntax(
    file_parts: list[tuple[str, str]],
    errors: list[DbFinding],
    warnings: list[DbFinding],
    meta: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Fallback: run sqlglot/heuristic syntax checks when Ollama is unavailable."""
    all_parsed: list[ParsedChangeset] = []
    for fname, content in file_parts:
        all_parsed.extend(parse_changesets(content, source_file=fname))

    for cs in all_parsed:
        if not cs.is_approved or _body_is_commented_only(cs.body) or not cs.body.strip():
            continue
        cs_key = _changeset_display_id(cs)
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
                hint=_fallback_sql_hint(msg, line_no, cs_key),
            ))

    meta["syntax_errors"] = _syntax_errors_to_meta(errors)
    return _compact_checks(errors, warnings, meta), meta


def apply_ai_sql_review_findings(
    errors: list[DbFinding],
    warnings: list[DbFinding],
    meta: dict[str, Any],
    ai_findings: dict[str, list[dict[str, Any]]],
    reviewed_changesets: set[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Legacy helper — prefer ``merge_ai_sql_hints_into_findings``.

    Kept for clean-path rebuilds when there are no SQL findings to merge.
    """
    if ai_findings:
        return merge_ai_sql_hints_into_findings(errors, warnings, meta, ai_findings)
    meta["syntax_errors"] = _syntax_errors_to_meta(errors)
    return _compact_checks(errors, warnings, meta), meta


def merge_ai_sql_hints_into_findings(
    errors: list[DbFinding],
    warnings: list[DbFinding],
    meta: dict[str, Any],
    ai_findings: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Polish deterministic findings with AI message/hint — never drop or replace the list.

    Matches AI items by ``(changeset, line, topic)`` so a comma hint on L17 cannot
    overwrite an INSET typo on the same line. Duplicate identical findings are removed.
    """
    by_cs_line: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for cs_key, items in (ai_findings or {}).items():
        for item in items or []:
            if not isinstance(item, dict):
                continue
            line = int(item.get("line") or 0)
            if cs_key and line:
                by_cs_line.setdefault((str(cs_key), line), []).append(item)

    used: set[tuple[str, int, int]] = set()
    refined: list[DbFinding] = []
    for err in errors:
        msg = err.message
        hint = (err.hint or _deterministic_hint_from_message(err.message, err.line) or "").strip()
        det_topic = _finding_topic(err.message)
        ai = None
        if err.changeset and err.line:
            candidates = by_cs_line.get((err.changeset, err.line), [])
            for idx, item in enumerate(candidates):
                key = (err.changeset, err.line, idx)
                if key in used:
                    continue
                ai_topic = _finding_topic(str(item.get("message") or ""))
                # Same topic, or AI topic unknown — allow polish for that one finding only.
                if ai_topic == det_topic or (ai_topic == "other" and det_topic != "other"):
                    if ai_topic == "other" and det_topic != "other":
                        # Don't let vague AI text replace a concrete deterministic topic.
                        continue
                    ai = item
                    used.add(key)
                    break

        if ai:
            ai_msg = str(ai.get("message") or "").strip()
            ai_hint = str(ai.get("hint") or "").strip()
            ai_topic = _finding_topic(ai_msg)
            # Only replace text when topics agree — keeps INSET typo distinct from comma.
            # Keep deterministic typo wording when it already has wrong→right form.
            if (
                ai_msg
                and len(ai_msg) >= 12
                and ai_topic == det_topic
                and not (det_topic == "typo" and re.search(r"typo `|did you mean", err.message, re.I))
            ):
                msg = ai_msg
            # Prefer deterministic hints for typos/commas/semicolons — AI often invents
            # wrong column names ("comma after Value") or awkward punctuation.
            if det_topic in ("typo", "comma", "semicolon"):
                det_hint = _deterministic_hint_from_message(err.message, err.line)
                hint = det_hint or hint
            elif ai_hint and not _ai_hint_is_generic(ai_hint):
                hint = ai_hint
            elif not hint:
                hint = _deterministic_hint_from_message(msg, err.line)

        hint = _normalize_hint_punctuation(hint)
        refined.append(DbFinding(
            file=err.file,
            line=err.line,
            message=msg,
            changeset=err.changeset,
            changeset_line=err.changeset_line,
            severity=err.severity,
            hint=hint,
        ))

    refined = _dedupe_findings(refined)
    meta["syntax_errors"] = _syntax_errors_to_meta(refined)
    return _compact_checks(refined, warnings, meta), meta


def _normalize_hint_punctuation(hint: str) -> str:
    """Avoid ``?.`` / ``..`` when formatters append a trailing period."""
    h = (hint or "").strip()
    if not h:
        return ""
    h = re.sub(r"[?.!]+$", "", h).rstrip()
    if h and h[-1] not in ".!?":
        h += "."
    return h


def _dedupe_findings(findings: list[DbFinding]) -> list[DbFinding]:
    """Drop duplicate (changeset, line, topic) rows after AI polish."""
    seen: set[tuple[str, int, str]] = set()
    out: list[DbFinding] = []
    for f in findings:
        key = (f.changeset or "", f.line, _finding_topic(f.message))
        if key in seen and key[2] != "other":
            continue
        # Also collapse identical messages on the same line.
        msg_key = (f.changeset or "", f.line, _canonical_message(f.message).lower())
        if msg_key in seen:
            continue
        seen.add(key)
        seen.add(msg_key)
        out.append(f)
    return out


def _ai_hint_is_generic(hint: str) -> bool:
    """Reject vague AI hints that add no remediation value."""
    low = (hint or "").strip().lower()
    if not low:
        return True
    if low in {"fix the sql.", "fix syntax.", "check the sql.", "invalid sql."}:
        return True
    if "add a comma after" in low:
        # Misleading — comma goes *between* column names, not after the last one.
        return True
    if len(low) < 16:
        return True
    return False


def build_ai_typo_contexts(
    file_parts: list[tuple[str, str]],
    errors: list[DbFinding],
) -> list[dict[str, Any]]:
    """Changesets whose parser failed generically — send SQL to Ollama for keyword typos."""
    content_by_file = dict(file_parts)
    by_cs: dict[str, list[DbFinding]] = {}
    for err in errors:
        if err.changeset and err.severity == "error" and _needs_ai_typo_review(err.message):
            by_cs.setdefault(err.changeset, []).append(err)

    contexts: list[dict[str, Any]] = []
    for cs_key in sorted(by_cs, key=lambda k: (by_cs[k][0].file, by_cs[k][0].changeset_line)):
        items = by_cs[cs_key]
        anchor = items[0]
        content = content_by_file.get(anchor.file, "")
        contexts.append({
            "changeset": cs_key,
            "file": anchor.file,
            "header_line": anchor.changeset_line,
            "sql_snippet": _extract_changeset_snippet(content, anchor.changeset_line or anchor.line),
            "parser_errors": [
                {"line": i.line, "message": i.message} for i in sorted(items, key=lambda x: x.line)
            ],
        })
    return contexts


def apply_ai_typo_findings(
    errors: list[DbFinding],
    warnings: list[DbFinding],
    meta: dict[str, Any],
    typo_findings: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Replace generic parser errors with Ollama-detected SQL keyword typos."""
    if not typo_findings:
        return _compact_checks(errors, warnings, meta), meta

    refined: list[DbFinding] = []
    for err in errors:
        if err.changeset in typo_findings and _needs_ai_typo_review(err.message):
            continue
        refined.append(err)

    for cs_key, finding in typo_findings.items():
        if not finding:
            continue
        wrong = str(finding.get("wrong") or "").strip()
        correct = str(finding.get("correct") or "").strip()
        line = int(finding.get("line") or 0)
        msg = str(finding.get("message") or "").strip()
        if not msg and wrong and correct:
            msg = f"typo `{wrong}` — did you mean `{correct}`?"
        if not msg:
            continue
        hint = str(finding.get("hint") or "").strip()
        if not hint:
            hint = _deterministic_hint_from_message(msg, line)
        anchor = next((e for e in errors if e.changeset == cs_key), None)
        refined.append(DbFinding(
            file=(anchor.file if anchor else finding.get("file") or ""),
            line=line or (anchor.line if anchor else 0),
            message=msg,
            changeset=cs_key,
            changeset_line=(anchor.changeset_line if anchor else int(finding.get("header_line") or 0)),
            hint=hint,
        ))

    meta["syntax_errors"] = [
        {
            "file": e.file,
            "line": e.line,
            "message": e.message,
            "changeset": e.changeset,
            "changeset_line": e.changeset_line,
            "severity": e.severity,
            **({"hint": e.hint} if e.hint else {}),
        }
        for e in refined if e.severity == "error"
    ]
    return _compact_checks(refined, warnings, meta), meta


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
    raw_errors = meta.get("syntax_errors", [])
    merged_hints = dict(hints)
    errors = []
    for e in raw_errors:
        hint = str(e.get("hint") or "").strip()
        cs = e.get("changeset", "")
        if not hint and cs:
            hint = merged_hints.get(cs, "")
        if not hint:
            det = _deterministic_hint_from_message(
                str(e.get("message", "")),
                int(e.get("line") or 0),
            )
            if det:
                hint = det
        errors.append(DbFinding(**{**e, "hint": hint}))
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
        approved = [_changeset_display_id(cs) for cs in parsed if cs.is_approved]
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
        for item in sorted(items, key=lambda x: (x.line, x.message)):
            loc = f"L{item.line}" if item.line else "L?"
            msg_line = f"     {loc}    {item.message}"
            if item.hint:
                hint = re.sub(
                    r"\s+(?:on|near)\s+line\s+\d+\.?\s*$",
                    "",
                    item.hint,
                    flags=re.I,
                )
                hint = _normalize_hint_punctuation(hint)
                if hint:
                    msg_line += f"  💡 {hint}"
            else:
                det = _normalize_hint_punctuation(
                    _deterministic_hint_from_message(item.message, item.line)
                )
                if det:
                    msg_line += f"  💡 {det}"
            lines.append(msg_line)
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
