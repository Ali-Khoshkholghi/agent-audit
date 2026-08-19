"""
Minimal 2-node LangGraph agent: fetches a stock price, then summarizes it.
Deliberately has no error handling around the network call — that's the
planted gap AgentAudit should eventually flag (relevant from M4 onward).
"""
from langgraph.graph import StateGraph, END
from typing import TypedDict


class State(TypedDict):
    ticker: str
    price: float
    summary: str


def fetch_price(state: State) -> State:
    # No try/except: a bad ticker or network failure crashes the whole graph.
    price = get_price_from_api(state["ticker"])
    return {**state, "price": price}


def summarize(state: State) -> State:
    return {**state, "summary": f"{state['ticker']} is at ${state['price']:.2f}"}


def get_price_from_api(ticker: str) -> float:
    raise NotImplementedError("stub for fixture purposes")


graph = StateGraph(State)
graph.add_node("fetch_price", fetch_price)
graph.add_node("summarize", summarize)
graph.set_entry_point("fetch_price")
graph.add_edge("fetch_price", "summarize")
graph.add_edge("summarize", END)

app = graph.compile()
