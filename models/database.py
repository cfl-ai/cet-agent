import aiosqlite
from pathlib import Path

DB_PATH = "data/cet_agent.db"

async def init_db():
    """初始化所有数据表"""
    Path("data").mkdir(exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # 用户表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE,
                level TEXT DEFAULT 'CET4',
                target_score INTEGER DEFAULT 425,
                created_at REAL,
                last_active REAL
            )
        """)

        # 用户知识状态（知识追踪）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_state (
                user_id TEXT,
                knowledge_point TEXT,
                mastery REAL DEFAULT 0.0,
                attempts INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                last_update REAL,
                PRIMARY KEY (user_id, knowledge_point)
            )
        """)

        # 题库
        await db.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                question_id TEXT PRIMARY KEY,
                question_type TEXT,
                topic TEXT,
                difficulty INTEGER,
                content TEXT,
                options TEXT,
                answer TEXT,
                explanation TEXT,
                knowledge_tags TEXT,
                source TEXT DEFAULT 'ai_generated',
                verified INTEGER DEFAULT 0,
                created_at REAL
            )
        """)

        # 答题记录
        await db.execute("""
            CREATE TABLE IF NOT EXISTS answer_records (
                record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                question_id TEXT,
                user_answer TEXT,
                correct_answer TEXT,
                is_correct INTEGER,
                duration REAL,
                created_at REAL
            )
        """)
        # ：真题演练会话表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS exam_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT,
                level TEXT,
                year INTEGER,
                month INTEGER,
                set_number INTEGER,
                duration_seconds INTEGER,
                started_at REAL,
                ended_at REAL,
                status TEXT DEFAULT 'in_progress',
                answers TEXT,
                score INTEGER,
                created_at REAL
            )
        """)

        # 单词分类字段（高频/常考/日常/少见）
        try:
            await db.execute("ALTER TABLE vocabulary ADD COLUMN category TEXT DEFAULT '高频'")
        except Exception:
            pass  # 字段已存在则忽略

        await db.execute("CREATE INDEX IF NOT EXISTS idx_session_user ON exam_sessions(user_id)")

        # 错题本
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wrong_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                question_id TEXT,
                wrong_count INTEGER DEFAULT 1,
                error_type TEXT,
                resolved INTEGER DEFAULT 0,
                last_wrong_at REAL,
                UNIQUE(user_id, question_id)
            )
        """)

        # 词汇表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vocabulary (
                word TEXT PRIMARY KEY,
                phonetic TEXT,
                meaning TEXT,
                level TEXT,
                frequency INTEGER DEFAULT 0,
                topic TEXT,
                examples TEXT
            )
        """)

        # 学习统计
        await db.execute("""
            CREATE TABLE IF NOT EXISTS study_stats (
                user_id TEXT,
                stat_date TEXT,
                questions_done INTEGER DEFAULT 0,
                correct_count INTEGER DEFAULT 0,
                words_reviewed INTEGER DEFAULT 0,
                study_minutes INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, stat_date)
            )
        """)

        # 索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_answer_user ON answer_records(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wrong_user ON wrong_questions(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kstate_user ON knowledge_state(user_id)")

        await db.commit()