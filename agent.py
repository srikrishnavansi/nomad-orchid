"""Orchid reading phase: answer a pricing question as the asker, with a citation on every field and one draft.

    python agent.py --user aoife "Which orders this month were priced off the list price or off the agreed rebate?"

Reads Odoo only through the asker's own login, so Odoo's access rules decide what they can see. The model (if a key
is present) plans the lookup and writes the prose; code fetches, computes and creates the draft. Nothing is sent.
"""
import argparse
import calendar
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import llm
import pricing
from odoo_rpc import URL, OdooError, Session

USERS = {k: v for k, v in json.load(open(Path(__file__).with_name("demo_users.json"))).items() if not k.startswith("_")}
DRAFT_MODELS = {"account.move", "mail.activity"}
READ_METHODS = {"search_read", "read", "search_count", "fields_get"}
# Anything that could make something leave the building. Blocked before it reaches Odoo; counted for auto-sent.
SEND_METHODS = {"action_post", "message_post", "send_mail", "action_send_and_print", "action_quotation_send",
                "action_invoice_sent", "action_confirm", "action_done", "action_feedback", "_action_done"}


class AgentSession(Session):
    """The asker's session with a hard allowlist: reads anywhere, writes only drafts, never a send/confirm/post."""

    def call(self, model, method, *args, **kw):
        if method in READ_METHODS:
            return super().call(model, method, *args, **kw)
        if method == "activity_schedule" and model == "sale.order":
            return super().call(model, method, *args, **kw)
        if method in ("create", "write") and model in DRAFT_MODELS:
            vals = args[-1] if args else {}
            if "state" in vals or vals.get("move_type", "out_refund") != "out_refund":
                raise PermissionError(f"agent may only write draft credit notes, got {vals.get('move_type')}/{vals.get('state')}")
            return super().call(model, method, *args, **kw)
        raise PermissionError(f"agent is not allowed to call {model}.{method}")


def odoo_link(model, rid):
    return f"{URL}/odoo/{model}/{rid}"


# ---------- planning ----------

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def rule_plan(question, today, customers):
    """Deterministic planner used when no model key is present (and in tests)."""
    q = question.lower()
    first = today.replace(day=1)
    if "last month" in q or "previous month" in q:
        end = first - timedelta(days=1)
        rng = (end.replace(day=1), end)
    elif "last week" in q:
        start = today - timedelta(days=today.weekday() + 7)
        rng = (start, start + timedelta(days=6))
    elif "this week" in q:
        rng = (today - timedelta(days=today.weekday()), today)
    elif m := re.search(r"last (\d+) days", q):
        rng = (today - timedelta(days=int(m.group(1)) - 1), today)
    elif m := next((mo for mo in MONTHS if re.search(rf"\b{mo}\b", q)), None):
        y = today.year if MONTHS[m] <= today.month else today.year - 1
        rng = (date(y, MONTHS[m], 1), date(y, MONTHS[m], calendar.monthrange(y, MONTHS[m])[1]))
    else:
        rng = (first, today)  # "this month" and the default
    ids = [cid for cid, name in customers.items() if name.split()[0].lower() in q]
    in_scope = bool(re.search(r"pric|rebate|discount|list|overcharg|undercharg", q))
    return {"in_scope": in_scope, "date_from": rng[0].isoformat(), "date_to": rng[1].isoformat(), "customer_ids": ids,
            "reading": f"Confirmed orders dated {rng[0]} to {rng[1]}" + (f" for customer ids {ids}" if ids else "") + "."}


def validate_plan(p, customers):
    d0, d1 = date.fromisoformat(p["date_from"]), date.fromisoformat(p["date_to"])
    if d1 < d0 or (d1 - d0).days > 400:
        raise ValueError(f"bad date range {d0}..{d1}")
    unknown = set(p["customer_ids"]) - set(customers)
    if unknown:
        raise ValueError(f"plan names customers the asker cannot see: {sorted(unknown)}")
    return p


# ---------- reading (as the asker) ----------

