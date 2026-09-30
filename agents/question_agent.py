import json
import time
import random
from agents.base import BaseAgent
from services.llm_client import llm_client

# ==================== 话题池 ====================
TOPIC_POOL = [
    "教育", "科技", "环境", "经济", "社会",
    "心理", "健康", "文化", "职场", "学术",
]

# ==================== 题型定义（按四六级真题结构） ====================
# key: 内部标识
# value: (显示名, 所属部分, 出题规范, 真题样例, 长度区间)
QUESTION_TYPES = {
    # ========== Part I: Writing ==========
    "writing": (
        "写作",
        "Part I 写作",
        "【格式要求】给出一个作文题目（如 'On the Importance of ...'），"
        "要求考生在30分钟内写出120-180词的短文。返回题目 + 3条写作要点提示。"
        "【输出】question 字段为题目，options 字段为空数组 []，"
        "answer 字段为 \"WRITING\"，explanation 字段为3条写作要点。",
        "【真题样例】\n"
        "Directions: For this part, you are allowed 30 minutes to write a short essay "
        "on the importance of reading classics. You should write at least 120 words "
        "but no more than 180 words.\n"
        "答案：WRITING\n"
        "要点：1) 引言指出阅读经典的意义 2) 主体论述2-3个理由 3) 结论呼应主题",
        (50, 500)
    ),

    # ========== Part II: Listening ==========
    "listening_news": (
        "听力-短篇新闻",
        "Part II Listening Section A",
        "【格式要求】给出一段 80-120 词的英文新闻短文（四级短篇新闻），"
        "然后出一道理解题。选项四选一。"
        "【输出】question 字段为「短文 + 题干」的完整文本（用 \\n\\n 分隔），"
        "options 为四个选项，answer 为 A/B/C/D。",
        "【真题样例】\n"
        "A fire broke out in a shopping mall in the city center yesterday evening. "
        "Firefighters arrived within 10 minutes and brought the situation under control. "
        "No injuries were reported. Authorities remind citizens to check electrical wiring regularly.\n\n"
        "What is the main cause of the fire according to the authorities?\n"
        "A. Electrical wiring problems\n"
        "B. Cooking accidents\n"
        "C. Cigarettes\n"
        "D. Arson",
        (300, 1500)
    ),
    "listening_conversation": (
        "听力-长对话",
        "Part II Listening Section B",
        "【格式要求】给出一段 150-200 词的英文对话（两人交替发言，用 A: / B: 标记），"
        "然后出一道理解题。"
        "【输出】question 为「对话 + 题干」，options 为四个选项。",
        "【真题样例】\n"
        "A: Hi Tom, how was your weekend?\n"
        "B: I went hiking with my family. The view was amazing.\n"
        "A: Did you take many photos?\n"
        "B: Yes, I took over 100 photos. I'll share some with you.\n\n"
        "What did the man do on the weekend?\n"
        "A. Stayed at home\n"
        "B. Went hiking\n"
        "C. Visited a museum\n"
        "D. Watched a movie",
        (400, 2000)
    ),
    "listening_passage": (
        "听力-听力篇章",
        "Part II Listening Section C",
        "【格式要求】给出一段 200-250 词的英文听力篇章（独白形式），"
        "然后出一道理解题。"
        "【输出】question 为「篇章 + 题干」，options 为四个选项。",
        "【真题样例】\n"
        "In recent years, remote work has become increasingly common among companies. "
        "Many employees appreciate the flexibility...\n\n"
        "What is the main idea of the passage?\n"
        "A. Remote work benefits\n"
        "B. Office culture\n"
        "C. Commuting problems\n"
        "D. Work-life balance",
        (500, 2500)
    ),

    # ========== Part III: Reading ==========
    "reading_selection": (
        "阅读-选词填空",
        "Part III Reading Section A",
        "【格式要求】给出一段 200-250 词的英文短文，挖掉 1 个空格（用 ______ 表示），"
        "四选一。四个选项词性一致。"
        "【输出】question 为短文+题干，options 为四个选项。",
        "【真题样例】\n"
        "Researchers have found that regular exercise can ______ cognitive decline in older adults.\n"
        "A. accelerate  B. delay  C. ignore  D. prevent\n"
        "答案：B",
        (300, 2000)
    ),
    "reading_matching": (
        "阅读-长篇匹配",
        "Part III Reading Section B",
        "【格式要求】给出一段 300-400 词的英文短文（含 5 个带编号段落 A-E），"
        "然后出一道『哪个段落最符合以下陈述』的匹配题。"
        "【输出】question 为短文+题干，options 为 A/B/C/D/E 段落编号（4个选项）。",
        "【真题样例】\n"
        "A. Many companies are adopting flexible work arrangements...\n"
        "B. Remote workers report higher job satisfaction...\n"
        "C. However, remote work also poses communication challenges...\n"
        "D. Team collaboration requires intentional effort...\n\n"
        "Which paragraph mentions the challenge of remote communication?\n"
        "A. Paragraph A  B. Paragraph B  C. Paragraph C  D. Paragraph D",
        (400, 3000)
    ),
    "reading_careful": (
        "阅读-仔细阅读",
        "Part III Reading Section C",
        "【格式要求】给出一段 200-300 词的英文短文，然后出一道理解题（主旨/细节/推断），"
        "题干以问号结尾。"
        "【输出】question 为短文+题干，options 为四个选项。",
        "【真题样例】\n"
        "A recent study published in Nature suggests that spending time in nature "
        "can significantly improve mental health...\n\n"
        "What is the main finding of the recent study?\n"
        "A. Nature reduces stress\n"
        "B. Cities are unhealthy\n"
        "C. Exercise is important\n"
        "D. Mental health is genetic",
        (400, 2500)
    ),

    # ========== Part IV: Translation ==========
    "translation": (
        "翻译-汉译英",
        "Part IV Translation",
        "【格式要求】给出一句中文（30-50 字，涉及中国传统文化/社会发展/科技教育等话题），"
        "考生需手写或语音输入英文译文。"
        "【输出】question 字段为中文原句，options 为空数组 []，"
        "answer 字段为参考答案（英文译文），"
        "explanation 字段为译文要点和关键短语。",
        "【真题样例】\n"
        "中文：中国越来越重视环境保护，许多城市已采取有效措施减少空气污染。\n"
        "答案：China is paying more and more attention to environmental protection, "
        "and many cities have taken effective measures to reduce air pollution.\n"
        "要点：pay attention to 注意；take measures 采取措施；air pollution 空气污染",
        (15, 400)
    ),

    # ========== 综合 ==========
    "comprehensive": (
        "综合训练",
        "随机",
        "从上述题型中随机选一种出题。",
        "参照具体题型样例。",
        (15, 3000)
    ),
}

