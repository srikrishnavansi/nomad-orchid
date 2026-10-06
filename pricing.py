"""Deterministic pricing check. Pure functions over records already read from Odoo: no IO, no LLM.

Every output field is either cited  {"value", "source": {"model", "id", "field"}}  (a value read from one Odoo record)
or computed {"value", "computed": formula, "inputs": [field names]} built only from cited fields.
"""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def money(x):
    return Decimal(str(x)).quantize(CENT, ROUND_HALF_UP)


def cite(model, rid, field, value):
    if isinstance(value, list) and len(value) == 2:  # many2one -> display name
        value = value[1]
    return {"value": value, "source": {"model": model, "id": rid, "field": field}}


def computed(value, formula, *inputs):
    return {"value": value, "computed": formula, "inputs": list(inputs)}


def applicable_rule(items, product, categ_ancestors, when, qty):
    """Odoo's pricelist precedence: product rule > category rule > global rule, then higher min qty, then newest.
    Returns the first item valid for `when` (an Odoo 'YYYY-MM-DD HH:MM:SS' string), or None."""
    def matches(it):
        if it["date_start"] and when < it["date_start"]:
            return False
        if it["date_end"] and when > it["date_end"]:
            return False
        if qty < (it["min_quantity"] or 0):
            return False
        on = it["applied_on"]
        return (on == "3_global"
                or (on == "2_product_category" and it["categ_id"] and it["categ_id"][0] in categ_ancestors)
                or (on == "1_product" and it["product_tmpl_id"] and it["product_tmpl_id"][0] == product["product_tmpl_id"][0])
                or (on == "0_product_variant" and it["product_id"] and it["product_id"][0] == product["id"]))
    ranked = sorted(items, key=lambda it: (it["applied_on"], -(it["min_quantity"] or 0), -it["id"]))
    return next((it for it in ranked if matches(it)), None)


def check_lines(orders, lines, products, categ_parents, partners, items_by_pricelist):
    """Compare every order line with the trade list price and the customer's agreed rebate.

    orders: {id: sale.order}, lines: [sale.order.line], products: {id: product.product},
    categ_parents: {categ_id: parent_id or False}, partners: {id: res.partner with property_product_pricelist},
    items_by_pricelist: {pricelist_id: [product.pricelist.item]}.
    Returns (findings, skipped) where skipped lists lines that could not be checked and why (never guessed).
    """
    findings, skipped = [], []
    for ln in lines:
        order = orders[ln["order_id"][0]]
        partner = partners[order["partner_id"][0]]
        product = products[ln["product_id"][0]]
        ancestors, c = set(), product["categ_id"][0]
        while c:
            ancestors.add(c)
            c = categ_parents.get(c)
        agreement_id = partner["property_product_pricelist"] and partner["property_product_pricelist"][0]
        agreement = items_by_pricelist.get(agreement_id, [])
        bases = {it["base_pricelist_id"][0] for it in agreement if it["base"] == "pricelist" and it["base_pricelist_id"]}
        if len(bases) != 1 or any(it["compute_price"] != "percentage" for it in agreement):
            skipped.append({"line_id": ln["id"], "reason": "agreement is not a plain % off one list; not checked"})
            continue
        list_item = applicable_rule(items_by_pricelist.get(bases.pop(), []), product, ancestors, order["date_order"], ln["product_uom_qty"])
        if not list_item or list_item["compute_price"] != "fixed":
            skipped.append({"line_id": ln["id"], "reason": "no fixed trade-list price for this product"})
            continue
        rule = applicable_rule(agreement, product, ancestors, order["date_order"], ln["product_uom_qty"])

        list_price = money(list_item["fixed_price"])
        pct = Decimal(str(rule["percent_price"])) if rule else Decimal(0)
        qty = Decimal(str(ln["product_uom_qty"]))
        charged_price, charged_disc = money(ln["price_unit"]), Decimal(str(ln["discount"]))
        expected = money(list_price * (1 - pct / 100) * qty)
        charged = money(ln["price_subtotal"])
        variance = charged - expected
        issues = []
        if charged_price != list_price:
            issues.append("off_list")
        if charged_disc != pct:
            issues.append("off_rebate")
        if not issues and variance == 0:
            continue

        f = {
            "order": cite("sale.order", order["id"], "name", order["name"]),
            "order_date": cite("sale.order", order["id"], "date_order", order["date_order"]),
            "customer": cite("sale.order", order["id"], "partner_id", order["partner_id"]),
            "salesperson": cite("sale.order", order["id"], "user_id", order["user_id"]),
            "product": cite("sale.order.line", ln["id"], "product_id", ln["product_id"]),
            "qty": cite("sale.order.line", ln["id"], "product_uom_qty", ln["product_uom_qty"]),
            "charged_unit_price": cite("sale.order.line", ln["id"], "price_unit", ln["price_unit"]),
            "charged_discount_pct": cite("sale.order.line", ln["id"], "discount", ln["discount"]),
            "charged_subtotal": cite("sale.order.line", ln["id"], "price_subtotal", ln["price_subtotal"]),
            "list_price": cite("product.pricelist.item", list_item["id"], "fixed_price", list_item["fixed_price"]),
            "agreement": cite("res.partner", partner["id"], "property_product_pricelist", partner["property_product_pricelist"]),
            "agreed_rebate_pct": (cite("product.pricelist.item", rule["id"], "percent_price", rule["percent_price"]) if rule
                                  else computed(0.0, "no rule of the agreement applies on the order date", "agreement", "order_date")),
            "expected_subtotal": computed(float(expected), "list_price x (1 - agreed_rebate_pct/100) x qty", "list_price", "agreed_rebate_pct", "qty"),
            "variance": computed(float(variance), "charged_subtotal - expected_subtotal (positive = customer overcharged)", "charged_subtotal", "expected_subtotal"),
            "issue": computed(issues, "off_list if charged_unit_price != list_price; off_rebate if charged_discount_pct != agreed_rebate_pct",
                              "charged_unit_price", "list_price", "charged_discount_pct", "agreed_rebate_pct"),
        }
        if rule and rule["applied_on"] == "2_product_category":
            f["rule_scope"] = cite("product.pricelist.item", rule["id"], "categ_id", rule["categ_id"])
        f["_ids"] = {"order_id": order["id"], "line_id": ln["id"], "partner_id": partner["id"]}
        findings.append(f)
    findings.sort(key=lambda f: (f["order_date"]["value"], f["_ids"]["line_id"]))
    return findings, skipped


