"""
基于 LangGraph 的智能规划层
把用户的自然语言请求路由到合适的 Agent，支持多步协作。
未装 LangGraph 时优雅降级。
"""
import json
import logging
import time
import re
from typing import TypedDict, Annotated
import operator

logger = logging.getLogger(__name__)

_AVAILABLE = False
try:
    from langgraph.graph import StateGraph, END
    _AVAILABLE = True
except ImportError:
    logger.info("[GraphOrchestrator] LangGraph 未安装，智能规划禁用")


# 支持的题型（用户输入 → 内部代码）
QUESTION_TYPE_MAP = {
    "语法": "grammar", "语法题": "grammar", "grammar": "grammar",
    "词汇": "vocab", "词汇辨析": "vocab", "vocab": "vocab",
    "阅读": "reading_careful", "仔细阅读": "reading_careful",
    "阅读理解": "reading_careful", "reading": "reading_careful",
    "选词填空": "reading_selection", "长篇匹配": "reading_matching",
    "听力": "listening_news", "短篇新闻": "listening_news",
    "长对话": "listening_conversation", "听力篇章": "listening_passage",
    "翻译": "translation", "汉译英": "translation", "translation": "translation",
    "写作": "writing", "作文": "writing", "writing": "writing",
    "综合": "comprehensive", "综合训练": "comprehensive",
}

# 内部题型代码 → 中文名（用于显示）
QTYPE_CN = {
    "grammar": "语法结构", "vocab": "词汇辨析",
    "reading_careful": "阅读-仔细阅读", "reading_selection": "阅读-选词填空",
    "reading_matching": "阅读-长篇匹配",
    "listening_news": "听力-短篇新闻", "listening_conversation": "听力-长对话",
    "listening_passage": "听力-听力篇章",
    "translation": "翻译-汉译英", "writing": "写作",
    "comprehensive": "综合训练",
}


class AgentState(TypedDict, total=False):
    user_request: str
    intent: str
    params: dict
    result: dict
    messages: Annotated[list, operator.add]
    iterations: int
    max_iterations: int
    executed_intents: list    # 记录已执行过的意图，避免重复


