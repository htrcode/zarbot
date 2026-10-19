"""Regression tests for the ticket, sales and digital-delivery flows.

Run with: python -m unittest discover -s tests -v
"""
import os
import shutil
import tempfile
import types
import unittest

from database import Database
from pdf_gen import invoice_pdf
from utils import parse_items, parse_when
from views import (build_deal_view, build_ticket_view, delivery_items, delivery_text,
                   transaction_details)


class CoreFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.mkdtemp(prefix="zarbot-test-")
        self.db = Database(os.path.join(self.dir, "test.db"))
        await self.db.connect()
        self.bot = types.SimpleNamespace(db=self.db)

    async def asyncTearDown(self):
        await self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_item_parsing(self):
        items = parse_items(
            "VPS 4GB | 1 | 700 | type=VPS; node=Ryzen; plan=4GB; id=HX-1122; "
            "link=https://panel.example.com/x; Instant setup"
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["product_type"], "VPS")
        self.assertEqual(item["node"], "Ryzen")
        self.assertEqual(item["plan"], "4GB")
        self.assertEqual(item["server_id"], "HX-1122")
        self.assertEqual(item["delivery_link"], "https://panel.example.com/x")
        self.assertEqual(item["details"], "Instant setup")
        self.assertEqual(item["unit_price"], 70000)

    def test_item_parsing_rejects_bad_quantity(self):
        with self.assertRaises(ValueError):
            parse_items("VPS | 0 | 700 | type=VPS")
        with self.assertRaises(ValueError):
            parse_items("not enough fields")

    async def test_ticket_deal_invoice_and_payment(self):
        items = parse_items("VPS 4GB | 1 | 700 | type=VPS; node=Ryzen; plan=4GB; "
                            "id=HX-1122; link=https://panel.example.com/x")
        ticket_id = await self.db.create_ticket(
            1, subject="Need a 4GB VPS", kind="enquiry", priority="high",
            client_discord_id=2, client_name="Flame", channel_id=999,
            budget=20000, service="VPS", note="budget 200",
        )
        ticket = await self.db.get_ticket(ticket_id)
        deal = await self.db.deal_by_ticket(ticket_id)
        self.assertEqual(ticket["channel_id"], 999)
        self.assertEqual(deal["stage"], "new")
        self.assertEqual(deal["budget"], 20000)

        invoice_id = await self.db.create_invoice(
            1, client_discord_id=2, client_name="Flame", items=items, due_days=7,
            tax_percent=18, discount_cents=0, category="Hosting Revenue",
            notes="Thanks", ticket_channel_id=999,
        )
        invoice = await self.db.get_invoice(invoice_id)
        self.assertEqual(invoice["total"], 82600)  # 700 + 18% tax
        self.assertEqual(invoice["items"][0]["product_type"], "VPS")

        deal = await self.db.deal_by_ticket(ticket_id)
        self.assertEqual(deal["stage"], "quoted")
        self.assertEqual(deal["invoice_id"], invoice_id)
        self.assertEqual(deal["value"], invoice["total"])

        payment_id = await self.db.add_payment(1, invoice_id, invoice["balance"], "UPI", "full")
        invoice = await self.db.get_invoice(invoice_id)
        deal = await self.db.deal_by_ticket(ticket_id)
        self.assertEqual(invoice["status"], "paid")
        self.assertEqual(deal["stage"], "won")
        self.assertEqual(await self.db.gross_sales(), invoice["total"])

        receipt = transaction_details(
            invoice, payment=invoice["payments"][0], gross=await self.db.gross_sales(),
            ticket_label="ticket-0001",
        )
        self.assertIn(f"Payment Received-{payment_id}", receipt)
        self.assertIn("Customer Name : Flame", receipt)
        self.assertIn("Server ID : HX-1122", receipt)
        self.assertIn("Plan : 4GB", receipt)
        self.assertIn("Gross Sales", receipt)

        view = await build_ticket_view(self.bot, ticket_id)
        deal_view = await build_deal_view(self.bot, deal["id"])
        self.assertIsNotNone(view)
        self.assertIsNotNone(deal_view)

        pdf = invoice_pdf(invoice)
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 3000)

    async def test_followup_and_delivery(self):
        items = parse_items("Bot setup | 1 | 600 | type=Bot; link=https://example.com/bot.zip")
        ticket_id = await self.db.create_ticket(
            1, subject="Bot setup", kind="purchase", priority="normal",
            client_discord_id=3, client_name="Shiv", channel_id=1000,
        )
        deal = await self.db.deal_by_ticket(ticket_id)
        when = parse_when("+2d")
        await self.db.set_deal_followup(1, deal["id"], when, "Check setup")
        due = await self.db.deals_due(parse_when("+3d"))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["next_follow_up"], when)

        invoice_id = await self.db.create_invoice(
            1, client_discord_id=3, client_name="Shiv", items=items, due_days=7,
            tax_percent=0, discount_cents=0, category="Hosting Revenue",
            notes="", ticket_channel_id=1000,
        )
        invoice = await self.db.get_invoice(invoice_id)
        self.assertEqual(len(delivery_items(invoice)), 1)
        self.assertIn("example.com/bot.zip", delivery_text(invoice))
        await self.db.mark_delivered(1, invoice_id, "sent")
        self.assertIsNotNone((await self.db.get_invoice(invoice_id))["delivered_at"])

        pipeline = {row["stage"]: row for row in await self.db.pipeline()}
        self.assertEqual(pipeline["quoted"]["count"], 1)
        self.assertEqual(pipeline["quoted"]["value"], 60000)


if __name__ == "__main__":
    unittest.main()