def totals(findings, orders_checked, lines_checked):
    over = sum((money(f["variance"]["value"]) for f in findings if f["variance"]["value"] > 0), Decimal(0))
    under = sum((-money(f["variance"]["value"]) for f in findings if f["variance"]["value"] < 0), Decimal(0))
    return {
        "orders_checked": orders_checked,
        "lines_checked": lines_checked,
        "lines_flagged": len(findings),
        "orders_flagged": len({f["_ids"]["order_id"] for f in findings}),
        "overcharged_total": float(over),
        "undercharged_total": float(under),
    }


if __name__ == "__main__":
    # Self-check on hand-built records (no Odoo needed).
    it = lambda i, **k: {"id": i, "date_start": False, "date_end": False, "min_quantity": 0, "categ_id": False,
                         "product_tmpl_id": False, "product_id": False, "base": "pricelist", "base_pricelist_id": [1, "Trade"],
                         "compute_price": "percentage", "percent_price": 0, "fixed_price": 0, **k}
    items = {1: [it(10, applied_on="1_product", product_tmpl_id=[7, "Bolt"], compute_price="fixed", fixed_price=42.0, base="list_price", base_pricelist_id=False)],
             2: [it(20, applied_on="3_global", percent_price=10.0), it(21, applied_on="2_product_category", categ_id=[3, "Fixings"], percent_price=15.0)]}
    order = {"id": 1, "name": "S1", "date_order": "2026-10-02 09:00:00", "partner_id": [5, "Dockside"], "user_id": [2, "Aoife"]}
    prod = {9: {"id": 9, "product_tmpl_id": [7, "Bolt"], "categ_id": [3, "Fixings"]}}
    partner = {5: {"id": 5, "property_product_pricelist": [2, "Agreement"]}}
    line = lambda i, p, d, q=10: {"id": i, "order_id": [1, "S1"], "product_id": [9, "Bolt"], "product_uom_qty": q, "price_unit": p, "discount": d,
                                  "price_subtotal": float(money(Decimal(str(p)) * (1 - Decimal(str(d)) / 100) * q))}
    fs, sk = check_lines({1: order}, [line(1, 42.0, 15.0), line(2, 42.0, 10.0), line(3, 39.48, 15.0)], prod, {3: False}, partner, items)
    assert not sk and [f["_ids"]["line_id"] for f in fs] == [2, 3], fs
    assert fs[0]["issue"]["value"] == ["off_rebate"] and fs[0]["variance"]["value"] == 21.0  # 15% category rule beats 10% global
    assert fs[1]["issue"]["value"] == ["off_list"] and fs[1]["variance"]["value"] == -21.42
    assert fs[0]["agreed_rebate_pct"]["source"] == {"model": "product.pricelist.item", "id": 21, "field": "percent_price"}
    t = totals(fs, 1, 3)
    assert (t["overcharged_total"], t["undercharged_total"], t["orders_flagged"]) == (21.0, 21.42, 1)
    print("pricing.py self-check ok")