def fetch(s, plan):
    domain = [("state", "=", "sale"), ("date_order", ">=", f"{plan['date_from']} 00:00:00"),
              ("date_order", "<=", f"{plan['date_to']} 23:59:59")]
    if plan["customer_ids"]:
        domain.append(("partner_id", "in", plan["customer_ids"]))
    orders = {o["id"]: o for o in s.search_read("sale.order", domain, ["name", "date_order", "partner_id", "user_id"], order="date_order")}
    lines = s.search_read("sale.order.line", [("order_id", "in", list(orders)), ("display_type", "=", False)],
                          ["order_id", "product_id", "product_uom_qty", "price_unit", "discount", "price_subtotal"], order="id") if orders else []
    products = {p["id"]: p for p in s.read("product.product", {l["product_id"][0] for l in lines}, ["product_tmpl_id", "categ_id"])} if lines else {}
    categ_parents = {c["id"]: c["parent_id"] and c["parent_id"][0] for c in s.search_read("product.category", [], ["parent_id"])}
    partners = {p["id"]: p for p in s.read("res.partner", {o["partner_id"][0] for o in orders.values()}, ["name", "property_product_pricelist"])} if orders else {}
    item_fields = ["pricelist_id", "applied_on", "compute_price", "fixed_price", "percent_price", "base", "base_pricelist_id",
                   "date_start", "date_end", "min_quantity", "product_tmpl_id", "product_id", "categ_id"]
    agreements = {p["property_product_pricelist"][0] for p in partners.values() if p["property_product_pricelist"]}
    items = s.search_read("product.pricelist.item", [("pricelist_id", "in", list(agreements))], item_fields) if agreements else []
    bases = {i["base_pricelist_id"][0] for i in items if i["base_pricelist_id"]}
    items += s.search_read("product.pricelist.item", [("pricelist_id", "in", list(bases))], item_fields) if bases else []
    by_pl = {}
    for i in items:
        by_pl.setdefault(i["pricelist_id"][0], []).append(i)
    return orders, lines, products, categ_parents, partners, by_pl


# ---------- drafting ----------

def pick_draft_target(findings):
    """The order with the largest net variance. Overcharged -> credit note; undercharged -> email."""
    by_order = {}
    for f in findings:
        by_order.setdefault(f["_ids"]["order_id"], []).append(f)
    if not by_order:
        return None
    oid, fs = max(by_order.items(), key=lambda kv: abs(sum(f["variance"]["value"] for f in kv[1])))
    net = float(sum(pricing.money(f["variance"]["value"]) for f in fs))
    return {"order_id": oid, "order": fs[0]["order"]["value"], "customer": fs[0]["customer"]["value"],
            "partner_id": fs[0]["_ids"]["partner_id"], "net_variance": net, "findings": fs}


def create_draft(s, target, subject, body, today):
    """One draft in Odoo as the asker. Try a draft credit note when the customer was overcharged; if Odoo refuses
    (a sales rep has no right to), fall back to a draft email kept as an Email to-do on the order. Re-runs update
    the asker's own draft instead of piling up new ones (never someone else's draft)."""
    order, html = target["order"], "<p>" + body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    refused = None
    if target["net_variance"] > 0:
        ref = f"Orchid draft: {order} pricing correction"
        lines = [(0, 0, {"name": f"{order} / {f['product']['value']}: price correction ({', '.join(f['issue']['value'])})",
                         "quantity": 1, "price_unit": f["variance"]["value"], "tax_ids": [(6, 0, [])]})
                 for f in target["findings"] if f["variance"]["value"] > 0]
        try:
            existing = s.search_read("account.move", [("move_type", "=", "out_refund"), ("state", "=", "draft"), ("ref", "=", ref), ("create_uid", "=", s.uid)], ["id"])
            if existing:
                mid = existing[0]["id"]
                s.call("account.move", "write", [mid], {"narration": html})
            else:
                mid = s.call("account.move", "create", {"move_type": "out_refund", "partner_id": target["partner_id"], "invoice_origin": order,
                                                        "ref": ref, "narration": html, "invoice_line_ids": lines})
            rec = s.read("account.move", [mid], ["state", "amount_total", "ref"])[0]
            return {"kind": "credit_note", "model": "account.move", "id": mid, "state": rec["state"], "amount": rec["amount_total"],
                    "reused": bool(existing), "subject": subject, "body": body, "url": odoo_link("account.move", mid)}
        except OdooError as e:
            refused = f"Odoo refused a draft credit note for this user ({e.kind}): {e.message}"
    summary = f"Orchid draft email: {order} pricing"
    existing = s.search_read("mail.activity", [("res_model", "=", "sale.order"), ("res_id", "=", target["order_id"]), ("summary", "=", summary), ("user_id", "=", s.uid)], ["id"])
    note = f"<p><b>To:</b> {target['customer']}<br><b>Subject:</b> {subject}</p>{html}"
    if existing:
        aid = existing[0]["id"]
        s.call("mail.activity", "write", [aid], {"note": note})
    else:
        aid = s.call("sale.order", "activity_schedule", [target["order_id"]], act_type_xmlid="mail.mail_activity_data_email",
                     summary=summary, note=note, user_id=s.uid, date_deadline=today.isoformat())[0]
    rec = s.read("mail.activity", [aid], ["state", "activity_type_id", "user_id"])[0]
    return {"kind": "email", "model": "mail.activity", "id": aid, "state": f"to-do ({rec['state']}), not sent",
            "reused": bool(existing), "subject": subject, "body": body, "to": target["customer"],
            "credit_note_refused": refused, "url": odoo_link("sale.order", target["order_id"])}


