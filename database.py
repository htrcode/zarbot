import asyncio
import json
import os
from decimal import Decimal, ROUND_HALF_UP

import aiosqlite

from config import CURRENCY, INVOICE_PREFIX
from utils import fmt, today

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL CHECK (kind IN ('income','expense')),
    UNIQUE(name, kind)
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_discord_id INTEGER,
    client_name TEXT NOT NULL,
    client_contact TEXT,
    issue_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    currency TEXT NOT NULL,
    subtotal INTEGER NOT NULL,
    discount INTEGER NOT NULL DEFAULT 0,
    tax_percent REAL NOT NULL DEFAULT 0,
    tax INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL,
    notes TEXT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    ticket_channel_id INTEGER,
    voided INTEGER NOT NULL DEFAULT 0,
    void_reason TEXT,
    overdue_notified INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    description TEXT NOT NULL,
    quantity REAL NOT NULL,
    unit_price INTEGER NOT NULL,
    line_total INTEGER NOT NULL,
    details TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    method TEXT,
    note TEXT,
    paid_at TEXT NOT NULL,
    created_by INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('income','expense')),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    description TEXT,
    occurred_on TEXT NOT NULL,
    invoice_id INTEGER REFERENCES invoices(id),
    payment_id INTEGER REFERENCES payments(id),
    voided INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(occurred_on);
