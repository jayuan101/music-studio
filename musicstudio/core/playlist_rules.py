"""Smart-playlist rules: a small JSON rule set compiled to parameterized SQL.

A smart playlist is stored as a dict, never as SQL::

    {"match": "all" | "any",
     "rules": [{"field": "genre", "op": "contains", "value": "jazz"}, ...],
     "order_by": "-added_at",
     "limit": 100}

Rules come from a dialog today but are persisted to disk and read back, so
they are treated as untrusted: every field, operator and sort column is
checked against an allowlist, and every value is bound as a parameter.
Nothing a user typed ever becomes part of the SQL text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

TEXT = "text"
NUMBER = "number"
BOOLEAN = "boolean"
TIME = "time"


@dataclass(frozen=True)
class RuleField:
    key: str
    label: str
    kind: str
    #: SQL expression over the tracks table. Fixed text from this module only.
    expression: str
    #: Shown next to number inputs ("kbps", "s").
    unit: str = ""


FIELDS: dict[str, RuleField] = {
    f.key: f
    for f in (
        RuleField("title", "Title", TEXT, "title"),
        RuleField("artist", "Artist", TEXT, "artist"),
        RuleField("album", "Album", TEXT, "album"),
        RuleField("albumartist", "Album artist", TEXT, "albumartist"),
        RuleField("genre", "Genre", TEXT, "genre"),
        RuleField("codec", "Codec", TEXT, "codec"),
        RuleField("year", "Year", NUMBER, "CAST(substr(date, 1, 4) AS INTEGER)"),
        RuleField("duration", "Length", NUMBER, "duration", "s"),
        RuleField("bitrate", "Bitrate", NUMBER, "(bitrate / 1000)", "kbps"),
        RuleField("sample_rate", "Sample rate", NUMBER, "sample_rate", "Hz"),
        RuleField("bit_depth", "Bit depth", NUMBER, "bit_depth", "bit"),
        RuleField("is_lossless", "Lossless", BOOLEAN, "is_lossless"),
        RuleField("has_artwork", "Has artwork", BOOLEAN, "has_artwork"),
        RuleField("has_replaygain", "Has ReplayGain", BOOLEAN, "(rg_track_gain IS NOT NULL)"),
        RuleField("has_album", "Has album", BOOLEAN, "(album <> '')"),
        RuleField("has_year", "Has year", BOOLEAN, "(date <> '')"),
        RuleField("suspect_lossless", "Looks transcoded", BOOLEAN, "(authenticity = 'suspect')"),
        RuleField("lossless_unchecked", "Lossless, not checked yet", BOOLEAN,
                  "(is_lossless = 1 AND authenticity IS NULL)"),
        RuleField("added_at", "Date added", TIME, "added_at"),
    )
}

#: Operators allowed per field kind, with their labels for the dialog.
OPERATORS: dict[str, dict[str, str]] = {
    TEXT: {
        "contains": "contains",
        "not_contains": "does not contain",
        "is": "is",
        "is_not": "is not",
        "starts_with": "starts with",
    },
    NUMBER: {
        "lt": "is less than",
        "gt": "is greater than",
        "eq": "is",
        "between": "is between",
    },
    BOOLEAN: {"is_true": "is yes", "is_false": "is no"},
    TIME: {"in_last_days": "in the last (days)", "not_in_last_days": "not in the last (days)"},
}

#: Sort keys a smart playlist may use. "random" reshuffles on every view.
ORDER_FIELDS: dict[str, str] = {
    "albumartist": "albumartist, album, disc_number, track_number, title",
    "artist": "artist",
    "title": "title",
    "album": "album",
    "date": "date",
    "genre": "genre",
    "duration": "duration",
    "bitrate": "bitrate",
    "added_at": "added_at",
    "random": "RANDOM()",
}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Expected a number, got {value!r}") from None


def _compile_rule(rule: dict, now: float) -> tuple[str, list]:
    field = FIELDS.get(rule.get("field", ""))
    if field is None:
        raise ValueError(f"Unknown rule field {rule.get('field')!r}")
    op = rule.get("op", "")
    if op not in OPERATORS[field.kind]:
        raise ValueError(f"{op!r} is not a valid test for {field.label}")
    value = rule.get("value")
    expr = field.expression

    if field.kind == TEXT:
        text = str(value or "")
        if op == "contains":
            return f"{expr} LIKE ? ESCAPE '\\'", [f"%{_like_escape(text)}%"]
        if op == "not_contains":
            return f"{expr} NOT LIKE ? ESCAPE '\\'", [f"%{_like_escape(text)}%"]
        if op == "starts_with":
            return f"{expr} LIKE ? ESCAPE '\\'", [f"{_like_escape(text)}%"]
        if op == "is":
            return f"{expr} = ? COLLATE NOCASE", [text]
        return f"{expr} <> ? COLLATE NOCASE", [text]

    if field.kind == NUMBER:
        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise ValueError(f"{field.label} 'between' needs two numbers")
            low, high = sorted((_number(value[0]), _number(value[1])))
            return f"{expr} BETWEEN ? AND ?", [low, high]
        symbol = {"lt": "<", "gt": ">", "eq": "="}[op]
        return f"{expr} {symbol} ?", [_number(value)]

    if field.kind == BOOLEAN:
        return f"{expr} = ?", [1 if op == "is_true" else 0]

    # TIME
    cutoff = now - _number(value) * 86400
    symbol = ">=" if op == "in_last_days" else "<"
    return f"{expr} {symbol} ?", [cutoff]


def compile_rules(rules: dict, *, now: float | None = None) -> tuple[str, list]:
    """``rules`` -> (``WHERE ... ORDER BY ... LIMIT ...``, params).

    Raises ValueError for anything not on the allowlists.
    """
    now = time.time() if now is None else now
    match = rules.get("match", "all")
    if match not in ("all", "any"):
        raise ValueError(f"match must be 'all' or 'any', not {match!r}")

    clauses: list[str] = []
    params: list = []
    for rule in rules.get("rules") or []:
        sql, values = _compile_rule(rule, now)
        clauses.append(f"({sql})")
        params.extend(values)

    joiner = " AND " if match == "all" else " OR "
    where = f"WHERE {joiner.join(clauses)}" if clauses else ""

    order_key = rules.get("order_by") or "albumartist"
    descending = order_key.startswith("-")
    column = order_key[1:] if descending else order_key
    if column not in ORDER_FIELDS:
        raise ValueError(f"Cannot sort by {column!r}")
    order = ORDER_FIELDS[column]
    if descending and column != "random":
        order = ", ".join(f"{part} DESC" for part in order.split(", "))
    sql = f"{where} ORDER BY {order}"

    limit = rules.get("limit")
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return sql, params


def _fmt(value) -> str:
    """1990.0 -> "1990": the dialog's number boxes hand back floats."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def describe_rules(rules: dict) -> str:
    """One readable line, e.g. "genre contains 'jazz' and lossless is yes"."""
    parts: list[str] = []
    for rule in rules.get("rules") or []:
        field = FIELDS.get(rule.get("field", ""))
        if field is None:
            continue
        op_label = OPERATORS[field.kind].get(rule.get("op", ""), rule.get("op", ""))
        value = rule.get("value")
        if field.kind == BOOLEAN:
            parts.append(f"{field.label.lower()} {op_label}")
        elif isinstance(value, (list, tuple)):
            parts.append(
                f"{field.label.lower()} {op_label} {_fmt(value[0])} and {_fmt(value[1])}"
            )
        elif field.kind == TIME:
            negation = "not " if rule.get("op") == "not_in_last_days" else ""
            parts.append(f"{field.label.lower()} {negation}in the last {_fmt(value)} days")
        elif field.kind == TEXT:
            parts.append(f"{field.label.lower()} {op_label} '{value}'")
        else:
            unit = f" {field.unit}" if field.unit else ""
            parts.append(f"{field.label.lower()} {op_label} {_fmt(value)}{unit}")
    joiner = " and " if rules.get("match", "all") == "all" else " or "
    text = joiner.join(parts) or "every song"
    if rules.get("limit"):
        text += f" (first {rules['limit']})"
    return text
