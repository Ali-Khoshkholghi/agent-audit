# Ground truth for this fixture

- Framework: LangGraph, `StateGraph`
- Nodes: 2 — `fetch_price`, `summarize`
- Entry point: `fetch_price`
- Flow: fetch_price -> summarize -> END (linear, no branching)
- Tools/external calls: one, `get_price_from_api` (unimplemented stub)
- Known gap: no error handling around the API call in `fetch_price`

M1's `inspect` output is "correct" if it identifies LangGraph, names both
nodes, and describes the linear flow. It does not need to catch the missing
error handling yet — that's a later milestone's job.
