"""Minimal Odoo XML-RPC session. One Session = one Odoo user; Odoo applies that user's ACLs and record rules.

ponytail: stdlib xmlrpc.client. Odoo 19 deprecates XML-RPC in favour of /json/2 (removed in Odoo 20);
swap the transport here if you move to 20, nothing else changes.
"""
import os
import xmlrpc.client

URL = os.environ.get("ODOO_URL", "http://127.0.0.1:8069")
DB = os.environ.get("ODOO_DB", "orchid")


class OdooError(Exception):
    """An Odoo-side refusal (AccessError, ValidationError...). `kind` is the Odoo exception class name."""

    def __init__(self, kind, message):
        super().__init__(f"{kind}: {message}")
        self.kind, self.message = kind, message


class Session:
    def __init__(self, login, password):
        self.login, self._password = login, password
        self.uid = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common").authenticate(DB, login, password, {})
        if not self.uid:
            raise OdooError("AuthenticationError", f"login refused for {login}")
        self._obj = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
        self.calls = []  # every RPC this session made: the audit trail shown in the UI

    def call(self, model, method, *args, **kw):
        entry = {"model": model, "method": method}
        if args and method in ("search_read", "search_count"):
            entry["domain"] = args[0]
        try:
            result = self._obj.execute_kw(DB, self.uid, self._password, model, method, list(args), kw)
        except xmlrpc.client.Fault as f:
            # Odoo's XML-RPC fault codes: 2 UserError/ValidationError, 3 AccessDenied, 4 AccessError
            kind = {2: "UserError", 3: "AccessDenied", 4: "AccessError"}.get(f.faultCode, "ServerError")
            msg = str(f.faultString).strip().splitlines()[0]
            entry["refused"] = kind
            self.calls.append(entry)
            raise OdooError(kind, msg) from None
        if isinstance(result, list):
            entry["rows"] = len(result)
        self.calls.append(entry)
        return result

    def search_read(self, model, domain, fields, **kw):
        return self.call(model, "search_read", domain, fields=fields, **kw)

    def read(self, model, ids, fields):
        return self.call(model, "read", list(ids), fields)
