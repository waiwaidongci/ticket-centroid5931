"""启动时补齐新增列，兼容已有数据库。

`Base.metadata.create_all` 只建缺失表，不会给已存在的表加列。
这里按需执行幂等的 ALTER，让升级后的实例能直接写入排班推算字段。
"""

from sqlalchemy import inspect, text

_COLUMNS = {
    'runs': (
        ('kind', "VARCHAR(20) DEFAULT 'acceptance' NOT NULL"),
        ('basis_run_id', 'INTEGER REFERENCES runs(id)'),
        ('request_key', 'VARCHAR(64)'),
        ('resumes', "INTEGER DEFAULT 0 NOT NULL"),
    ),
}


def upgrade(engine):
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, columns in _COLUMNS.items():
            if table not in tables:
                continue
            existing = {column['name'] for column in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in existing:
                    continue
                connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
        if 'runs' in tables:
            indexes = {index['name'] for index in inspector.get_indexes('runs')}
            if 'ix_runs_request_key' not in indexes:
                try:
                    connection.execute(text('CREATE UNIQUE INDEX ix_runs_request_key '
                                            'ON runs (request_key)'))
                except Exception:
                    # 并发的启动实例可能已建好索引，忽略重复创建。
                    pass


def normalize_legacy(engine):
    """旧运行没有 kind 时按验收处理，保证列表与筛选一致。"""
    with engine.begin() as connection:
        connection.execute(text("UPDATE runs SET kind = 'acceptance' WHERE kind IS NULL"))