def build_graph(orchestrator, llm_client):
    """构建 LangGraph 状态图"""
    if not _AVAILABLE:
        return None

    async def router_node(state: AgentState):
        """LLM 解析意图 + 提取参数"""
        iterations = state.get("iterations", 0) + 1
        executed = state.get("executed_intents", [])
        user_req = state.get("user_request", "")

        # 已执行过 generate_question，就不要再执行了
        already_done = "generate_question" in executed

        prompt = f"""分析用户请求，输出 JSON。**只输出 JSON，不要任何 markdown 标记**。

格式：
{{
  "intent": "generate_question | generate_from_wrong | diagnose | chat | done",
  "params": {{
    "num": 题目数量（整数，默认1）,
    "question_type": 从以下选一个：grammar/vocab/reading_careful/reading_selection/reading_matching/listening_news/listening_conversation/listening_passage/translation/writing/comprehensive
  }}
}}

【用户请求】{user_req}
【已执行轮次】{iterations}
【已执行过的意图】{executed}
【本轮已生成题目】{"是" if already_done else "否"}

【判断规则】
1. 用户要"生成/出题/来几道题" → intent="generate_question"，并从请求中提取数量和题型
   - "2道语法题" → num=2, question_type="grammar"
   - "3道阅读" → num=3, question_type="reading_careful"
   - "来几道题" → num=3, question_type="comprehensive"
2. 用户要"错题变式题" → intent="generate_from_wrong"
3. 用户要"诊断/预测分数/学情" → intent="diagnose"
4. 其他闲聊/问答 → intent="chat"
5. ★ 如果【本轮已生成题目】= "是"，则 intent 必须是 "done"
6. ★ 如果【已执行轮次】≥ 2，则 intent 必须是 "done"

只输出 JSON。"""

        try:
            raw = await llm_client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=200,
            )
            raw = raw.strip()
            # 去掉 markdown 代码块
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw)
            data = json.loads(raw)
            intent = data.get("intent", "chat")
            params = data.get("params") or {}

            # 校验意图
            valid_intents = ["generate_question", "generate_from_wrong",
                             "diagnose", "chat", "done"]
            if intent not in valid_intents:
                intent = "chat"

            # 校验题型
            qtype = params.get("question_type", "comprehensive")
            if qtype not in QTYPE_CN:
                qtype = "comprehensive"

            # 校验数量
            try:
                num = int(params.get("num", 1))
                num = max(1, min(num, 5))  # 限制 1-5 题
            except Exception:
                num = 1

            params["question_type"] = qtype
            params["num"] = num

        except Exception as e:
            logger.warning(f"[Router] 解析失败: {e}，降级为 done")
            intent = "done"
            params = {}

        return {
            "intent": intent,
            "params": params,
            "iterations": iterations,
            "max_iterations": state.get("max_iterations", 3),
            "executed_intents": executed + ([intent] if intent == "generate_question" else []),
        }

    async def question_node(state: AgentState):
        params = state.get("params", {})
        num = params.get("num", 1)
        qtype = params.get("question_type", "comprehensive")
        qtype_cn = QTYPE_CN.get(qtype, qtype)

        try:
            result = await orchestrator.generate_exam({
                "num_questions": num,
                "question_type": qtype,
            })
            return {
                "result": result,
                "messages": [f"✅ 生成 {result['total']} 道 {qtype_cn}"],
            }
        except Exception as e:
            logger.error(f"[QuestionNode] 失败: {e}")
            return {"messages": [f"❌ 出题失败: {e}"]}

    async def wrong_node(state: AgentState):
        params = state.get("params", {})
        num = params.get("num", 3)
        try:
            result = await orchestrator.generate_from_wrong(
                params.get("user_id", "anonymous"), num=num,
            )
            return {
                "result": result,
                "messages": [f"✅ 生成 {result['total']} 道错题变式题"],
            }
        except Exception as e:
            return {"messages": [f"❌ 变式题生成失败: {e}"]}

    async def diagnose_node(state: AgentState):
        params = state.get("params", {})
        try:
            result = await orchestrator.review_agent.execute(
                {"user_id": params.get("user_id", "anonymous"), "answers": []},
                task_id=f"graph_diag_{int(time.time())}",
            )
            return {
                "result": result,
                "messages": [f"📊 预测分数: {result.get('predicted_score')}，"
                             f"过线概率: {(result.get('pass_probability', 0) * 100):.0f}%"],
            }
        except Exception as e:
            return {"messages": [f"❌ 诊断失败: {e}"]}

    async def chat_node(state: AgentState):
        try:
            reply = await llm_client.chat([
                {"role": "system",
                 "content": "你是四六级备考助手，用中文简洁回答。"},
                {"role": "user", "content": state["user_request"]},
            ], temperature=0.7, max_tokens=300)
            return {
                "result": {"type": "chat", "content": reply},
                "messages": [reply[:200]],
            }
        except Exception as e:
            return {"messages": [f"❌ 对话失败: {e}"]}

    def route_by_intent(state: AgentState):
        """条件路由"""
        intent = state.get("intent", "chat")

        # 上限保护
        if state.get("iterations", 0) >= state.get("max_iterations", 3):
            return "end"

        if intent == "generate_question":
            # 已经生成过了 → 结束
            if state.get("executed_intents", []).count("generate_question") >= 2:
                return "end"
            return "question"
        if intent == "generate_from_wrong":
            return "wrong"
        if intent == "diagnose":
            return "diagnose"
        if intent == "chat":
            return "chat"
        return "end"

    graph = StateGraph(AgentState)
    graph.add_node("router", router_node)
    graph.add_node("question", question_node)
    graph.add_node("wrong", wrong_node)
    graph.add_node("diagnose", diagnose_node)
    graph.add_node("chat", chat_node)

    graph.set_entry_point("router")
    graph.add_conditional_edges("router", route_by_intent, {
        "question": "question",
        "wrong": "wrong",
        "diagnose": "diagnose",
        "chat": "chat",
        "end": END,
    })

    # 执行完后回到 router 判断是否结束
    graph.add_edge("question", "router")
    graph.add_edge("wrong", "router")
    graph.add_edge("diagnose", "router")
    graph.add_edge("chat", END)

    return graph.compile()


_graph_instance = None


def get_graph(orchestrator, llm_client):
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = build_graph(orchestrator, llm_client)
    return _graph_instance


def is_enabled() -> bool:
    return _AVAILABLE