CREATE INDEX IF NOT EXISTS idx_pay_inv ON payments(invoice_id);
"""

DEFAULT_CATEGORIES = [
    ("Hosting Revenue", "income"),
    ("Other Sales", "income"),
    ("Server Rental", "expense"),
    ("Software Subscriptions", "expense"),
    ("Marketing", "expense"),
    ("Commissions", "expense"),
    ("Other Expenses", "expense"),
]


def invoice_number(invoice_id: int) -> str:
    return f"{INVOICE_PREFIX}-{invoice_id:05d}"


def _finish(inv: dict) -> dict:
    """Add computed fields: number, balance, status, is_partial."""
    paid = inv.get("paid", 0) or 0
    inv["paid"] = paid
    inv["number"] = invoice_number(inv["id"])
    inv["balance"] = max(inv["total"] - paid, 0)
    inv["is_partial"] = 0 < paid < inv["total"]
    if inv["voided"]:
        inv["status"] = "void"
    elif paid >= inv["total"]:
        inv["status"] = "paid"
    elif today().isoformat() > inv["due_date"]:
        inv["status"] = "overdue"
    elif paid > 0:
        inv["status"] = "partial"
    else:
        inv["status"] = "unpaid"
    return inv


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        # upgrade databases created by older versions
        for table, col in (("invoice_items", "details"), ("invoices", "client_contact")):
            cols = [r["name"] for r in await self.fetchall(f"PRAGMA table_info({table})")]
            if col not in cols:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        for name, kind in DEFAULT_CATEGORIES:
            await self.conn.execute(
                "INSERT OR IGNORE INTO categories(name, kind) VALUES (?, ?)", (name, kind)
            )
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def fetchall(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()

    async def fetchone(self, sql, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _audit(self, actor_id, action, entity, entity_id, details=None):
        await self.conn.execute(
            "INSERT INTO audit_log(actor_id, action, entity, entity_id, details) VALUES (?,?,?,?,?)",
            (actor_id, action, entity, entity_id, json.dumps(details or {}, default=str)),
        )

    # ---------- categories ----------
    async def categories(self, kind: str | None = None):
        if kind:
            rows = await self.fetchall("SELECT * FROM categories WHERE kind=? ORDER BY name", (kind,))
        else:
            rows = await self.fetchall("SELECT * FROM categories ORDER BY kind, name")
        return [dict(r) for r in rows]

    async def category_id(self, name: str, kind: str) -> int:
        row = await self.fetchone(
            "SELECT id FROM categories WHERE name=? AND kind=?", (name.strip(), kind)
        )
        if not row:
            raise ValueError(f"Unknown {kind} category `{name}`. Pick one from the list or add it first.")
        return row["id"]

    async def add_category(self, actor_id: int, name: str, kind: str):
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    "INSERT INTO categories(name, kind) VALUES (?,?)", (name.strip()[:50], kind)
                )
            except aiosqlite.IntegrityError:
                raise ValueError("That category already exists.")
            await self._audit(actor_id, "category.add", "category", cur.lastrowid, {"name": name, "kind": kind})
            await self.conn.commit()

    # ---------- invoices ----------
    async def create_invoice(self, actor_id, *, client_discord_id, client_name, client_contact=None, items,
                             due_days, tax_percent, discount_cents, category, notes, ticket_channel_id):
        category_id = await self.category_id(category, "income")
        lines = [(d, q, p, int((Decimal(str(q)) * p).quantize(Decimal(1), ROUND_HALF_UP)), det or None) for d, q, p, det in items]
        subtotal = sum(l[3] for l in lines)
        discount = min(discount_cents, subtotal)
        taxable = subtotal - discount
        tax = int((Decimal(taxable) * Decimal(str(tax_percent)) / 100).quantize(Decimal(1), ROUND_HALF_UP))
        total = taxable + tax
        issue = today()
        due = issue.fromordinal(issue.toordinal() + due_days)
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    """INSERT INTO invoices(client_discord_id, client_name, client_contact, issue_date, due_date, currency,
                       subtotal, discount, tax_percent, tax, total, notes, category_id, ticket_channel_id, created_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (client_discord_id, client_name, (client_contact or None), issue.isoformat(), due.isoformat(), CURRENCY,
                     subtotal, discount, tax_percent, tax, total, notes or None, category_id,
                     ticket_channel_id, actor_id),
                )
                invoice_id = cur.lastrowid
                await self.conn.executemany(
                    "INSERT INTO invoice_items(invoice_id, description, quantity, unit_price, line_total, details) VALUES (?,?,?,?,?,?)",
                    [(invoice_id, *l) for l in lines],
                )
                await self._audit(actor_id, "invoice.create", "invoice", invoice_id,
                                  {"client": client_name, "total": total, "items": len(lines)})
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        return invoice_id

    async def get_invoice(self, invoice_id: int):
        row = await self.fetchone(
            "SELECT i.*, c.name AS category FROM invoices i JOIN categories c ON c.id=i.category_id WHERE i.id=?",
            (invoice_id,),
        )
        if not row:
            return None
        inv = dict(row)
        inv["items"] = [dict(r) for r in await self.fetchall(
            "SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,))]
        inv["payments"] = [dict(r) for r in await self.fetchall(
            "SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (invoice_id,))]
        inv["paid"] = sum(p["amount"] for p in inv["payments"])
        return _finish(inv)

    async def list_invoices(self, status: str | None = None, month: str | None = None, limit: int = 15):
        rows = await self.fetchall(
            """SELECT i.*, COALESCE((SELECT SUM(amount) FROM payments p WHERE p.invoice_id=i.id),0) AS paid
               FROM invoices i ORDER BY i.id DESC"""
        )
        out = []
        for r in rows:
            inv = _finish(dict(r))
            if month and not inv["issue_date"].startswith(month):
                continue
            if status == "partial" and not (inv["is_partial"] and not inv["voided"]):
                continue
            if status and status != "partial" and inv["status"] != status:
                continue
            out.append(inv)
            if len(out) >= limit:
                break
        return out

    async def set_ticket(self, actor_id, invoice_id, channel_id):
        async with self.lock:
            await self.conn.execute("UPDATE invoices SET ticket_channel_id=? WHERE id=?", (channel_id, invoice_id))
            await self._audit(actor_id, "invoice.ticket", "invoice", invoice_id, {"channel_id": channel_id})
            await self.conn.commit()

    async def add_payment(self, actor_id, invoice_id, amount, method="manual", note=""):
        async with self.lock:  # prevents two simultaneous payments overpaying
            inv = await self.get_invoice(invoice_id)
            if not inv:
                raise ValueError("Invoice not found.")
            if inv["voided"]:
                raise ValueError("That invoice is void.")
            if amount <= 0:
                raise ValueError("Amount must be above zero.")
            if amount > inv["balance"]:
                raise ValueError(f"Amount exceeds the remaining balance ({fmt(inv['balance'])}).")
            try:
                paid_on = today().isoformat()
                cur = await self.conn.execute(
                    "INSERT INTO payments(invoice_id, amount, method, note, paid_at, created_by) VALUES (?,?,?,?,?,?)",
                    (invoice_id, amount, method[:40], note[:200], paid_on, actor_id),
                )
                pid = cur.lastrowid
                # Auto-record the income so invoices and books never drift apart
                await self.conn.execute(
                    """INSERT INTO transactions(kind, category_id, amount, description, occurred_on,
                       invoice_id, payment_id, created_by) VALUES ('income',?,?,?,?,?,?,?)""",
                    (inv["category_id"], amount, f"Payment for {inv['number']} ({inv['client_name']})",
                     paid_on, invoice_id, pid, actor_id),
                )
                await self._audit(actor_id, "payment.add", "invoice", invoice_id,
                                  {"payment_id": pid, "amount": amount, "method": method,
                                   "balance_after": inv["balance"] - amount})
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
            return pid

    async def void_invoice(self, actor_id, invoice_id, reason):
        async with self.lock:
            inv = await self.get_invoice(invoice_id)
            if not inv:
                raise ValueError("Invoice not found.")
            if inv["voided"]:
                raise ValueError("Already void.")
            await self.conn.execute("UPDATE invoices SET voided=1, void_reason=? WHERE id=?", (reason[:200], invoice_id))
            await self.conn.execute("UPDATE transactions SET voided=1 WHERE invoice_id=?", (invoice_id,))
            await self._audit(actor_id, "invoice.void", "invoice", invoice_id,
                              {"reason": reason, "had_paid": inv["paid"]})
            await self.conn.commit()

    async def overdue_to_notify(self):
        rows = await self.list_invoices(limit=10_000)
        return [i for i in rows if i["status"] == "overdue" and not i["overdue_notified"]]

    async def mark_notified(self, invoice_id):
        async with self.lock:
            await self.conn.execute("UPDATE invoices SET overdue_notified=1 WHERE id=?", (invoice_id,))
            await self.conn.commit()

    async def outstanding(self) -> int:
        row = await self.fetchone(
            """SELECT COALESCE(SUM(i.total - COALESCE(p.paid,0)),0) AS due FROM invoices i
               LEFT JOIN (SELECT invoice_id, SUM(amount) paid FROM payments GROUP BY invoice_id) p
               ON p.invoice_id=i.id WHERE i.voided=0"""
        )
        return row["due"]

    # ---------- transactions ----------
    async def add_transaction(self, actor_id, kind, category, amount, description, occurred_on):
        if amount <= 0:
            raise ValueError("Amount must be above zero.")
        cid = await self.category_id(category, kind)
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT INTO transactions(kind, category_id, amount, description, occurred_on, created_by) VALUES (?,?,?,?,?,?)",
                (kind, cid, amount, description[:200], occurred_on, actor_id),
            )
            await self._audit(actor_id, f"transaction.{kind}", "transaction", cur.lastrowid,
                              {"category": category, "amount": amount, "date": occurred_on})
            await self.conn.commit()
            return cur.lastrowid

    async def void_transaction(self, actor_id, tx_id, reason):
        async with self.lock:
            row = await self.fetchone("SELECT * FROM transactions WHERE id=?", (tx_id,))
            if not row:
                raise ValueError("Transaction not found.")
            if row["voided"]:
                raise ValueError("Already void.")
            if row["invoice_id"]:
                raise ValueError("This income came from an invoice payment. Void the invoice instead.")
            await self.conn.execute("UPDATE transactions SET voided=1 WHERE id=?", (tx_id,))
            await self._audit(actor_id, "transaction.void", "transaction", tx_id,
                              {"reason": reason, "amount": row["amount"]})
            await self.conn.commit()

    async def transactions(self, month: str, kind: str | None = None, limit: int = 20):
        sql = """SELECT t.*, c.name AS category FROM transactions t JOIN categories c ON c.id=t.category_id
                 WHERE t.voided=0 AND substr(t.occurred_on,1,7)=?"""
        params = [month]
        if kind:
            sql += " AND t.kind=?"
            params.append(kind)
        sql += " ORDER BY t.occurred_on DESC, t.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in await self.fetchall(sql, params)]

    async def month_report(self, month: str) -> dict:
        rows = await self.fetchall(
            """SELECT c.name, t.kind, SUM(t.amount) AS total FROM transactions t
               JOIN categories c ON c.id=t.category_id
               WHERE t.voided=0 AND substr(t.occurred_on,1,7)=?
               GROUP BY c.name, t.kind ORDER BY total DESC""",
            (month,),
        )
        income = [(r["name"], r["total"]) for r in rows if r["kind"] == "income"]
        expenses = [(r["name"], r["total"]) for r in rows if r["kind"] == "expense"]
        revenue = sum(t for _, t in income)
        spent = sum(t for _, t in expenses)
        return {"month": month, "income": income, "expenses": expenses,
                "revenue": revenue, "expense_total": spent, "net": revenue - spent}

    # ---------- audit ----------
    async def recent_audit(self, limit: int = 15):
        return [dict(r) for r in await self.fetchall(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]
