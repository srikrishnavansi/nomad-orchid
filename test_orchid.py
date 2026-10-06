"""Assert-based checks against the live local Odoo. Run: make test  (or .venv/bin/python test_orchid.py)

Admin is used here ONLY as an independent referee (ground truth, mail queue, posted entries). The agent never sees it.
"""
import json
import runpy
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import agent
import llm
import pricing
from odoo_rpc import OdooError, Session

Q = "Which orders this month were priced off the list price or off the agreed rebate?"
U = agent.USERS
admin = Session("admin", "admin")
truth = json.load(open("ground_truth.json"))
uid = {k: admin.search_read("res.users", [("login", "=", v["login"])], ["id"])[0]["id"] for k, v in U.items()}
passed = []


def test(fn):
    fn()
    passed.append(fn.__name__)
    print(f"ok  {fn.__name__}")
    return fn


def full_range_plan():
    first = min(o["date_order"] for o in admin.search_read("sale.order", [], ["date_order"]))[:10]
    return {"date_from": first, "date_to": date.today().isoformat(), "customer_ids": []}


def run_offline(user, **kw):
    """Agent run with the model forced off (the key file is never read)."""
    return agent.run(user, Q, use_llm=False, **kw)


@test
def pure_logic_self_checks():
    runpy.run_path("pricing.py", run_name="__main__")
    runpy.run_path("llm.py", run_name="__main__")


@test
def odoo_scopes_orders_by_user():
    for rep in ("aoife", "cian"):
        s = Session(U[rep]["login"], U[rep]["password"])
        seen = {o["id"] for o in s.search_read("sale.order", [], ["id"])}
        own = {o["id"] for o in admin.search_read("sale.order", [("user_id", "=", uid[rep])], ["id"])}
        assert seen == own and len(own) > 20, (rep, len(seen), len(own))
    aoife = Session(U["aoife"]["login"], U["aoife"]["password"])
    cian_order = admin.search_read("sale.order", [("user_id", "=", uid["cian"])], ["id"], limit=1)[0]["id"]
    try:
        aoife.read("sale.order", [cian_order], ["name"])
        raise AssertionError("Aoife could read Cian's order")
    except OdooError as e:
        assert e.kind == "AccessError"
    niamh = Session(U["niamh"]["login"], U["niamh"]["password"])
    assert niamh.call("sale.order", "search_count", []) == admin.call("sale.order", "search_count", []) == truth["orders"]


@test
def finds_exactly_the_seeded_mistakes_and_nothing_else():
    kind_to_issue = {"stale_list_price": "off_list", "typo_price": "off_list", "rebate_missed": "off_rebate", "rebate_too_high": "off_rebate",
                     "wrong_rule_applied": "off_rebate", "rebate_before_agreement": "off_rebate"}
    for user, expected in (("niamh", truth["mistakes"]), ("aoife", [m for m in truth["mistakes"] if m["rep"] == "aoife"])):
        s = agent.AgentSession(U[user]["login"], U[user]["password"])
        findings, skipped = pricing.check_lines(*agent.fetch(s, full_range_plan()))
        assert not skipped, skipped
        got = {f["_ids"]["line_id"]: f["issue"]["value"] for f in findings}
        want = {m["line_id"]: [kind_to_issue[m["kind"]]] for m in expected}
        assert got == want, (user, sorted(set(got) ^ set(want)))
    # the date-validity case: Kilmainham's 5% was given before the agreement started, so the agreed rebate is 0
    early = next(m for m in truth["mistakes"] if m["kind"] == "rebate_before_agreement")
    f = next(f for f in findings if f["_ids"]["line_id"] == early["line_id"])
    assert f["agreed_rebate_pct"]["value"] == 0.0 and "computed" in f["agreed_rebate_pct"]


@test
def answers_are_scoped_to_the_asker():
    a, n = run_offline("aoife", make_draft=False), run_offline("niamh", make_draft=False)
    aoife_orders = {o["id"] for o in admin.search_read("sale.order", [("user_id", "=", uid["aoife"])], ["id"])}
    a_lines = {f["_ids"]["line_id"] for f in a["findings"]}
    n_lines = {f["_ids"]["line_id"] for f in n["findings"]}
    assert a_lines and {f["_ids"]["order_id"] for f in a["findings"]} <= aoife_orders
    assert a_lines < n_lines, "finance should see Aoife's findings plus Cian's"
    assert all(f["salesperson"]["value"] == "Aoife Byrne" for f in a["findings"])
    assert any(f["salesperson"]["value"] == "Cian Walsh" for f in n["findings"])
    for ans, user in ((a, "aoife"), (n, "niamh")):
        assert ans["asker"]["uid"] == uid[user] and all("refused" not in c for c in ans["odoo_calls"]), "agent read as someone else"


