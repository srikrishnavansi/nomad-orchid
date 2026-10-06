"""Seed a fresh local Odoo with sample data for a Dublin building-supplies distributor.

Runs as the Odoo admin over XML-RPC. This is the ONLY file (plus the test's ground-truth checks) that uses admin;
the agent never does. Writes ground_truth.json: the order lines this script priced wrong on purpose, and how.

Usage: python seed.py            (refuses to run twice; `make reset` wipes the database)
"""
import json
import random
from datetime import date, datetime, timedelta

from odoo_rpc import Session

ADMIN_LOGIN, ADMIN_PASSWORD = "admin", "admin"  # Odoo's default for a database created with -d/-i, localhost only
USERS = json.load(open("demo_users.json"))
TODAY = date.today()
FIRST_OF_MONTH = TODAY.replace(day=1)
PREV_MONTH_START = (FIRST_OF_MONTH - timedelta(days=1)).replace(day=1)

CATEGORIES = ["Fixings", "Adhesives & Sealants", "Power Tools", "Site Safety"]
# name, category, trade list price (EUR)
PRODUCTS = [
    ("M8 Anchor Bolt, box of 50", "Fixings", 42.00),
    ("Concrete Screw 7.5x100, box of 100", "Fixings", 38.90),
    ("Timber Screw 5x80, box of 200", "Fixings", 22.40),
    ("Chemical Anchor Resin 380ml", "Adhesives & Sealants", 18.50),
    ("PU Sealant 600ml", "Adhesives & Sealants", 9.80),
    ("Cordless Drill 18V", "Power Tools", 189.00),
    ("Angle Grinder 115mm", "Power Tools", 96.00),
    ("SDS Drill Bit Set", "Power Tools", 64.00),
    ("Hi-Vis Vest, pack of 10", "Site Safety", 35.00),
    ("Hard Hat EN397", "Site Safety", 12.60),
]
# customer -> (sales rep key, [(rule scope, percent, start date or None)])
CUSTOMERS = {
    "Brennan Builders": ("aoife", [("global", 8.0, None)]),
    "Dockside Construction": ("aoife", [("global", 10.0, None), ("Fixings", 15.0, None)]),
    "Kilmainham Interiors": ("aoife", [("global", 5.0, PREV_MONTH_START + timedelta(days=14))]),
    "Harbour Facilities Management": ("cian", [("global", 6.0, None), ("Power Tools", 12.0, None)]),
    "Northside Fit-Out": ("cian", [("global", 7.0, None)]),
}


# Seeding must not queue a single email (no "you have been assigned" notices, no invites, no chatter mail).
QUIET = {"tracking_disable": True, "mail_create_nolog": True, "mail_notrack": True, "mail_auto_subscribe_no_notify": True, "no_reset_password": True}


