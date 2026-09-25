"""首迁移的 PostgreSQL catalog 契约；只读、借用连接，不持有事务或修复结构。"""

import asyncio
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class SchemaError(RuntimeError):
    """仅公开稳定原因码；数据库错误由调用边界归因。"""


_COLUMNS = {
    "schema_versions": {
        "version": ("integer", True),
        "name": ("text", True),
        "checksum": ("text", True),
        "applied_at": ("timestamp with time zone", True),
    },
    "sessions": {
        "id": ("character varying(36)", True),
        "user_id": ("character varying(64)", True),
        "title": ("character varying(200)", False),
        "system_prompt": ("text", False),
        "created_at": ("timestamp with time zone", False),
        "updated_at": ("timestamp with time zone", False),
        "status": ("character varying(20)", False),
        "meta": ("json", False),
    },
    "messages": {
        "id": ("bigint", True),
        "session_id": ("character varying(36)", False),
        "role": ("character varying(20)", True),
        "content": ("text", True),
        "reasoning_content": ("text", False),
        "token_count": ("integer", False),
        "created_at": ("timestamp with time zone", False),
        "meta": ("json", False),
    },
}


async def _rows(connection: AsyncConnection, sql: str, deadline: float, **params: Any) -> list[Any]:
    if isinstance(deadline, bool) or not math.isfinite(deadline):
        raise SchemaError("deadline_invalid")

    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError

    if asyncio.get_running_loop().time() >= deadline:
        raise SchemaError("timeout")

    async with asyncio.timeout_at(deadline):
        result = await connection.execute(text(sql), params)
        return list(result.mappings().all())


async def managed_tables(connection: AsyncConnection, deadline: float) -> set[str]:
    """仅发现三张受管表；同名非普通表直接拒绝。"""
    rows = await _rows(
        connection,
        """
        SELECT c.relname, c.relkind::text AS relkind FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname IN ('schema_versions','sessions','messages')
    """,
        deadline,
    )
    if any(row["relkind"] != "r" for row in rows):
        raise SchemaError("schema_mismatch")
    return {row["relname"] for row in rows}


async def _validate_table(connection: AsyncConnection, table: str, deadline: float) -> None:
    rows = await _rows(
        connection,
        """
        SELECT c.relkind::text AS relkind, c.relpersistence::text AS relpersistence,
          c.relrowsecurity, c.relforcerowsecurity,
          c.relispartition,
          EXISTS(SELECT 1 FROM pg_catalog.pg_inherits i
                 WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS inherited,
          EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t
                 WHERE t.tgrelid=c.oid AND (NOT t.tgisinternal OR t.tgenabled<>'O')) AS triggers,
          EXISTS(SELECT 1 FROM pg_catalog.pg_rewrite r WHERE r.ev_class=c.oid) AS rules
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname=:table
    """,
        deadline,
        table=table,
    )
    if not rows:
        raise SchemaError("schema_missing")
    row = rows[0]
    if (
        row["relkind"] != "r"
        or row["relpersistence"] != "p"
        or any(
            row[key]
            for key in ("relrowsecurity", "relforcerowsecurity", "relispartition", "inherited", "triggers", "rules")
        )
    ):
        raise SchemaError("schema_mismatch")
    columns = await _rows(
        connection,
        """
        SELECT a.attname, pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,
          a.attnotnull, a.attidentity::text AS attidentity, a.attgenerated::text AS attgenerated,
          a.attcollation=t.typcollation AS default_collation,
          pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS default_expr
        FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
        LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relname=:table AND a.attnum>0 AND NOT a.attisdropped
    """,
        deadline,
        table=table,
    )
    if {r["attname"] for r in columns} != set(_COLUMNS[table]):
        raise SchemaError("schema_mismatch")
    for row in columns:
        expected = _COLUMNS[table][row["attname"]]
        serial = table == "messages" and row["attname"] == "id"
        if (
            (row["type"], row["attnotnull"]) != expected
            or row["attidentity"]
            or row["attgenerated"]
            or not row["default_collation"]
            or (not serial and row["default_expr"] is not None)
        ):
            raise SchemaError("schema_mismatch")
    await _validate_constraints(connection, table, deadline)
    await _validate_indexes(connection, table, deadline)


