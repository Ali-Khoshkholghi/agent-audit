---
name: severity-rubric
description: AgentAudit's official severity policy — determine a Finding's severity from its category. Use whenever assigning or reconsidering a Finding's severity field.
---

# AgentAudit severity rubric

Use this table to assign the `severity` field of a Finding. Look up the
finding's `category` and use EXACTLY the value shown — do not adjust it
up or down based on your own judgement of how bad the issue seems.

| category            | severity |
| -------------------- | -------- |
| error-handling        | high     |
| prompt-injection       | critical |
| tool-misuse            | high     |
| data-exposure          | critical |
| resource-exhaustion    | medium   |
| other                  | low      |

A category not listed above gets `info`.

When asked to determine a severity, respond with exactly one of:
`critical`, `high`, `medium`, `low`, `info` — the value from the table,
nothing else.
