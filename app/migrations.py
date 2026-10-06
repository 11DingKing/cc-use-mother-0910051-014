"""轻量级幂等迁移：SQLite 不会自动为已存在的表补充新列，
这里对老版本数据库执行必要的 ALTER TABLE ADD COLUMN。"""
from sqlalchemy import inspect, text

from app.database import engine


# 表名 -> [(列名, 列定义SQL)]
_PENDING_COLUMNS = {
    "sessions": [
        ("device_sets_needed", "INTEGER DEFAULT 1"),
    ],
    "change_requests": [
        ("resource_group_id", "VARCHAR(36)"),
    ],
}


def run_migrations() -> None:
    with engine.begin() as conn:
        # 必须复用当前连接做反射：所有事务均以 BEGIN IMMEDIATE 开始，
        # 另取连接会与本连接持有的写锁自死锁
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())
        for table, columns in _PENDING_COLUMNS.items():
            if table not in existing_tables:
                continue  # 新表由 create_all 直接建出，无需迁移
            present = {col["name"] for col in inspector.get_columns(table)}
            for column_name, column_sql in columns:
                if column_name not in present:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {column_name} {column_sql}"
                    ))
