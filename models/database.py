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

        # 听力音频缓存
        # 注意：此前该表只在 scripts/migrate_db.py 中创建，
        # 全新部署只执行 init_db 时会缺失，导致 TTS 听力功能写入失败。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS listening_assets (
                asset_id TEXT PRIMARY KEY,
                question_id TEXT,
                transcript TEXT,
                audio_url TEXT,
                created_at REAL
            )
        """)

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
                examples TEXT,
                category TEXT DEFAULT '高频'
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

        # 真实考试成绩 — 预测模型的监督信号来源。
        # 此前 train_predictor.py 的标签由掌握度自举推导（290+avg*420），
        # 模型只是在复述这条公式，训再多也不会变准。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_exam_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                level TEXT,
                score INTEGER,
                exam_date TEXT,
                source TEXT DEFAULT 'real_exam',
                created_at REAL
            )
        """)

        # 模型版本注册表 — 记录每次训练的结果，用于自动晋升与回滚决策
        await db.execute("""
            CREATE TABLE IF NOT EXISTS model_registry (
                version INTEGER PRIMARY KEY AUTOINCREMENT,
                trained_at REAL,
                sample_count INTEGER,
                train_mae REAL,
                val_mae REAL,
                baseline_mae REAL,
                status TEXT,
                note TEXT
            )
        """)

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_exam_score_user "
            "ON user_exam_scores(user_id)")

        # ==================== 主观题批改 ====================
        # 写作批改结果。score 为四六级写作 15 分制原始分，
        # score_710 为折算后的 106.5 分制得分（写作占 15%）。
        # status 支持"机评 → 人工复核"两段式：pending_review / approved / overridden。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS writing_grades (
                grade_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                level TEXT DEFAULT 'CET4',
                topic TEXT,
                essay TEXT,
                word_count INTEGER DEFAULT 0,
                score REAL,
                score_710 REAL,
                level_band TEXT,
                content_score REAL,
                language_score REAL,
                organization_score REAL,
                feedback TEXT,
                strengths TEXT,
                improvements TEXT,
                sentence_notes TEXT,
                ai_traces TEXT,
                status TEXT DEFAULT 'pending_review',
                reviewer_note TEXT,
                reviewed_at REAL,
                model_meta TEXT,
                created_at REAL
            )
        """)

        # 翻译批改结果。issues 记录逐句问题（漏译/错译/语法/用词），
        # 是"真实判分"的核心证据：分数必须能对应到具体句子，而非一句笼统评语。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS translation_grades (
                grade_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                level TEXT DEFAULT 'CET4',
                source_text TEXT,
                user_translation TEXT,
                reference_translation TEXT,
                score REAL,
                score_710 REAL,
                level_band TEXT,
                accuracy_score REAL,
                language_score REAL,
                feedback TEXT,
                issues TEXT,
                improved_translation TEXT,
                status TEXT DEFAULT 'pending_review',
                reviewer_note TEXT,
                reviewed_at REAL,
                model_meta TEXT,
                created_at REAL
            )
        """)

        # ==================== 错题归因 ====================
        # 每道错题一条归因：错因 + 知识点 + 根因 + 补救动作 + 置信度。
        # evidence 保存判定依据（时长/相似选项/掌握度/错因规则），
        # 便于解释"为什么判成这个概念不清"，也支持人工复核。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS wrong_attributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                knowledge_point TEXT,
                error_type TEXT,
                confidence REAL DEFAULT 0.5,
                root_cause TEXT,
                remediation TEXT,
                evidence TEXT,
                source TEXT DEFAULT 'rules',
                created_at REAL,
                UNIQUE(user_id, question_id)
            )
        """)

        # ==================== 个性化提分方案 ====================
        # source_snapshot 固化生成时的学情快照，保证方案可复算、可追溯；
        # model_meta 记录是否走了 LLM 或模板兜底。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS improvement_plans (
                plan_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                level TEXT,
                current_score REAL,
                target_score REAL,
                gap REAL,
                horizon_days INTEGER,
                focus_points TEXT,
                weekly_schedule TEXT,
                actions TEXT,
                narrative TEXT,
                source_snapshot TEXT,
                model_meta TEXT,
                created_at REAL
            )
        """)

        # 索引
        await db.execute("CREATE INDEX IF NOT EXISTS idx_answer_user ON answer_records(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_writing_user ON writing_grades(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_translation_user ON translation_grades(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_attr_user ON wrong_attributions(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_plan_user ON improvement_plans(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_wrong_user ON wrong_questions(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kstate_user ON knowledge_state(user_id)")

        await db.commit()