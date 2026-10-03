"""时间工具：统一按 UTC 处理。

PostgreSQL 的 timestamptz 取回带时区，SQLite 取回为无时区时间；
统一在系统边界归一化，避免 aware/naive 比较报错。
"""
from datetime import datetime, timezone


def ensure_utc(dt: datetime) -> datetime:
    """无时区时间按 UTC 处理；有时区的统一转成 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