# ==================== 题型显示顺序 ====================
QUESTION_ORDER = [
    "writing",
    "listening_news", "listening_conversation", "listening_passage",
    "reading_selection", "reading_matching", "reading_careful",
    "translation",
    "comprehensive",
]

# ==================== 各题型可选题量 ====================
QUESTION_COUNTS = {
    "writing": [1],
    "listening_news": [1, 2, 3, 5],
    "listening_conversation": [1, 2, 3],
    "listening_passage": [1, 2, 3],
    "reading_selection": [1, 2, 3],
    "reading_matching": [1, 2, 3],
    "reading_careful": [1, 2, 3, 5],
    "translation": [1, 2, 3],
    "comprehensive": [3, 5, 10],
}

# ==================== 长度区间（兜底校验） ====================
LENGTH_RANGE = {
    "写作": (30, 800),
    "听力-短篇新闻": (200, 2000),
    "听力-长对话": (300, 2500),
    "听力-听力篇章": (400, 3000),
    "阅读-选词填空": (200, 2000),
    "阅读-长篇匹配": (300, 3500),
    "阅读-仔细阅读": (300, 3000),
    "翻译-汉译英": (10, 500),
    "综合训练": (10, 3500),
}

PROMPT_TEMPLATE = """你是英语四六级出题专家，深谙 2015-2025 年 CET-4/CET-6 真题的命题风格。

【本次题型】{qtype_cn} （{qtype_section}）

【出题规范】
{qtype_desc}

{qtype_sample}

【本次出题约束】
1. 话题领域：**{topic}**（必须围绕此话题）
2. 考察知识点：**{knowledge_point}**（必须考察）
3. 难度：{difficulty}（1-5，5最难）
4. 唯一标识：{seed}
5. ★★★ 本题必须与此前生成的所有题目完全不同 ★★★

【严格输出格式】只输出以下 JSON，不要任何 markdown 标记：
{{
    "question": "题目内容",
    "options": ["A选项", "B选项", "C选项", "D选项"],
    "answer": "A",
    "explanation": "解析",
    "knowledge_tags": ["考点1", "考点2"],
    "difficulty": {difficulty}
}}"""


