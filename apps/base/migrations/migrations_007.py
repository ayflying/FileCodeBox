"""为 FileCodes 补充 P2P 直传所需字段（详见 docs/p2p-design.md §4.1）。

SQLite 的 ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS，
因此这里先用 PRAGMA 读取现有列，仅在缺列时执行，保证迁移可重复执行。
"""

from tortoise import connections

NEW_COLUMNS = [
    ("is_p2p", "INTEGER NOT NULL DEFAULT 0"),
    ("p2p_token_hash", "VARCHAR(64)"),
    ("p2p_status", "VARCHAR(16) NOT NULL DEFAULT 'offline'"),
    ("p2p_last_seen", "TIMESTAMP"),
    ("p2p_served_count", "INTEGER NOT NULL DEFAULT 0"),
    ("p2p_bytes_sent", "BIGINT NOT NULL DEFAULT 0"),
    ("p2p_last_transport", "VARCHAR(16)"),
]


async def migrate():
    conn = connections.get("default")
    rows = await conn.execute_query_dict("PRAGMA table_info(filecodes)")
    existing = {str(row.get("name") or "") for row in rows}

    for column_name, column_ddl in NEW_COLUMNS:
        if column_name in existing:
            continue
        await conn.execute_script(
            f"ALTER TABLE filecodes ADD COLUMN {column_name} {column_ddl};"
        )

    await conn.execute_script(
        """
        CREATE INDEX IF NOT EXISTS idx_filecodes_is_p2p
            ON filecodes (is_p2p);
        """
    )
