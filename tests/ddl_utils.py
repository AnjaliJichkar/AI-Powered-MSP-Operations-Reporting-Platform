"""Helpers for comparing rendered DDL, used by the migration drift test."""

from __future__ import annotations

import re

CREATE_TABLE_RE = re.compile(r"CREATE TABLE (\w+) \((.*?)\n\)", re.DOTALL | re.IGNORECASE)
CREATE_INDEX_RE = re.compile(r"CREATE (?:UNIQUE )?INDEX .*?;", re.DOTALL | re.IGNORECASE)
ADD_COLUMN_RE = re.compile(r"ALTER TABLE (\w+) ADD COLUMN (.+?);", re.DOTALL | re.IGNORECASE)
ADD_CONSTRAINT_RE = re.compile(
    r"ALTER TABLE (\w+) ADD (CONSTRAINT .+?);", re.DOTALL | re.IGNORECASE
)
DROP_CONSTRAINT_RE = re.compile(r"ALTER TABLE (\w+) DROP CONSTRAINT (\w+)", re.IGNORECASE)
ALTER_NULLABILITY_RE = re.compile(
    r"ALTER TABLE (\w+) ALTER COLUMN (\w+) (SET|DROP) NOT NULL", re.IGNORECASE
)
# A constraint added NOT VALID is the same constraint once the rows comply;
# the models have no way to say NOT VALID, so the suffix is not drift.
NOT_VALID_SUFFIX_RE = re.compile(r"\s+NOT VALID$", re.IGNORECASE)


def _split_top_level(body: str) -> list[str]:
    """Split a CREATE TABLE body on commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    in_string = False
    current: list[str] = []
    for char in body:
        if char == "'":
            in_string = not in_string
        if not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append("".join(current))
                current = []
                continue
        current.append(char)
    parts.append("".join(current))
    return [normalise(p) for p in parts if p.strip()]


def normalise(text: str) -> str:
    """Collapse whitespace so formatting differences do not register as drift."""
    return re.sub(r"\s+", " ", text).strip().rstrip(",")


#: Alembic's own bookkeeping table; not part of the application schema.
IGNORED_TABLES = frozenset({"alembic_version"})


def parse_tables(sql: str) -> dict[str, set[str]]:
    """Map table name -> the set of its column and constraint definitions.

    Later migrations alter tables rather than creating them, so the ALTERs are
    replayed onto the CREATEs. Without that, comparing a multi-migration script
    against the models would report every added column as missing.
    """
    tables: dict[str, set[str]] = {}
    for match in CREATE_TABLE_RE.finditer(sql):
        name = match.group(1).lower()
        if name in IGNORED_TABLES:
            continue
        tables[name] = set(_split_top_level(match.group(2)))

    # The ALTERs are replayed in the order the script runs them. Order matters
    # as soon as a migration drops a constraint and re-adds it under the same
    # name with a new definition: replaying every ADD before every DROP would
    # erase the replacement along with the original.
    alterations = sorted(
        (
            (match.start(), kind, match)
            for kind, pattern in (
                ("add", ADD_COLUMN_RE),
                ("add", ADD_CONSTRAINT_RE),
                ("nullability", ALTER_NULLABILITY_RE),
                ("drop", DROP_CONSTRAINT_RE),
            )
            for match in pattern.finditer(sql)
        ),
        key=lambda entry: entry[0],
    )

    for _, kind, match in alterations:
        name = match.group(1).lower()
        if name in IGNORED_TABLES or name not in tables:
            continue

        if kind == "add":
            tables[name].add(NOT_VALID_SUFFIX_RE.sub("", normalise(match.group(2))))

        elif kind == "nullability":
            # SET/DROP NOT NULL rewrites the column definition an earlier
            # CREATE or ADD COLUMN produced.
            column = match.group(2).lower()
            wants_not_null = match.group(3).upper() == "SET"
            for definition in list(tables[name]):
                if definition.lower().split(" ", 1)[0] != column:
                    continue
                tables[name].discard(definition)
                stripped = re.sub(r"\s+NOT NULL\b", "", definition, flags=re.IGNORECASE)
                tables[name].add(f"{stripped} NOT NULL" if wants_not_null else stripped)

        else:
            # A later migration can drop what an earlier one created. Without
            # replaying the drop, a removed constraint looks like drift forever.
            constraint = match.group(2)
            tables[name] = {
                definition
                for definition in tables[name]
                if not definition.upper().startswith(f"CONSTRAINT {constraint.upper()} ")
            }

    return tables


def parse_indexes(sql: str) -> set[str]:
    """The set of CREATE INDEX statements, normalised."""
    return {normalise(stmt).rstrip(";").strip() for stmt in CREATE_INDEX_RE.findall(sql)}