async def _validate_constraints(connection: AsyncConnection, table: str, deadline: float) -> None:
    rows = await _rows(
        connection,
        """
        SELECT con.contype::text AS contype, con.convalidated, con.condeferrable, con.condeferred,
          con.confupdtype::text AS confupdtype, con.confdeltype::text AS confdeltype,
          con.confmatchtype::text AS confmatchtype,
          ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(num,ord)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.num
            ORDER BY k.ord) AS columns,
          rn.nspname AS target_schema, rc.relname AS target_table,
          ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(num,ord)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.num
            ORDER BY k.ord) AS target_columns
        FROM pg_catalog.pg_constraint con
        JOIN pg_catalog.pg_class c ON c.oid=con.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_class rc ON rc.oid=con.confrelid
        LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid=rc.relnamespace
        WHERE n.nspname='public' AND c.relname=:table
          AND (con.contype<>'n' OR NOT con.convalidated)
    """,
        deadline,
        table=table,
    )
    expected_count = 2 if table == "messages" else 1
    if len(rows) != expected_count:
        raise SchemaError("schema_mismatch")
    pk = "version" if table == "schema_versions" else "id"
    kinds = []
    for row in rows:
        kinds.append(row["contype"])
        if not row["convalidated"] or row["condeferrable"] or row["condeferred"]:
            raise SchemaError("schema_mismatch")
        if row["contype"] == "p" and row["columns"] == [pk]:
            continue
        if (
            table == "messages"
            and row["contype"] == "f"
            and row["columns"] == ["session_id"]
            and row["target_schema"] == "public"
            and row["target_table"] == "sessions"
            and row["target_columns"] == ["id"]
            and row["confupdtype"] == row["confdeltype"] == "a"
            and row["confmatchtype"] == "s"
        ):
            continue
        raise SchemaError("schema_mismatch")
    if sorted(kinds) != (["f", "p"] if table == "messages" else ["p"]):
        raise SchemaError("schema_mismatch")
    await _validate_incoming_foreign_keys(connection, table, deadline)


async def _validate_incoming_foreign_keys(connection: AsyncConnection, table: str, deadline: float) -> None:
    """其他表的入向外键也能改变受管表删除行为，不能只检查本表声明的约束。"""
    rows = await _rows(
        connection,
        """
        SELECT sn.nspname AS source_schema, sc.relname AS source_table,
          ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(num,ord)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=con.conrelid AND a.attnum=k.num
            ORDER BY k.ord) AS columns,
          ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(num,ord)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=con.confrelid AND a.attnum=k.num
            ORDER BY k.ord) AS target_columns
        FROM pg_catalog.pg_constraint con
        JOIN pg_catalog.pg_class tc ON tc.oid=con.confrelid
        JOIN pg_catalog.pg_namespace tn ON tn.oid=tc.relnamespace
        JOIN pg_catalog.pg_class sc ON sc.oid=con.conrelid
        JOIN pg_catalog.pg_namespace sn ON sn.oid=sc.relnamespace
        WHERE con.contype='f' AND tn.nspname='public' AND tc.relname=:table
        """,
        deadline,
        table=table,
    )
    if table != "sessions":
        if rows:
            raise SchemaError("schema_mismatch")
        return
    # 其动作、有效性、延迟属性在 messages 的出向约束检查中完整核验。
    if len(rows) != 1 or dict(rows[0]) != {
        "source_schema": "public",
        "source_table": "messages",
        "columns": ["session_id"],
        "target_columns": ["id"],
    }:
        raise SchemaError("schema_mismatch")


async def _validate_indexes(connection: AsyncConnection, table: str, deadline: float) -> None:
    rows = await _rows(
        connection,
        """
        SELECT i.indisunique, i.indisprimary, i.indisvalid, i.indisready, i.indislive,
          i.indimmediate, am.amname, i.indnkeyatts, i.indnatts,
          i.indexprs IS NULL AND i.indpred IS NULL AS plain,
          ARRAY(SELECT a.attname FROM unnest(i.indkey::smallint[]) WITH ORDINALITY k(num,ord)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
            ORDER BY k.ord) AS columns,
          NOT EXISTS(SELECT 1 FROM unnest(i.indoption::smallint[]) opt WHERE opt<>0) AS ascending,
          NOT EXISTS(SELECT 1 FROM unnest(i.indclass::oid[]) op
            JOIN pg_catalog.pg_opclass cls ON cls.oid=op WHERE NOT cls.opcdefault) AS default_ops,
          NOT EXISTS(SELECT 1 FROM unnest(i.indkey::smallint[],i.indcollation::oid[]) k(num,coll)
            JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
            WHERE k.coll<>a.attcollation) AS default_collation
        FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indrelid
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_class ic ON ic.oid=i.indexrelid
        JOIN pg_catalog.pg_am am ON am.oid=ic.relam
        WHERE n.nspname='public' AND c.relname=:table
    """,
        deadline,
        table=table,
    )
    expected = (
        {(("version",), True, True)}
        if table == "schema_versions"
        else {
            (("id",), True, True),
            ((("user_id" if table == "sessions" else "session_id"),), False, False),
        }
    )
    actual = set()
    for row in rows:
        if (
            row["amname"] != "btree"
            or row["indnkeyatts"] != 1
            or row["indnatts"] != 1
            or not all(
                row[k]
                for k in (
                    "indisvalid",
                    "indisready",
                    "indislive",
                    "indimmediate",
                    "plain",
                    "ascending",
                    "default_ops",
                    "default_collation",
                )
            )
        ):
            raise SchemaError("schema_mismatch")
        actual.add((tuple(row["columns"]), row["indisunique"], row["indisprimary"]))
    if actual != expected or len(rows) != len(expected):
        raise SchemaError("schema_mismatch")


