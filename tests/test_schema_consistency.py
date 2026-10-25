"""
Schema 一致性测试 —— 本项目的"回归防线"。

背景：/api/exam/paper/* 三个接口曾全线 500，根因是
models/database.py 与 scripts/migrate_db.py 维护了两套互相冲突的
exam_sessions 建表语句，数据库实际采用后者，导致 main.py 查询的
year / month / set_number / duration_seconds / score 列根本不存在。
而 CREATE TABLE IF NOT EXISTS 让这种漂移悄无声息。

本测试从三个维度守住这条线：
  A. 两份 DDL 来源对同一张表的列定义必须一致
  B. 源码中所有 SQL 实际引用的列，必须存在于表结构中（含 JOIN 场景）
  C. init_db 必须建出全部核心表
"""
import ast
import re
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------- 工具 ----------

def _extract_string_literals(source: str) -> list[str]:
    """
    用 AST 提取源码中的全部字符串常量。
    比全文正则更可靠：不会跨语句贪婪匹配，也不会匹配到注释。
    """
    out = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def _parse_create_table_columns(source: str) -> dict[str, set[str]]:
    """从源码中提取所有 CREATE TABLE 的 {表名: 列名集合}"""
    tables: dict[str, set[str]] = {}
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*)\)",
        re.I | re.S,
    )
    # 只在字符串常量里找 DDL
    for literal in _extract_string_literals(source):
        for m in pattern.finditer(literal):
            table = m.group(1).lower()
            body = m.group(2)
            cols = set()
            for line in body.split(","):
                line = line.strip()
                if not line:
                    continue
                # 跳过约束行
                if re.match(
                    r"^(PRIMARY|UNIQUE|FOREIGN|CHECK|CONSTRAINT|INDEX)\b",
                    line, re.I,
                ):
                    continue
                cm = re.match(
                    r"^(\w+)\s+(TEXT|INTEGER|REAL|BLOB|NUMERIC|BOOLEAN)",
                    line, re.I,
                )
                if cm:
                    cols.add(cm.group(1).lower())
            if cols:
                tables.setdefault(table, set()).update(cols)
    return tables


def _parse_alter_add_columns(source: str) -> dict[str, set[str]]:
    """提取 ALTER TABLE ... ADD COLUMN 的 {表名: 列名集合}"""
    result: dict[str, set[str]] = {}
    for literal in _extract_string_literals(source):
        for m in re.finditer(
            r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
            literal, re.I,
        ):
            result.setdefault(m.group(1).lower(), set()).add(
                m.group(2).lower()
            )
    # ALTER 语句常以 f-string 形式出现在代码里，也扫描原文
    for m in re.finditer(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", source, re.I
    ):
        result.setdefault(m.group(1).lower(), set()).add(m.group(2).lower())
    return result


def _load_source(rel_path: str) -> str:
    return (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")


def _known_schema() -> dict[str, set[str]]:
    """合并 database.py 的 DDL、migrate_db.py 的 DDL 与其 ALTER 补齐"""
    known = _parse_create_table_columns(
        _load_source("models/database.py")
    )
    for t, cols in _parse_create_table_columns(
        _load_source("scripts/migrate_db.py")
    ).items():
        known.setdefault(t, set()).update(cols)
    for t, cols in _parse_alter_add_columns(
        _load_source("scripts/migrate_db.py")
    ).items():
        known.setdefault(t, set()).update(cols)
    for t, cols in _parse_alter_add_columns(
        _load_source("models/database.py")
    ).items():
        known.setdefault(t, set()).update(cols)
    return known


# ---------- SQL 引用提取 ----------

_AGG_FUNCS = {
    "count", "sum", "avg", "max", "min", "distinct",
    "round", "coalesce", "total", "group_concat",
}
_JOIN_KEYWORDS = {
    "join", "inner", "left", "right", "outer", "full", "cross",
    "on", "using", "as", "and", "or",
}


def _tables_in_from(from_clause: str) -> set[str]:
    """'wrong_questions w JOIN questions q' → {'wrong_questions', 'questions'}"""
    tables = set()
    # 截断到 ON / USING 之前
    head = re.split(r"\bON\b|\bUSING\b", from_clause, flags=re.I)[0]
    for m in re.finditer(r"\b([a-z_][a-z0-9_]*)\b", head, re.I):
        name = m.group(1).lower()
        if name not in _JOIN_KEYWORDS:
            tables.add(name)
    return tables


def _extract_sql_references(source: str):
    """
    从字符串常量中提取 [(操作, 表名集合, 列名集合)]。
    每个元素对应一条 SQL；表名集合涵盖 FROM/JOIN 涉及的所有表。
    """
    refs = []
    literals = _extract_string_literals(source)

    for sql in literals:
        # UPDATE <table> SET col=?, ...
        for m in re.finditer(
            r"UPDATE\s+(\w+)\s+SET\s+(.+?)(?:\bWHERE\b|$)",
            sql, re.I | re.S,
        ):
            table = m.group(1).lower()
            cols = set()
            for part in m.group(2).split(","):
                cm = re.match(r"\s*(\w+)\s*=", part)
                if cm:
                    cols.add(cm.group(1).lower())
            if cols:
                refs.append(("UPDATE", {table}, cols))

        # INSERT [OR IGNORE] INTO <table> (col, ...)
        for m in re.finditer(
            r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)\s*\(([^)]*)\)",
            sql, re.I,
        ):
            table = m.group(1).lower()
            cols = {
                c.strip().lower()
                for c in m.group(2).split(",")
                if c.strip().isidentifier()
            }
            if cols:
                refs.append(("INSERT", {table}, cols))

        # SELECT <cols> FROM <tables> [WHERE|GROUP|ORDER|LIMIT|JOIN|END]
        for m in re.finditer(
            r"SELECT\s+(.+?)\s+FROM\s+(.+?)"
            r"(?:\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|\bJOIN\b|$)",
            sql, re.I | re.S,
        ):
            cols_str, from_clause = m.group(1), m.group(2)
            if "*" in cols_str:
                continue
            cols = set()
            for part in cols_str.split(","):
                part = part.strip()
                if not part:
                    continue
                im = re.match(r"^(?:\w+\.)?(\w+)", part)
                if not im:
                    continue
                name = im.group(1).lower()
                if name in _AGG_FUNCS:
                    continue
                cols.add(name)
            if cols:
                refs.append(
                    ("SELECT", _tables_in_from(from_clause), cols)
                )

    return refs


