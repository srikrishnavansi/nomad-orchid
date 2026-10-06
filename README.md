# Orchid, reading phase, on a real Odoo

Ask an ERP a pricing question and get an answer where every field cites the Odoo record it came from, scoped to what the asker is allowed to see, with one draft at the end and **auto-sent: 0**.

> "Which orders this month were priced off the list price or off the agreed rebate?"

- **Aoife (sales rep)** gets her 3 flagged lines. Odoo will not hand her Cian's orders, and when the agent tries to draft a credit note for her, Odoo refuses (`AccessError`), so her draft is an email to the customer, kept as an Email to-do on her own order.
- **Niamh (finance manager)** gets all 5 flagged lines this month, across both reps, and a real **draft** credit note (`account.move`, `out_refund`, state `draft`) for the overcharge.
- Nothing is posted, confirmed or emailed. A named person opens the draft in Odoo and decides.

## Run it

```bash
docker compose up -d      # Odoo 19 Community + Postgres on 127.0.0.1:8069 (first boot ~1 min)
make demo                 # venv, seed sample data, ask the question as Aoife and as Niamh
make ui                   # one-page UI on http://127.0.0.1:8070 (user switcher, clickable citations)
make test                 # 10 assert-based checks against the live Odoo
make reset                # wipe the database and start again
```

Odoo itself: http://127.0.0.1:8069, logins in `demo_users.json` (local test users made by `seed.py`). Optional model: put an Anthropic key in `~/.config/jobhunt/anthropic.key`. Without it everything works and the prose comes from a template, labelled as such.

## How it works

```
question -> plan (model, or a rule planner)          which dates, which customers; validated against what the asker can see
         -> read Odoo AS THE ASKER (XML-RPC)          sale.order, sale.order.line, product, res.partner, product.pricelist.item
         -> pricing.py (Decimal, no model)            list price x (1 - agreed rebate %) x qty vs what was charged
         -> answer: every field {value, source: {model, id, field}} or {value, computed: formula, inputs}
         -> prose (model, or template)                must not contain a number that is not in the computed facts
         -> one draft in Odoo, as the asker           credit note if Odoo allows it, else an Email to-do; never sent
```

| File | What |
|---|---|
| `agent.py` | the run: plan, fetch as the asker, check, draft, answer. CLI. |
| `pricing.py` | deterministic check and citations. Pure functions, self-test in `__main__`. |
| `llm.py` | the only model calls: key file only, `claude-sonnet-5-5`, hard 20k token cap per run, number guard |
| `odoo_rpc.py` | 50-line XML-RPC session; records every call for the audit trail |
| `seed.py` | sample company, users, price list, rebate agreements, 60 orders, 14 deliberate pricing mistakes |
| `server.py`, `index.html` | stdlib web server and the one page |
| `test_orchid.py` | the checks below |

## What the tests prove (`make test`)

1. **Odoo does the scoping.** Each rep sees exactly the orders Odoo's record rules give them; reading the other rep's order raises `AccessError`; finance sees all 60.
2. **The check finds exactly the 14 seeded mistakes, and nothing else**, over the full two months, including a category rule beating a global rule and a rebate given before its agreement started.
3. **Answers are scoped**: Aoife's answer is a strict subset of Niamh's and never contains Cian's customers.
4. **Every cited field re-reads identically** from Odoo as the same user, and every computed field recomputes from its cited inputs.
5. **Drafts stay drafts**: the credit note is `draft` and created by the asker (not a superuser), the sales rep's credit note is refused by Odoo, no `mail.mail` is queued, nothing is posted, no order changes state, re-runs update the same draft.
6. **The agent session cannot send**: `action_post`, `message_post`, `action_quotation_send`, writing `state`, creating invoices or editing partners are blocked before they reach Odoo.
7. **The model is boxed in** (with a fake client): prose with an invented number is rejected, a call that could break the token cap is never made, a plan naming a customer the asker cannot see is refused.

## Real vs sample

| Real | Sample |
|---|---|
| Odoo 19 Community, its access rights and record rules, its draft credit notes and activities | The company (Liffey Fixings Ltd), customers, products, prices, rebate %s |
| Every read happens through the asker's own login; admin is used only by `seed.py` and as the test's referee | The 60 orders and the 14 mistakes in them (`ground_truth.json`, generated) |
| The discrepancy maths, the citations, the draft records | The three users and their passwords (`demo_users.json`, local only) |
| Model outputs in `runs/`, when a key is present | |

## Honest notes

- **Rebate agreements** are Odoo pricelists of `%` rules on top of the trade list (Community has no rebate module). The check reads them with Odoo's own precedence (product, then category, then global; dates; min qty). Agreements that are not plain `%` off one list are reported as "not checked", never guessed.
- **Draft email**: Odoo has no "draft email" record that cannot be sent by its mail queue, so the draft lives as an Email to-do (`mail.activity`) on the order, assigned to the asker.
- **Logins**: the UI's user switcher logs in with the seeded test users. A real deployment would use the person's own Odoo login or API key; the agent code does not change.
- **XML-RPC** is deprecated in Odoo 19 (removed in 20). `odoo_rpc.py` is the only file to change for `/json/2`.
- **Model**: `claude-sonnet-5-5` only, effort `low`, structured JSON output, every call pre-counted with `count_tokens`. No automatic fallback to another model: on refusal, cap or a failed number check, the run falls back to the deterministic plan and template, and says so in `notes`.
- VAT is left out of the sample to keep the arithmetic readable.