@test
def every_cited_field_matches_odoo_when_reread_as_the_asker():
    for user in ("aoife", "niamh"):
        ans = run_offline(user, make_draft=False)
        s = Session(U[user]["login"], U[user]["password"])
        checked = 0
        for f in ans["findings"]:
            for name, v in f.items():
                if name.startswith("_"):
                    continue
                if "source" in v:
                    src = v["source"]
                    assert set(src) >= {"model", "id", "field", "href"}, src
                    live = s.read(src["model"], [src["id"]], [src["field"]])[0][src["field"]]
                    live = live[1] if isinstance(live, list) and len(live) == 2 else live
                    assert live == v["value"], (user, name, src, live, v["value"])
                    checked += 1
                else:
                    assert v["computed"] and all(i in f for i in v["inputs"]), (name, v)
            exp = pricing.money(Decimal(str(f["list_price"]["value"])) * (1 - Decimal(str(f["agreed_rebate_pct"]["value"])) / 100) * Decimal(str(f["qty"]["value"])))
            assert float(exp) == f["expected_subtotal"]["value"]
            assert float(pricing.money(f["charged_subtotal"]["value"]) - exp) == f["variance"]["value"]
        assert checked >= 10 * len(ans["findings"]) > 0


@test
def drafts_stay_drafts_and_nothing_is_sent():
    mail_before = admin.call("mail.mail", "search_count", [])
    a, n = run_offline("aoife"), run_offline("niamh")
    for ans in (a, n):
        assert ans["auto_sent"] == 0 and ans["draft"]
    # finance: a real draft credit note in Odoo, for exactly the overcharge on the target order
    d = n["draft"]
    mv = admin.read("account.move", [d["id"]], ["state", "move_type", "amount_total", "partner_id", "create_uid"])[0]
    assert d["kind"] == "credit_note" and mv["state"] == "draft" and mv["move_type"] == "out_refund"
    target = agent.pick_draft_target(n["findings"])
    assert abs(mv["amount_total"] - sum(f["variance"]["value"] for f in target["findings"] if f["variance"]["value"] > 0)) < 0.005
    assert mv["create_uid"][0] == uid["niamh"], "the draft must be created by the asker, not a superuser"
    # sales rep: Odoo itself refused the credit note, so the draft is an email kept as a to-do on her own order
    d = a["draft"]
    act = admin.read("mail.activity", [d["id"]], ["res_model", "res_id", "user_id", "activity_type_id"])[0]
    assert d["kind"] == "email" and "AccessError" in d["credit_note_refused"]
    assert act["res_model"] == "sale.order" and act["user_id"][0] == uid["aoife"] and act["activity_type_id"][1] == "Email"
    # nothing left the building: no mail queued, nothing posted, no order changed state
    assert admin.call("mail.mail", "search_count", []) == mail_before
    assert admin.call("account.move", "search_count", [("state", "=", "posted")]) == 0
    assert admin.call("sale.order", "search_count", [("state", "!=", "sale")]) == 0
    # re-running updates the same drafts instead of piling up new ones
    again = run_offline("niamh")
    assert again["draft"]["id"] == n["draft"]["id"] and again["draft"]["reused"]


@test
def agent_session_cannot_send_post_or_confirm():
    s = agent.AgentSession(U["niamh"]["login"], U["niamh"]["password"])
    move = admin.search_read("account.move", [("move_type", "=", "out_refund")], ["id"], limit=1)[0]["id"]
    order = admin.search_read("sale.order", [], ["id"], limit=1)[0]["id"]
    for model, method, args in (("account.move", "action_post", [[move]]), ("sale.order", "message_post", [[order]]),
                                ("sale.order", "action_quotation_send", [[order]]), ("account.move", "write", [[move], {"state": "posted"}]),
                                ("account.move", "create", [{"move_type": "out_invoice"}]), ("res.partner", "write", [[1], {"name": "x"}])):
        try:
            s.call(model, method, *args)
            raise AssertionError(f"{model}.{method} was allowed")
        except PermissionError:
            pass
    assert not s.calls, "blocked calls must never reach Odoo"
    assert admin.read("account.move", [move], ["state"])[0]["state"] == "draft"