def template_text(asker, totals, findings, target):
    """Deterministic prose used when there is no model key (or the model's text fails the number check)."""
    if not findings:
        return f"No order lines in range differ from the trade list or the agreed rebate ({totals['lines_checked']} lines on {totals['orders_checked']} orders checked).", "", ""
    summary = (f"{totals['lines_flagged']} of {totals['lines_checked']} order lines on {totals['orders_flagged']} orders differ from the trade list "
               f"or the agreed rebate. Customers were overcharged EUR {totals['overcharged_total']:.2f} and undercharged EUR {totals['undercharged_total']:.2f} in total.")
    if not target:
        return summary, "", ""
    rows = "\n".join(f"- {f['product']['value']}: charged {f['charged_unit_price']['value']:.2f} less {f['charged_discount_pct']['value']:g}%, "
                     f"agreed {f['list_price']['value']:.2f} less {f['agreed_rebate_pct']['value']:g}% (difference EUR {f['variance']['value']:.2f})"
                     for f in target["findings"])
    if target["net_variance"] > 0:
        subject = f"Credit for pricing on order {target['order']}"
        lead = f"We found that order {target['order']} was charged above your agreed terms, and a credit note for EUR {target['net_variance']:.2f} will follow."
    else:
        subject = f"Pricing on order {target['order']}"
        lead = f"While reviewing order {target['order']} we found it was priced below your agreed terms by EUR {-target['net_variance']:.2f}. We would like to talk it through before anything changes."
    body = f"Hi {target['customer']} team,\n\n{lead}\n\n{rows}\n\nKind regards,\n{asker['name']}\nLiffey Fixings Ltd"
    return summary, subject, body


def fact_sheet(totals, findings):
    facts = {"totals": totals}
    for i, f in enumerate(findings, 1):
        facts[f"F{i}"] = {k: v["value"] for k, v in f.items() if not k.startswith("_")}
    return facts


# ---------- the run ----------

def run(user_key, question, use_llm=True, make_draft=True, today=None):
    today = today or date.today()
    u = USERS[user_key]
    s = AgentSession(u["login"], u["password"])
    asker = {"key": user_key, "name": u["name"], "login": u["login"], "role": u["role"], "uid": s.uid}
    visible = {o["partner_id"][0]: o["partner_id"][1] for o in s.search_read("sale.order", [], ["partner_id"])}

    key = llm.load_key() if use_llm else None
    client, budget = (llm.make_client(key), llm.Budget()) if key else (None, None)
    notes = []
    plan, planned_by = None, "rules"
    if client:
        try:
            plan, planned_by = validate_plan(llm.plan(client, budget, question, today, visible), visible), llm.MODEL
        except Exception as e:  # budget, refusal, bad plan: fall back to the deterministic planner, and say so
            notes.append(f"model plan not used: {e}")
    plan = plan or rule_plan(question, today, visible)
    plan["planned_by"] = planned_by

    answer = {"asker": asker, "question": question, "asked_at": datetime.now().isoformat(timespec="seconds"), "plan": plan}
    if not plan["in_scope"]:
        answer.update(summary={"text": "Out of scope: this reading phase answers whether orders were priced off the trade list or the agreed rebate.", "by": "rules"},
                      findings=[], draft=None)
    else:
        orders, lines, products, categ_parents, partners, by_pl = fetch(s, plan)
        findings, skipped = pricing.check_lines(orders, lines, products, categ_parents, partners, by_pl)
        totals = pricing.totals(findings, len(orders), len(lines))
        item_pl = {i["id"]: pl for pl, its in by_pl.items() for i in its}
        for f in findings:  # clickable source links: lines open their order, pricelist rules open their pricelist
            for v in f.values():
                src = v.get("source") if isinstance(v, dict) else None
                if src:
                    m, rid = src["model"], src["id"]
                    src["href"] = (odoo_link("sale.order", f["_ids"]["order_id"]) if m == "sale.order.line"
                                   else odoo_link("product.pricelist", item_pl[rid]) if m == "product.pricelist.item" else odoo_link(m, rid))
        target = pick_draft_target(findings)
        summary, subject, body = template_text(asker, totals, findings, target)
        by = "template"
        facts = fact_sheet(totals, findings)
        if client and findings:
            brief = ("none" if not target else
                     f"{'a credit note cover message' if target['net_variance'] > 0 else 'an email'} to {target['customer']} about order "
                     f"{target['order']} (facts {[f'F{findings.index(x) + 1}' for x in target['findings']]}), net difference {target['net_variance']:.2f} EUR, "
                     f"{'customer was overcharged' if target['net_variance'] > 0 else 'customer was undercharged'}")
            try:
                out = llm.explain(client, budget, asker, question, facts, brief)
                stray = llm.numbers_not_in(" ".join(out.values()), facts | {"brief": brief})
                if stray or not llm.valid_fact_refs(out["summary"], len(findings)):
                    notes.append(f"model text rejected: numbers not in the facts {stray}")
                else:
                    summary, by = out["summary"], llm.MODEL
                    if target:
                        subject, body = out["draft_subject"], out["draft_body"]
            except Exception as e:
                notes.append(f"model text not used: {e}")
        draft = create_draft(s, target, subject, body, today) if (target and make_draft) else None
        answer.update(totals=totals, findings=findings, skipped=skipped, facts_index={f"F{i}": f["_ids"] for i, f in enumerate(findings, 1)},
                      summary={"text": summary, "by": by}, draft=draft)

    answer["odoo_calls"] = s.calls
    answer["auto_sent"] = sum(1 for c in s.calls if c["method"] in SEND_METHODS)
    answer["llm"] = {"model": llm.MODEL if client else None, "key_present": bool(key), "token_cap": llm.RUN_TOKEN_CAP,
                     "tokens_used": budget.spent if budget else 0, "calls": budget.calls if budget else []}
    answer["notes"] = notes
    return answer


