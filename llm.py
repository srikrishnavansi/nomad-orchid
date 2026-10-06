"""The only place the model is called. It plans and it writes prose; it never does arithmetic.

Key: ONLY ~/.config/jobhunt/anthropic.key. No env vars, no SDK profiles. Every call is pre-counted against a hard
per-run token cap, and every number in the model's prose must already exist in the facts code computed.
"""
import json
import os
import re
from pathlib import Path

MODEL = "claude-sonnet-5-5"
KEY_FILE = Path.home() / ".config" / "jobhunt" / "anthropic.key"
RUN_TOKEN_CAP = 20_000  # input + output (thinking included) across all calls in one agent run
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, cap=RUN_TOKEN_CAP):
        self.cap, self.spent, self.calls = cap, 0, []

    def reserve(self, input_tokens, max_tokens):
        if self.spent + input_tokens + max_tokens > self.cap:
            raise BudgetExceeded(f"call needs up to {input_tokens + max_tokens} tokens, {self.cap - self.spent} left of {self.cap}")

    def charge(self, name, usage):
        used = usage.input_tokens + usage.output_tokens
        self.spent += used
        self.calls.append({"step": name, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens})


def load_key():
    try:
        key = KEY_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    return key or None


def make_client(key):
    import anthropic
    # Scrub ANTHROPIC_* from this process so the SDK cannot pick up any credential or endpoint other than the key file.
    for name in [n for n in os.environ if n.startswith("ANTHROPIC_")]:
        os.environ.pop(name)
    return anthropic.Anthropic(api_key=key, base_url="https://api.anthropic.com", max_retries=1)


def ask_json(client, budget, step, system, user, schema, max_tokens):
    """One structured-output call, refused up front if it could break the run's token cap."""
    params = dict(model=MODEL, system=system, messages=[{"role": "user", "content": user}])
    fmt = {"format": {"type": "json_schema", "schema": schema}}
    counted = client.messages.count_tokens(**params, output_config=fmt).input_tokens
    budget.reserve(counted, max_tokens)
    resp = client.messages.create(**params, max_tokens=max_tokens, output_config={"effort": "low", **fmt})
    budget.charge(step, resp.usage)
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"{step}: model stopped with {resp.stop_reason}")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "in_scope": {"type": "boolean"},
        "date_from": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
        "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
        "customer_ids": {"type": "array", "items": {"type": "integer"}},
        "reading": {"type": "string"},
    },
    "required": ["in_scope", "date_from", "date_to", "customer_ids", "reading"],
    "additionalProperties": False,
}


def plan(client, budget, question, today, customers):
    system = (
        "You turn a question into a read-only lookup plan for an ERP pricing check. The check compares confirmed sales "
        "order lines with the trade list price and the customer's agreed rebate. Decide the order-date range and which "
        "customers the question is about (empty list = all the asker can see). in_scope is false if the question is not "
        "about orders priced off the list or off the rebate. `reading` is one short sentence saying how you read it. "
        f"Today is {today.isoformat()} ({today:%A}). Customers the asker can see (id: name): "
        + json.dumps({str(k): v for k, v in customers.items()})
    )
    return ask_json(client, budget, "plan", system, question, PLAN_SCHEMA, max_tokens=2000)


EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "draft_subject": {"type": "string"},
        "draft_body": {"type": "string"},
    },
    "required": ["summary", "draft_subject", "draft_body"],
    "additionalProperties": False,
}


def explain(client, budget, asker, question, facts, draft_brief):
    system = (
        f"You are Orchid, reading an ERP for {asker['name']} ({asker['role']}) at Liffey Fixings Ltd. Code has already "
        "fetched the records and done every calculation. Rules: copy numbers, dates and order names exactly as they appear "
        "in FACTS; never add, subtract, round or derive a new number; cite facts inline as [F1], [F2]. "
        "summary: 2 to 4 plain sentences answering the question. draft_subject/draft_body: the draft described in DRAFT "
        "for a person to review; it will not be sent by you. Sign the draft with the asker's name. No markdown."
    )
    user = f"QUESTION: {question}\n\nFACTS:\n{json.dumps(facts, indent=1)}\n\nDRAFT: {draft_brief}"
    return ask_json(client, budget, "explain", system, user, EXPLAIN_SCHEMA, max_tokens=4000)


def numbers_not_in(text, facts):
    """Numbers in the model's text that do not appear anywhere in the facts it was given (commas ignored)."""
    allowed = {n.replace(",", "") for n in NUM.findall(json.dumps(facts))}
    allowed |= {n.lstrip("0") or "0" for n in allowed}  # S00056 may be written as 56; dates as 6 Oct
    return sorted({n for n in NUM.findall(text) if n.replace(",", "") not in allowed and n.replace(",", "").lstrip("0") not in allowed})


def valid_fact_refs(text, n_facts):
    return all(1 <= int(i) <= n_facts for i in re.findall(r"\[F(\d+)\]", text))


if __name__ == "__main__":
    b = Budget(100)
    b.reserve(40, 60)
    try:
        b.reserve(41, 60)
        raise AssertionError("cap not enforced")
    except BudgetExceeded:
        pass
    facts = {"F1": {"order": "S00056", "variance": "1,234.50", "date": "2026-10-06"}}
    assert numbers_not_in("S00056 on 2026-10-06 was overcharged by 1,234.50 [F1]", facts) == []
    assert numbers_not_in("overcharged by 1,300.00 in total", facts) == ["1,300.00"]
    assert valid_fact_refs("see [F1]", 1) and not valid_fact_refs("see [F2]", 1)
    print("llm.py self-check ok")