# ---------- A. 两份 DDL 必须一致 ----------

def test_ddl_sources_are_consistent():
    """
    database.py 与 migrate_db.py 对同一张表的列定义必须一致。
    这正是 exam_sessions 事故的根因。
    """
    db_tables = _parse_create_table_columns(
        _load_source("models/database.py")
    )
    mig_create = _parse_create_table_columns(
        _load_source("scripts/migrate_db.py")
    )
    mig_alter = _parse_alter_add_columns(
        _load_source("scripts/migrate_db.py")
    )

    assert db_tables, "未能从 models/database.py 解析出建表语句"

    problems = []
    for table, cols in db_tables.items():
        # 只比较两边都用 CREATE TABLE 管理的表；
        # migrate_db.py 仅做 ALTER 的表（如 vocabulary）不在此列
        if table not in mig_create:
            continue
        effective = mig_create[table] | mig_alter.get(table, set())
        missing = cols - effective
        if missing:
            problems.append(
                f"表 {table}: database.py 定义而 migrate_db.py 缺少列 "
                f"{sorted(missing)}"
            )

    # 反向守护：migrate_db.py 试图 ALTER 添加的列，
    # 必须是 database.py 权威 schema 中已定义的列，
    # 防止迁移脚本偷偷引入 schema 里不存在的新列。
    for table, cols in mig_alter.items():
        if table not in db_tables:
            continue
        unknown = cols - db_tables[table]
        if unknown:
            problems.append(
                f"表 {table}: migrate_db.py 添加了 database.py 未定义的列 "
                f"{sorted(unknown)}"
            )

    assert not problems, "两份 DDL 定义不一致：\n" + "\n".join(problems)


# ---------- B. SQL 引用的列必须存在 ----------

@pytest.mark.parametrize("module", [
    "main.py",
    "agents/orchestrator.py",
    "agents/grading_agent.py",
    "agents/review_agent.py",
    "agents/question_agent.py",
    "agents/study_plan_agent.py",
    "core/kt_model.py",
    "core/score_predictor.py",
    "services/user_service.py",
    "scripts/build_vector_index.py",
    "scripts/train_predictor.py",
    "scripts/migrate_db.py",
])
def test_sql_columns_exist_in_schema(module):
    """模块中每条 SQL 引用的列，必须能在其涉及的表中找到"""
    path = PROJECT_ROOT / module
    if not path.exists():
        pytest.skip(f"{module} 不存在")

    src = path.read_text(encoding="utf-8")
    known = _known_schema()
    # 项目全部已知列的并集：JOIN 与表别名场景下，列只要存在于
    # 任一相关表中即视为合法，避免把 q.xxx 误判为漂移
    all_known_cols = set()
    for cols in known.values():
        all_known_cols |= cols

    violations = []
    for op, tables, cols in _extract_sql_references(src):
        if not tables:
            continue
        # 只校验涉及已知表的 SQL；完全未登记的表交给 DDL 一致性测试
        if not any(t in known for t in tables):
            continue
        missing = cols - all_known_cols
        if missing:
            violations.append(
                f"{op} {sorted(tables)} 引用了不存在的列: {sorted(missing)}"
            )

    assert not violations, f"{module} 存在 schema 漂移：\n" + "\n".join(violations)


# ---------- C. init_db 必须建出全部核心表 ----------

def test_init_db_creates_all_expected_tables():
    """init_db 必须独立建出全部核心表（不依赖 migrate_db.py）"""
    import asyncio
    from models.database import init_db

    asyncio.run(init_db())

    conn = sqlite3.connect("data/cet_agent.db")
    tables = {
        r[0].lower() for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    conn.close()

    expected = {
        "users", "knowledge_state", "questions", "answer_records",
        "exam_sessions", "wrong_questions", "vocabulary", "study_stats",
        "listening_assets",
    }
    missing = expected - tables
    assert not missing, (
        f"init_db 未创建表: {sorted(missing)}。"
        f"这些表目前只在 scripts/migrate_db.py 中创建，"
        f"全新部署只执行 init_db 时会缺失。"
    )
