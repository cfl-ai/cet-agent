"""初始化基础词汇和示例题目 — 从真题高频词精选"""
import aiosqlite
import time
import json
from models.database import DB_PATH

# 四六级高频词 Top 30 示例（完整版可扩充至500+）
HIGH_FREQ_WORDS = [
    ("abandon", "/əˈbændən/", "v. 放弃，抛弃", "CET4", 15, "心理"),
    ("abstract", "/ˈæbstrækt/", "a. 抽象的 n. 摘要", "CET4", 12, "学术"),
    ("academic", "/ˌækəˈdemɪk/", "a. 学术的", "CET4", 18, "教育"),
    ("accommodate", "/əˈkɒmədeɪt/", "v. 容纳，适应", "CET6", 10, "社会"),
    ("accompany", "/əˈkʌmpəni/", "v. 陪伴，伴随", "CET4", 11, "社会"),
    ("accomplish", "/əˈkʌmplɪʃ/", "v. 完成，实现", "CET4", 14, "工作"),
    ("accumulate", "/əˈkjuːmjəleɪt/", "v. 积累", "CET6", 9, "学术"),
    ("accurate", "/ˈækjərət/", "a. 精确的", "CET4", 16, "学术"),
    ("achieve", "/əˈtʃiːv/", "v. 实现，达到", "CET4", 22, "工作"),
    ("acknowledge", "/əkˈnɒlɪdʒ/", "v. 承认，致谢", "CET6", 13, "学术"),
    ("acquire", "/əˈkwaɪə/", "v. 获得，习得", "CET4", 17, "学术"),
    ("adapt", "/əˈdæpt/", "v. 适应，改编", "CET4", 15, "环境"),
    ("adequate", "/ˈædɪkwət/", "a. 充足的", "CET6", 12, "学术"),
    ("adjust", "/əˈdʒʌst/", "v. 调整，适应", "CET4", 14, "心理"),
    ("administration", "/ədˌmɪnɪˈstreɪʃn/", "n. 管理，行政", "CET4", 11, "社会"),
    ("adopt", "/əˈdɒpt/", "v. 采纳，收养", "CET4", 13, "社会"),
    ("advocate", "/ˈædvəkeɪt/", "v. 提倡 n. 拥护者", "CET6", 10, "社会"),
    ("affect", "/əˈfekt/", "v. 影响", "CET4", 20, "心理"),
    ("aggressive", "/əˈɡresɪv/", "a. 进取的，好斗的", "CET6", 9, "心理"),
    ("allocate", "/ˈæləkeɪt/", "v. 分配", "CET6", 8, "经济"),
    ("alternative", "/ɔːlˈtɜːnətɪv/", "n. 替代品 a. 替代的", "CET4", 15, "环境"),
    ("ambiguous", "/æmˈbɪɡjuəs/", "a. 模棱两可的", "CET6", 7, "学术"),
    ("analyze", "/ˈænəlaɪz/", "v. 分析", "CET4", 19, "学术"),
    ("anticipate", "/ænˈtɪsɪpeɪt/", "v. 预期，期望", "CET6", 11, "心理"),
    ("apparent", "/əˈpærənt/", "a. 明显的", "CET4", 13, "学术"),
    ("appeal", "/əˈpiːl/", "v./n. 呼吁，吸引", "CET4", 14, "社会"),
    ("appreciate", "/əˈpriːʃieɪt/", "v. 感激，欣赏", "CET4", 17, "心理"),
    ("approach", "/əˈprəʊtʃ/", "n. 方法 v. 接近", "CET4", 21, "学术"),
    ("appropriate", "/əˈprəʊpriət/", "a. 适当的", "CET4", 18, "学术"),
    ("approve", "/əˈpruːv/", "v. 批准，赞成", "CET4", 12, "社会"),
]

SAMPLE_QUESTIONS = [
    {
        "question_id": "q_seed_001",
        "question_type": "词汇辨析",
        "topic": "教育",
        "difficulty": 3,
        "content": "The university has decided to ______ its admission policy to attract more international students.",
        "options": json.dumps(["adapt", "adopt", "adjust", "advocate"]),
        "answer": "C",
        "explanation": "adjust 表示'调整'，与 policy 搭配最合适；adapt 侧重'适应'；adopt 是'采纳'；advocate 是'提倡'。",
        "knowledge_tags": json.dumps(["动词辨析", "adjust", "adopt"]),
        "source": "seed",
        "verified": 1,
    },
    {
        "question_id": "q_seed_002",
        "question_type": "词汇辨析",
        "topic": "社会",
        "difficulty": 4,
        "content": "Despite the ______ evidence, the committee refused to change its decision.",
        "options": json.dumps(["apparent", "obvious", "transparent", "ambiguous"]),
        "answer": "B",
        "explanation": "obvious evidence 是固定搭配，表示'显而易见的证据'。apparent 也有'明显'义，但 obvious 更强调'一眼看出'。",
        "knowledge_tags": json.dumps(["形容词辨析", "obvious"]),
        "source": "seed",
        "verified": 1,
    },
]

async def seed_if_empty():
    """仅在数据库为空时导入种子数据"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM vocabulary")
        (count,) = await cursor.fetchone()
        if count == 0:
            now = time.time()
            await db.executemany(
                """INSERT OR IGNORE INTO vocabulary
                   (word, phonetic, meaning, level, frequency, topic, examples)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(w[0], w[1], w[2], w[3], w[4], w[5], "[]")
                 for w in HIGH_FREQ_WORDS]
            )

        cursor = await db.execute("SELECT COUNT(*) FROM questions")
        (qcount,) = await cursor.fetchone()
        if qcount == 0:
            now = time.time()
            for q in SAMPLE_QUESTIONS:
                await db.execute(
                    """INSERT OR IGNORE INTO questions
                       (question_id, question_type, topic, difficulty,
                        content, options, answer, explanation,
                        knowledge_tags, source, verified, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (q["question_id"], q["question_type"], q["topic"],
                     q["difficulty"], q["content"], q["options"], q["answer"],
                     q["explanation"], q["knowledge_tags"], q["source"],
                     q["verified"], now)
                )
        await db.commit()