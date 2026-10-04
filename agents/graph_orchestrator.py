"""
基于 LangGraph 的智能规划层
把用户的自然语言请求路由到合适的 Agent，支持多步协作。
未装 LangGraph 时优雅降级。
"""
import logging
import time
from typing import TypedDict, Annotated
import operator

logger = logging.getLogger(__name__)

_AVAILABLE = False
try:
    from langgraph.graph import StateGraph, END
    _AVAILABLE = True
except ImportError:
    logger.info("[GraphOrchestrator] LangGraph 未安装，智能规划禁用")


class AgentState(TypedDict, total=False):
    user_request: str
    intent: str
    params: dict
    result: dict
    messages: Annotated[list, operator.add]
    iterations: int
    max_iterations: int


def build_graph(orchestrator, llm_client):
    """构建 LangGraph 状态图"""
    if not _AVAILABLE:
        return None

    async def router_node(state: AgentState):
        """LLM 解析意图"""
        iterations = state.get("iterations", 0) + 1
        prompt = f"""分析用户请求，输出意图标签。

可选意图：
- generate_question: 生成题目（练习）
- generate_from_wrong: 基于错题生成变式题
- diagnose: 学情诊断
- research: 联网检索
- translate: 翻译
- chat: 一般性对话（无明确任务）
- done: 任务已完成，无需继续

用户请求：{state['user_request']}

已执行轮次：{iterations}
如果已执行 ≥3 轮或任务明显已完成，输出 done。
只输出一个意图标签，不要任何解释。"""

        try:
            intent = await llm_client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2, max_tokens=50,
            )
            intent = intent.strip().lower().split()[0] if intent.strip() else "chat"
            for valid in ["generate_question", "generate_from_wrong",
                          "diagnose", "research", "translate", "chat", "done"]:
                if valid in intent:
                    intent = valid
                    break
            else:
                intent = "chat"
        except Exception as e:
            logger.warning(f"[Router] 意图解析失败: {e}")
            intent = "done"

        return {
            "intent": intent,
            "iterations": iterations,
            "max_iterations": state.get("max_iterations", 3),
        }

    async def question_node(state: AgentState):
        try:
            result = await orchestrator.generate_exam({
                "num_questions": 1,
                "question_type": state.get("params", {}).get(
                    "question_type", "reading_careful"
                ),
            })
            return {
                "result": result,
                "messages": [f"✅ 生成 {result['total']} 道题"],
            }
        except Exception as e:
            return {"messages": [f"❌ 出题失败: {e}"]}

    async def wrong_node(state: AgentState):
        try:
            result = await orchestrator.generate_from_wrong(
                state.get("params", {}).get("user_id", "anonymous"),
                num=state.get("params", {}).get("num", 3),
            )
            return {
                "result": result,
                "messages": [f"✅ 生成 {result['total']} 道变式题"],
            }
        except Exception as e:
            return {"messages": [f"❌ 变式题生成失败: {e}"]}

    async def diagnose_node(state: AgentState):
        try:
            result = await orchestrator.review_agent.execute(
                {"user_id": state.get("params", {}).get("user_id", "anonymous"),
                 "answers": []},
                task_id=f"graph_diag_{int(time.time())}",
            )
            return {
                "result": result,
                "messages": [f"📊 预测分数: {result.get('predicted_score')}"],
            }
        except Exception as e:
            return {"messages": [f"❌ 诊断失败: {e}"]}

    async def chat_node(state: AgentState):
        try:
            reply = await llm_client.chat([
                {"role": "system",
                 "content": "你是四六级备考助手，用中文回答。"},
                {"role": "user", "content": state["user_request"]},
            ], temperature=0.7, max_tokens=300)
            return {
                "result": {"type": "chat", "content": reply},
                "messages": [reply[:100]],
            }
        except Exception as e:
            return {"messages": [f"❌ 对话失败: {e}"]}

    def route_by_intent(state: AgentState):
        if state.get("iterations", 0) >= state.get("max_iterations", 3):
            return "end"
        intent = state.get("intent", "chat")
        if intent == "done":
            return "end"
        if intent == "generate_question":
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