class QuestionAgent(BaseAgent):
    """智能出题Agent — 覆盖四六级真题全部题型"""

    def __init__(self):
        super().__init__(name="QuestionAgent", max_retries=2, idempotent=False)

    async def _run(self, task: dict) -> dict:
        qtype_key = task.get("question_type", "reading_careful")
        qtype_cn, qtype_section, qtype_desc, qtype_sample, _ = QUESTION_TYPES.get(
            qtype_key, QUESTION_TYPES["comprehensive"]
        )

        topic = task.get("topic") or random.choice(TOPIC_POOL)
        knowledge_point = task.get("knowledge_point") or qtype_cn

        prompt = PROMPT_TEMPLATE.format(
            qtype_cn=qtype_cn,
            qtype_section=qtype_section,
            qtype_desc=qtype_desc,
            qtype_sample=qtype_sample,
            topic=topic,
            knowledge_point=knowledge_point,
            difficulty=task.get("difficulty", 3),
            seed=task.get("seed", int(time.time() * 1000)),
        )

        content = await llm_client.chat([
            {"role": "system",
             "content": f"你是专业的四六级出题助手，只输出JSON。当前题型：{qtype_cn}。"},
            {"role": "user", "content": prompt},
        ], temperature=0.9)

        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(content)
        result["question_type"] = qtype_cn
        result["question_type_key"] = qtype_key
        result["section"] = qtype_section
        result["topic"] = topic
        return result

    def _validate_output(self, result: dict):
        for field in ["question", "options", "answer", "explanation"]:
            if field not in result:
                raise ValueError(f"输出缺少字段: {field}")

        qtype_cn = result.get("question_type", "")
        stem = result["question"]
        stem_len = len(stem)

        # 写作和翻译允许空 options
        if qtype_cn not in ("写作", "翻译-汉译英"):
            if result["answer"] not in ["A", "B", "C", "D"]:
                raise ValueError(f"答案格式错误: {result['answer']}")
            if len(result["options"]) != 4:
                raise ValueError(f"选项数量不为4，实际 {len(result['options'])}")

        min_len, max_len = LENGTH_RANGE.get(qtype_cn, (10, 3500))
        if stem_len < min_len:
            raise ValueError(f"题型「{qtype_cn}」题干过短 ({stem_len} < {min_len})")
        if stem_len > max_len:
            raise ValueError(f"题型「{qtype_cn}」题干过长 ({stem_len} > {max_len})")

        # 阅读/听力/选词填空必须有挖空或问句
        if qtype_cn in ("阅读-选词填空",):
            if "______" not in stem and "___" not in stem:
                raise ValueError("选词填空题必须包含挖空标记")
        if qtype_cn in ("阅读-仔细阅读", "听力-短篇新闻", "听力-长对话", "听力-听力篇章"):
            if "?" not in stem:
                raise ValueError(f"{qtype_cn}必须有以问号结尾的题干")