async def validate_version_table(connection: AsyncConnection, deadline: float) -> None:
    """核对版本表的完整结构，拒绝额外写入约束。"""
    await _validate_table(connection, "schema_versions", deadline)


async def validate_session_tables(connection: AsyncConnection, deadline: float, *, baseline: bool = False) -> None:
    """校验业务结构；baseline 要求调用者已排空旧 writer 并持有两表排他锁。"""
    await _validate_table(connection, "sessions", deadline)
    await _validate_table(connection, "messages", deadline)
    rows = await _rows(
        connection,
        """
        SELECT seq.seqtypid='pg_catalog.int8'::regtype AS bigint,
          seq.seqstart, seq.seqincrement, seq.seqmin, seq.seqmax, seq.seqcache, seq.seqcycle,
          sc.relname, sn.nspname,
          pg_catalog.pg_get_expr(ad.adbin,ad.adrelid)=
            'nextval(' || quote_literal(sc.oid::regclass::text) || '::regclass)' AS default_matches
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attname='id'
        JOIN pg_catalog.pg_depend dep ON dep.refclassid='pg_catalog.pg_class'::regclass
          AND dep.refobjid=c.oid AND dep.refobjsubid=a.attnum
          AND dep.classid='pg_catalog.pg_class'::regclass AND dep.deptype='a'
        JOIN pg_catalog.pg_class sc ON sc.oid=dep.objid AND sc.relkind='S'
        JOIN pg_catalog.pg_namespace sn ON sn.oid=sc.relnamespace
        JOIN pg_catalog.pg_sequence seq ON seq.seqrelid=sc.oid
        JOIN pg_catalog.pg_attrdef ad ON ad.adrelid=c.oid AND ad.adnum=a.attnum
        WHERE n.nspname='public' AND c.relname='messages'
    """,
        deadline,
    )
    if len(rows) != 1:
        raise SchemaError("schema_mismatch")
    row = rows[0]
    if (
        not row["bigint"]
        or not row["default_matches"]
        or row["seqcycle"]
        or (row["seqstart"], row["seqincrement"], row["seqmin"], row["seqmax"], row["seqcache"])
        != (1, 1, 1, 9223372036854775807, 1)
        or row["nspname"] != "public"
        or row["relname"] != "messages_id_seq"
    ):
        raise SchemaError("schema_mismatch")
    if baseline:
        values = await _rows(
            connection,
            """
            SELECT last_value, is_called,
              (SELECT max(id) FROM public.messages) AS max_id
            FROM public.messages_id_seq
        """,
            deadline,
        )
        value = values[0]
        next_id = value["last_value"] + int(value["is_called"])
        if next_id > 9223372036854775807 or next_id <= (value["max_id"] or 0):
            raise SchemaError("schema_mismatch")


async def check_permissions(connection: AsyncConnection, deadline: float) -> None:
    """readiness 仅查询权限，不写业务数据，也不要求迁移 DDL 权限。"""
    rows = await _rows(
        connection,
        """
        SELECT has_schema_privilege('public','USAGE')
          AND has_table_privilege('public.schema_versions','SELECT')
          AND has_table_privilege('public.sessions','SELECT')
          AND has_table_privilege('public.sessions','INSERT')
          AND has_table_privilege('public.sessions','UPDATE')
          AND has_table_privilege('public.sessions','DELETE')
          AND has_table_privilege('public.messages','SELECT')
          AND has_table_privilege('public.messages','INSERT')
          AND has_table_privilege('public.messages','UPDATE')
          AND has_table_privilege('public.messages','DELETE')
          AND (has_sequence_privilege('public.messages_id_seq','USAGE')
               OR has_sequence_privilege('public.messages_id_seq','UPDATE'))
          AND current_setting('transaction_read_only')='off' AS allowed
    """,
        deadline,
    )
    if not rows or not rows[0]["allowed"]:
        raise SchemaError("permission_denied")
