"""结构检查必须拒绝不确定写入语义，fake 不替代真实 catalog 验收。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

import app.infrastructure.database_schema as schema
from app.infrastructure.database_schema import SchemaError, managed_tables


class Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    async def execute(self, statement, params):
        self.calls += 1
        return Result(self.rows)


@pytest.mark.asyncio
async def test_view_cannot_impersonate_managed_table():
    connection = Connection([{"relname": "sessions", "relkind": "v"}])
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await managed_tables(connection, asyncio.get_running_loop().time() + 1)


@pytest.mark.asyncio
async def test_expired_deadline_does_not_query():
    connection = Connection([])
    with pytest.raises(SchemaError, match="timeout"):
        await managed_tables(connection, asyncio.get_running_loop().time() - 1)
    assert connection.calls == 0


def sequence():
    return {
        "bigint": True,
        "default_matches": True,
        "seqstart": 1,
        "seqincrement": 1,
        "seqmin": 1,
        "seqmax": 9223372036854775807,
        "seqcache": 1,
        "seqcycle": False,
        "nspname": "public",
        "relname": "messages_id_seq",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("bigint", False),
        ("default_matches", False),
        ("seqincrement", 2),
        ("seqmin", 0),
        ("seqmax", 100),
        ("seqcache", 2),
        ("seqcycle", True),
        ("seqstart", 2),
        ("nspname", "other"),
    ],
)
async def test_sequence_semantics_mismatch_rejected(monkeypatch, field, value):
    row = sequence()
    row[field] = value
    monkeypatch.setattr(schema, "_validate_table", AsyncMock())
    monkeypatch.setattr(schema, "_rows", AsyncMock(return_value=[row]))
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await schema.validate_session_tables(None, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last,called,maximum,accepted",
    [
        (1, False, None, True),
        (10, True, 10, True),
        (10, False, 10, False),
        (9, True, 10, False),
        (9223372036854775807, True, 1, False),
    ],
)
async def test_baseline_checks_next_id_without_consuming_sequence(monkeypatch, last, called, maximum, accepted):
    monkeypatch.setattr(schema, "_validate_table", AsyncMock())
    read = AsyncMock(side_effect=[[sequence()], [{"last_value": last, "is_called": called, "max_id": maximum}]])
    monkeypatch.setattr(schema, "_rows", read)
    if accepted:
        await schema.validate_session_tables(None, 100, baseline=True)
    else:
        with pytest.raises(SchemaError, match="schema_mismatch"):
            await schema.validate_session_tables(None, 100, baseline=True)
    assert "nextval" not in read.call_args.args[1]
    assert "setval" not in read.call_args.args[1]


@pytest.mark.asyncio
async def test_readiness_does_not_require_sequence_select(monkeypatch):
    monkeypatch.setattr(schema, "_validate_table", AsyncMock())
    read = AsyncMock(return_value=[sequence()])
    monkeypatch.setattr(schema, "_rows", read)
    await schema.validate_session_tables(None, 100)
    assert read.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["extra", "nullable", "default", "generated", "collation"])
async def test_version_columns_reject_write_semantic_changes(monkeypatch, mutation):
    table = {
        "relkind": "r",
        "relpersistence": "p",
        "relrowsecurity": False,
        "relforcerowsecurity": False,
        "relispartition": False,
        "inherited": False,
        "triggers": False,
        "rules": False,
    }
    columns = [
        {
            "attname": name,
            "type": typename,
            "attnotnull": True,
            "attidentity": "",
            "attgenerated": "",
            "default_collation": True,
            "default_expr": None,
        }
        for name, typename in [
            ("version", "integer"),
            ("name", "text"),
            ("checksum", "text"),
            ("applied_at", "timestamp with time zone"),
        ]
    ]
    if mutation == "extra":
        columns.append({"attname": "extra"})
    elif mutation == "nullable":
        columns[0]["attnotnull"] = False
    elif mutation == "default":
        columns[0]["default_expr"] = "1"
    elif mutation == "generated":
        columns[0]["attgenerated"] = "s"
    else:
        columns[0]["default_collation"] = False
    monkeypatch.setattr(schema, "_rows", AsyncMock(side_effect=[[table], columns]))
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await schema.validate_version_table(None, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["indisvalid", "indisready", "indislive", "indimmediate", "plain", "ascending", "default_ops", "default_collation"],
)
async def test_index_semantics_rejected(monkeypatch, field):
    row = {
        key: True
        for key in [
            "indisvalid",
            "indisready",
            "indislive",
            "indimmediate",
            "plain",
            "ascending",
            "default_ops",
            "default_collation",
            "indisunique",
            "indisprimary",
        ]
    }
    row.update(amname="btree", indnkeyatts=1, indnatts=1, columns=["version"])
    row[field] = False
    monkeypatch.setattr(schema, "_rows", AsyncMock(return_value=[row]))
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await schema._validate_indexes(None, "schema_versions", 100)


@pytest.mark.asyncio
async def test_runtime_permissions_denied_is_explicit(monkeypatch):
    monkeypatch.setattr(schema, "_rows", AsyncMock(return_value=[{"allowed": False}]))
    with pytest.raises(SchemaError, match="permission_denied"):
        await schema.check_permissions(None, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("convalidated", False),
        ("condeferrable", True),
        ("confdeltype", "c"),
        ("confupdtype", "r"),
        ("confmatchtype", "f"),
        ("target_schema", "other"),
        ("target_columns", ["user_id"]),
    ],
)
async def test_foreign_key_semantics_must_match(monkeypatch, field, value):
    pk = {"contype": "p", "convalidated": True, "condeferrable": False, "condeferred": False, "columns": ["id"]}
    fk = {
        "contype": "f",
        "convalidated": True,
        "condeferrable": False,
        "condeferred": False,
        "columns": ["session_id"],
        "target_schema": "public",
        "target_table": "sessions",
        "target_columns": ["id"],
        "confdeltype": "a",
        "confupdtype": "a",
        "confmatchtype": "s",
    }
    fk[field] = value
    monkeypatch.setattr(schema, "_rows", AsyncMock(return_value=[pk, fk]))
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await schema._validate_constraints(None, "messages", 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["relrowsecurity", "relforcerowsecurity", "relispartition", "inherited", "triggers", "rules"]
)
async def test_table_features_with_hidden_write_semantics_rejected(monkeypatch, field):
    row = {
        "relkind": "r",
        "relpersistence": "p",
        "relrowsecurity": False,
        "relforcerowsecurity": False,
        "relispartition": False,
        "inherited": False,
        "triggers": False,
        "rules": False,
    }
    row[field] = True
    monkeypatch.setattr(schema, "_rows", AsyncMock(return_value=[row]))
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await schema.validate_version_table(None, 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table,rows",
    [
        ("schema_versions", [{"source_table": "other"}]),
        ("messages", [{"source_table": "other"}]),
        (
            "sessions",
            [{"source_schema": "public", "source_table": "other", "columns": ["session_id"], "target_columns": ["id"]}],
        ),
        (
            "sessions",
            [
                {
                    "source_schema": "other",
                    "source_table": "messages",
                    "columns": ["session_id"],
                    "target_columns": ["id"],
                }
            ],
        ),
        ("sessions", []),
    ],
)
async def test_unexpected_incoming_foreign_keys_rejected(monkeypatch, table, rows):
    monkeypatch.setattr(schema, "_rows", AsyncMock(return_value=rows))
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await schema._validate_incoming_foreign_keys(None, table, 100)


@pytest.mark.asyncio
async def test_only_single_expected_incoming_foreign_key_allowed(monkeypatch):
    row = {"source_schema": "public", "source_table": "messages", "columns": ["session_id"], "target_columns": ["id"]}
    read = AsyncMock(return_value=[row])
    monkeypatch.setattr(schema, "_rows", read)
    await schema._validate_incoming_foreign_keys(None, "sessions", 100)
    read.return_value = [row, row]
    with pytest.raises(SchemaError, match="schema_mismatch"):
        await schema._validate_incoming_foreign_keys(None, "sessions", 100)