def save_run(answer):
    Path("runs").mkdir(exist_ok=True)
    path = Path("runs") / f"{answer['asked_at'].replace(':', '')}-{answer['asker']['key']}.json"
    text = json.dumps(answer, indent=1, default=str)
    assert USERS[answer["asker"]["key"]]["password"] not in text and "sk-ant" not in text, "refusing to save a secret"
    path.write_text(text)
    return path


def print_answer(a):
    print(f"\n== {a['asker']['name']} ({a['asker']['role']}) asks: {a['question']}")
    p = a["plan"]
    print(f"plan [{p['planned_by']}]: {p['reading']}  ({p['date_from']} .. {p['date_to']})")
    print(f"\n{a['summary']['text']}\n   [prose by {a['summary']['by']}]")
    for i, f in enumerate(a.get("findings", []), 1):
        src = lambda k: f"{f[k]['source']['model']}#{f[k]['source']['id']}.{f[k]['source']['field']}"
        print(f"\n F{i} {f['order']['value']} {f['order_date']['value'][:10]} {f['customer']['value']} ({f['salesperson']['value']})"
              f"\n    {f['product']['value']} x {f['qty']['value']:g}: {', '.join(f['issue']['value'])}, variance EUR {f['variance']['value']:+.2f}"
              f"\n    charged {f['charged_unit_price']['value']:.2f} less {f['charged_discount_pct']['value']:g}%   <- {src('charged_unit_price')}, {src('charged_discount_pct')}"
              f"\n    list    {f['list_price']['value']:.2f}   <- {src('list_price')}"
              f"\n    rebate  {f['agreed_rebate_pct']['value']:g}%   <- " + (src('agreed_rebate_pct') if 'source' in f['agreed_rebate_pct'] else f['agreed_rebate_pct']['computed']))
    d = a.get("draft")
    if d:
        print(f"\nDRAFT ({d['kind']}, Odoo {d['model']} #{d['id']}, state: {d['state']}) {d['url']}")
        if d.get("credit_note_refused"):
            print(f"  ({d['credit_note_refused']})")
        print(f"  Subject: {d['subject']}\n  " + d["body"].replace("\n", "\n  "))
    print(f"\nauto-sent: {a['auto_sent']}   odoo calls as {a['asker']['login']}: {len(a['odoo_calls'])}   "
          f"model: {a['llm']['model'] or 'off (no key)'}   tokens: {a['llm']['tokens_used']}/{a['llm']['token_cap']}")
    for n in a["notes"]:
        print(f"note: {n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", choices=sorted(USERS), required=True)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-draft", action="store_true")
    ap.add_argument("question")
    args = ap.parse_args()
    ans = run(args.user, args.question, use_llm=not args.no_llm, make_draft=not args.no_draft)
    print_answer(ans)
    print(f"saved {save_run(ans)}")