def fake_client(plan_json, explain_json, input_tokens=300):
    """Stands in for the Anthropic client so the token cap and the number guard can be tested without a key."""
    replies = iter([plan_json, explain_json])
    usage = NS(input_tokens=input_tokens, output_tokens=200)
    return NS(messages=NS(count_tokens=lambda **kw: NS(input_tokens=input_tokens),
                          create=lambda **kw: NS(usage=usage, stop_reason="end_turn", content=[NS(type="text", text=json.dumps(next(replies)))])))


@test
def model_prose_is_number_checked_and_token_capped():
    today = date.today()
    plan = {"in_scope": True, "date_from": today.replace(day=1).isoformat(), "date_to": today.isoformat(), "customer_ids": [], "reading": "this month"}
    real_load, real_make = llm.load_key, llm.make_client
    llm.load_key = lambda: "not-a-real-key"
    try:
        # 1) prose that invents a number is rejected; the deterministic summary is used instead
        llm.make_client = lambda k: fake_client(plan, {"summary": "Total leakage was 99999.99 EUR [F1].", "draft_subject": "x", "draft_body": "y"})
        ans = agent.run("aoife", Q, make_draft=False)
        assert ans["summary"]["by"] == "template" and any("99999.99" in n for n in ans["notes"]), ans["notes"]
        assert ans["plan"]["planned_by"] == llm.MODEL
        # 2) prose that only copies numbers from the facts is accepted
        f1 = run_offline("aoife", make_draft=False)["findings"][0]
        ok = {"summary": f"{f1['order']['value']} differs by {f1['variance']['value']} [F1].", "draft_subject": "Pricing", "draft_body": "Hi, see the order."}
        llm.make_client = lambda k: fake_client(plan, ok)
        ans = agent.run("aoife", Q, make_draft=False)
        assert ans["summary"]["by"] == llm.MODEL, ans["notes"]
        assert ans["llm"]["tokens_used"] == 1000 <= ans["llm"]["token_cap"]
        # 3) a call that could break the hard cap is never made; the run still answers deterministically
        llm.make_client = lambda k: fake_client(plan, ok, input_tokens=llm.RUN_TOKEN_CAP)
        ans = agent.run("aoife", Q, make_draft=False)
        assert ans["llm"]["tokens_used"] == 0 and ans["summary"]["by"] == "template" and ans["plan"]["planned_by"] == "rules"
        assert any("left of" in n for n in ans["notes"]), ans["notes"]
        # 4) a plan naming a customer the asker cannot see is refused
        cian_customer = admin.search_read("sale.order", [("user_id", "=", uid["cian"])], ["partner_id"], limit=1)[0]["partner_id"][0]
        llm.make_client = lambda k: fake_client(plan | {"customer_ids": [cian_customer]}, ok)
        ans = agent.run("aoife", Q, make_draft=False)
        assert ans["plan"]["planned_by"] == "rules" and any("cannot see" in n for n in ans["notes"])
    finally:
        llm.load_key, llm.make_client = real_load, real_make


@test
def rule_planner_reads_common_ranges():
    today = date(2026, 10, 6)
    c = {9: "Brennan Builders", 10: "Dockside Construction"}
    p = agent.rule_plan("which orders last month were off the rebate for Dockside?", today, c)
    assert (p["date_from"], p["date_to"], p["customer_ids"], p["in_scope"]) == ("2026-09-01", "2026-09-30", [10], True)
    assert agent.rule_plan(Q, today, c)["date_from"] == "2026-10-01"
    assert agent.rule_plan("any pricing issues in August?", today, c)["date_to"] == "2026-08-31"
    assert not agent.rule_plan("what's the weather?", today, c)["in_scope"]


@test
def agent_never_touches_admin_and_runs_hold_no_secrets():
    src = Path("agent.py").read_text() + Path("llm.py").read_text() + Path("server.py").read_text()
    assert "admin" not in src.lower().replace("administrator", "") and "import seed" not in src
    for p in Path("runs").glob("*.json"):
        t = p.read_text()
        assert "sk-ant" not in t and not any(u["password"] in t for u in U.values()), p


print(f"\nALL {len(passed)} TESTS PASSED")