def main():
    admin = Session(ADMIN_LOGIN, ADMIN_PASSWORD)
    rpc = admin.call
    admin.call = lambda model, method, *a, **k: rpc(model, method, *a, **({"context": QUIET} | k))
    if admin.search_read("res.users", [("login", "=", USERS["aoife"]["login"])], ["id"]):
        raise SystemExit("Already seeded. Run `make reset` for a fresh database.")
    xmlid = lambda name: admin.search_read("ir.model.data", [("module", "=", name.split(".")[0]), ("name", "=", name.split(".")[1])], ["res_id"])[0]["res_id"]

    # Company: Irish, EUR. Pricelists and per-line discounts switched on (Settings > Sales).
    eur = admin.call("res.currency", "search", [("name", "=", "EUR")], context={"active_test": False})[0]
    admin.call("res.currency", "write", [eur], {"active": True})
    ie = admin.call("res.country", "search", [("code", "=", "IE")])[0]
    admin.call("res.company", "write", [1], {"name": "Liffey Fixings Ltd", "currency_id": eur, "country_id": ie, "city": "Dublin"})
    settings = admin.call("res.config.settings", "create", {"group_product_pricelist": True, "group_discount_per_so_line": True})
    admin.call("res.config.settings", "execute", [settings])

    # Users: two sales reps restricted to their own orders, one finance manager who sees all and can issue credit notes.
    sales_own, sales_all, acc_admin = xmlid("sales_team.group_sale_salesman"), xmlid("sales_team.group_sale_salesman_all_leads"), xmlid("account.group_account_manager")
    internal = xmlid("base.group_user")
    groups = {"aoife": [internal, sales_own], "cian": [internal, sales_own], "niamh": [internal, sales_all, acc_admin]}
    uid = {}
    for key, u in USERS.items():
        if key.startswith("_"):
            continue
        uid[key] = admin.call("res.users", "create", {"name": u["name"], "login": u["login"], "email": u["login"],
                                                      "password": u["password"], "group_ids": [(6, 0, groups[key])],
                                                      "notification_type": "inbox"})

    categ = {c: admin.call("product.category", "create", {"name": c}) for c in CATEGORIES}
    tmpl, product = {}, {}
    for name, c, price in PRODUCTS:
        tmpl[name] = admin.call("product.template", "create", {"name": name, "categ_id": categ[c], "list_price": price, "type": "consu", "taxes_id": [(6, 0, [])]})
        product[name] = admin.call("product.product", "search", [("product_tmpl_id", "=", tmpl[name])])[0]

    trade = admin.call("product.pricelist", "create", {"name": "Trade List 2026 (EUR)", "currency_id": eur, "item_ids": [
        (0, 0, {"applied_on": "1_product", "product_tmpl_id": tmpl[n], "compute_price": "fixed", "fixed_price": p}) for n, _, p in PRODUCTS]})

    partner, rep_of, rules = {}, {}, {}
    for cname, (rep, agreement) in CUSTOMERS.items():
        items = []
        for scope, pct, start in agreement:
            item = {"compute_price": "percentage", "percent_price": pct, "base": "pricelist", "base_pricelist_id": trade}
            item.update({"applied_on": "3_global"} if scope == "global" else {"applied_on": "2_product_category", "categ_id": categ[scope]})
            if start:
                item["date_start"] = f"{start} 00:00:00"
            items.append((0, 0, item))
        pl = admin.call("product.pricelist", "create", {"name": f"Rebate agreement: {cname}", "currency_id": eur, "item_ids": items})
        partner[cname] = admin.call("res.partner", "create", {"name": cname, "is_company": True, "city": "Dublin", "country_id": ie,
                                                              "email": f"accounts@{cname.split()[0].lower()}.test",
                                                              "user_id": uid[rep], "property_product_pricelist": pl})
        rep_of[cname], rules[cname] = rep, agreement

    def agreed_pct(cname, pname, on):
        """Seed-side intent only (what the agreement says), used to price the 'correct' lines."""
        cat = next(c for n, c, _ in PRODUCTS if n == pname)
        best = None
        for scope, pct, start in rules[cname]:
            if start and on < start:
                continue
            if scope == cat:
                return pct
            if scope == "global":
                best = pct
        return best or 0.0

    rng = random.Random(1403)
    # 60 orders: 48 across the ~two months before this one, 12 so far this month.
    early = [PREV_MONTH_START - timedelta(days=28) + timedelta(days=rng.randrange((FIRST_OF_MONTH - PREV_MONTH_START).days + 28)) for _ in range(48)]
    late = [FIRST_OF_MONTH + timedelta(days=rng.randrange((TODAY - FIRST_OF_MONTH).days + 1)) for _ in range(12)]
    days = sorted(early) + sorted(late)
    names = list(CUSTOMERS)
    plan = []
    for i, d in enumerate(days):
        cname = names[i % len(names)]
        lines = rng.sample([p[0] for p in PRODUCTS], rng.randint(2, 4))
        plan.append({"date": d, "customer": cname, "lines": [{"product": p, "qty": rng.choice([2, 4, 5, 6, 10, 12, 20, 24])} for p in lines]})

    # Deliberate mistakes, spread over both reps and all three calendar months: (period, customer, kind).
    period = lambda d: "this" if d >= FIRST_OF_MONTH else "prev" if d >= PREV_MONTH_START else "older"
    MISTAKES = [
        ("older", "Harbour Facilities Management", "rebate_missed"), ("older", "Northside Fit-Out", "stale_list_price"),
        ("older", "Dockside Construction", "rebate_too_high"),
        ("prev", "Kilmainham Interiors", "typo_price"), ("prev", "Brennan Builders", "rebate_missed"),
        ("prev", "Kilmainham Interiors", "rebate_before_agreement"), ("prev", "Dockside Construction", "wrong_rule_applied"),
        ("prev", "Harbour Facilities Management", "wrong_rule_applied"), ("prev", "Northside Fit-Out", "rebate_missed"),
        ("this", "Northside Fit-Out", "stale_list_price"), ("this", "Harbour Facilities Management", "rebate_too_high"),
        ("this", "Brennan Builders", "typo_price"), ("this", "Dockside Construction", "rebate_missed"),
        ("this", "Kilmainham Interiors", "rebate_too_high"),
    ]
    mistakes, used = [], set()
    for per, cname, kind in MISTAKES:
        start = rules[cname][0][2]
        cands = [i for i, o in enumerate(plan) if o["customer"] == cname and period(o["date"]) == per and i not in used
                 and (kind != "rebate_before_agreement" or o["date"] < start)
                 and (kind not in ("rebate_too_high", "rebate_missed") or not start or o["date"] >= start)]
        assert cands, (per, cname, kind)
        idx = cands[len(cands) // 2]
        used.add(idx)
        li = 0
        if kind == "wrong_rule_applied":  # the customer's category rule should win over their global rule
            cat = next(s for s, _, _ in rules[cname] if s != "global")
            pname = next(n for n, c, _ in PRODUCTS if c == cat)
            if not any(l["product"] == pname for l in plan[idx]["lines"]):
                plan[idx]["lines"].append({"product": pname, "qty": 10})
            li = next(j for j, l in enumerate(plan[idx]["lines"]) if l["product"] == pname)
        mistakes.append((idx, li, kind))
    bad = {(i, j): k for i, j, k in mistakes}

    ground_truth = []
    for i, o in enumerate(plan):
        cname = o["customer"]
        vals = []
        for j, l in enumerate(o["lines"]):
            list_price = next(p for n, _, p in PRODUCTS if n == l["product"])
            pct = agreed_pct(cname, l["product"], o["date"])
            kind = bad.get((i, j))
            price, disc = list_price, pct
            if kind == "rebate_missed":
                disc = 0.0
            elif kind == "stale_list_price":
                price = round(list_price * 0.94, 2)          # last year's list price
            elif kind == "rebate_too_high":
                disc = pct + 5.0
            elif kind == "typo_price":
                price = round(list_price * 1.8, 2)           # keyed wrong
            elif kind == "wrong_rule_applied":
                disc = next(p for s, p, _ in rules[cname] if s == "global")  # global % instead of the category %
            elif kind == "rebate_before_agreement":
                disc = rules[cname][0][1]                    # agreement % given before its start date
            vals.append((0, 0, {"product_id": product[l["product"]], "product_uom_qty": l["qty"], "price_unit": price, "discount": disc, "tax_ids": [(6, 0, [])]}))
            l["kind"] = kind
        so = admin.call("sale.order", "create", {"partner_id": partner[cname], "user_id": uid[rep_of[cname]],
                                                 "pricelist_id": admin.read("res.partner", [partner[cname]], ["property_product_pricelist"])[0]["property_product_pricelist"][0],
                                                 "order_line": vals})
        admin.call("sale.order", "action_confirm", [so])
        stamp = datetime.combine(o["date"], datetime.min.time()) + timedelta(hours=7 + rng.randrange(4), minutes=rng.randrange(60))
        admin.call("sale.order", "write", [so], {"date_order": stamp.strftime("%Y-%m-%d %H:%M:%S")})
        line_ids = admin.search_read("sale.order.line", [("order_id", "=", so)], ["id", "price_unit", "discount"], order="id")
        for l, rec, v in zip(o["lines"], line_ids, vals):
            assert abs(rec["price_unit"] - v[2]["price_unit"]) < 1e-6 and abs(rec["discount"] - v[2]["discount"]) < 1e-6, ("Odoo repriced a line", rec, v)
            if l["kind"]:
                ground_truth.append({"order_id": so, "line_id": rec["id"], "kind": l["kind"], "rep": rep_of[cname], "customer": cname, "date": str(o["date"])})

    assert admin.call("mail.mail", "search_count", []) == 0, "seeding queued email"
    json.dump({"seeded_on": str(TODAY), "orders": len(plan), "mistakes": ground_truth}, open("ground_truth.json", "w"), indent=1)
    print(f"Seeded {len(plan)} orders, {sum(len(o['lines']) for o in plan)} lines, {len(ground_truth)} deliberate pricing mistakes.")


if __name__ == "__main__":
    main()
