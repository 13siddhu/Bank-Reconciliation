from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APITestCase

from .models import (
    Organization,
    InvestorEntity,
    CapitalCall,
    LineItem,
    BankTransaction,
    Match,
)


class ReconciliationModelTests(TestCase):
    def setUp(self):
        self.org1 = Organization.objects.create(name="Alpha Fund", slug="alpha-fund")
        self.org2 = Organization.objects.create(name="Beta Fund", slug="beta-fund")

        self.investor1 = InvestorEntity.objects.create(
            organization=self.org1,
            name="Apex Holdings LP",
        )

        self.capital_call1 = CapitalCall.objects.create(
            organization=self.org1,
            call_number="CC-001",
            call_date=date(2026, 1, 15),
            due_date=date(2026, 1, 30),
        )

    def test_organization_slug_uniqueness(self):
        with self.assertRaises((ValidationError, IntegrityError)):
            Organization.objects.create(name="Duplicate Alpha", slug="alpha-fund")

    def test_investor_entity_uniqueness_per_organization(self):
        # Same name in different org is allowed
        InvestorEntity.objects.create(organization=self.org2, name="Apex Holdings LP")

        # Same name in same org is rejected
        with self.assertRaises((ValidationError, IntegrityError)):
            InvestorEntity.objects.create(organization=self.org1, name="Apex Holdings LP")

    def test_capital_call_call_number_uniqueness_per_org(self):
        # Same call number in different org is allowed
        CapitalCall.objects.create(
            organization=self.org2,
            call_number="CC-001",
            call_date=date(2026, 1, 15),
            due_date=date(2026, 1, 30),
        )

        # Same call number in same org is rejected
        with self.assertRaises((ValidationError, IntegrityError)):
            CapitalCall.objects.create(
                organization=self.org1,
                call_number="CC-001",
                call_date=date(2026, 2, 1),
                due_date=date(2026, 2, 15),
            )

    def test_capital_call_due_date_cannot_precede_call_date(self):
        with self.assertRaises((ValidationError, IntegrityError)):
            CapitalCall.objects.create(
                organization=self.org1,
                call_number="CC-002",
                call_date=date(2026, 2, 15),
                due_date=date(2026, 2, 10),
            )

    def test_line_item_decimal_calculations_and_status(self):
        item = LineItem.objects.create(
            organization=self.org1,
            capital_call=self.capital_call1,
            investor_entity=self.investor1,
            expected_wire_reference="WIRE-APEX-001",
            amount_called=Decimal("150000.00"),
            amount_funded=Decimal("50000.00"),
            status=LineItem.Status.PARTIALLY_FUNDED,
        )

        self.assertIsInstance(item.remaining_amount, Decimal)
        self.assertEqual(item.remaining_amount, Decimal("100000.00"))

    def test_line_item_amount_validation(self):
        # amount_called must be strictly positive
        with self.assertRaises((ValidationError, IntegrityError)):
            LineItem.objects.create(
                organization=self.org1,
                capital_call=self.capital_call1,
                investor_entity=self.investor1,
                amount_called=Decimal("0.00"),
            )

        # amount_funded cannot exceed amount_called
        with self.assertRaises((ValidationError, IntegrityError)):
            LineItem.objects.create(
                organization=self.org1,
                capital_call=self.capital_call1,
                investor_entity=self.investor1,
                amount_called=Decimal("10000.00"),
                amount_funded=Decimal("15000.00"),
            )

    def test_line_item_organization_mismatch_prevented(self):
        # CapitalCall belongs to org2, but LineItem assigned to org1
        cc_org2 = CapitalCall.objects.create(
            organization=self.org2,
            call_number="CC-BETA-01",
            call_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
        )

        with self.assertRaises(ValidationError):
            LineItem.objects.create(
                organization=self.org1,
                capital_call=cc_org2,
                investor_entity=self.investor1,
                amount_called=Decimal("50000.00"),
            )

    def test_bank_transaction_signed_amount_and_unique_reference(self):
        tx = BankTransaction.objects.create(
            organization=self.org1,
            transaction_date=date(2026, 1, 20),
            description="WIRE IN FROM APEX",
            signed_amount=Decimal("50000.00"),
            transaction_type="WIRE_IN",
            bank_reference="REF-TX-1001",
            import_metadata={"batch_id": "BATCH-1", "source": "csv"},
        )

        self.assertIsInstance(tx.signed_amount, Decimal)
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx.import_metadata["batch_id"], "BATCH-1")

        # Duplicate bank_reference in same org is rejected
        with self.assertRaises((ValidationError, IntegrityError)):
            BankTransaction.objects.create(
                organization=self.org1,
                transaction_date=date(2026, 1, 21),
                signed_amount=Decimal("20000.00"),
                bank_reference="REF-TX-1001",
            )

        # Same bank reference in another org is allowed
        tx_org2 = BankTransaction.objects.create(
            organization=self.org2,
            transaction_date=date(2026, 1, 21),
            signed_amount=Decimal("20000.00"),
            bank_reference="REF-TX-1001",
        )
        self.assertIsNotNone(tx_org2.pk)

    def test_match_supports_multiple_proposals_for_same_transaction(self):
        tx = BankTransaction.objects.create(
            organization=self.org1,
            transaction_date=date(2026, 1, 20),
            signed_amount=Decimal("50000.00"),
            bank_reference="REF-AMBIGUOUS-01",
        )
        line_item_a = LineItem.objects.create(
            organization=self.org1,
            capital_call=self.capital_call1,
            investor_entity=self.investor1,
            amount_called=Decimal("50000.00"),
        )
        investor2 = InvestorEntity.objects.create(
            organization=self.org1,
            name="Summit Capital",
        )
        line_item_b = LineItem.objects.create(
            organization=self.org1,
            capital_call=self.capital_call1,
            investor_entity=investor2,
            amount_called=Decimal("50000.00"),
        )

        # Ambiguity: two proposed matches for the single transaction
        match1 = Match.objects.create(
            organization=self.org1,
            bank_transaction=tx,
            line_item=line_item_a,
            status=Match.Status.PROPOSED,
            match_reason="Exact amount match with Candidate A",
        )
        match2 = Match.objects.create(
            organization=self.org1,
            bank_transaction=tx,
            line_item=line_item_b,
            status=Match.Status.PROPOSED,
            match_reason="Exact amount match with Candidate B",
        )

        self.assertIsNotNone(match1.pk)
        self.assertIsNotNone(match2.pk)
        self.assertEqual(tx.matches.count(), 2)

    def test_match_cross_organization_prevented(self):
        tx = BankTransaction.objects.create(
            organization=self.org1,
            transaction_date=date(2026, 1, 20),
            signed_amount=Decimal("50000.00"),
            bank_reference="REF-ORG1-TX",
        )
        inv_org2 = InvestorEntity.objects.create(organization=self.org2, name="Org2 Investor")
        cc_org2 = CapitalCall.objects.create(
            organization=self.org2,
            call_number="CC-ORG2-01",
            call_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
        )
        line_item_org2 = LineItem.objects.create(
            organization=self.org2,
            capital_call=cc_org2,
            investor_entity=inv_org2,
            amount_called=Decimal("50000.00"),
        )

        with self.assertRaises(ValidationError):
            Match.objects.create(
                organization=self.org1,
                bank_transaction=tx,
                line_item=line_item_org2,
                status=Match.Status.PROPOSED,
            )

    def test_match_only_one_approved_match_per_transaction(self):
        tx = BankTransaction.objects.create(
            organization=self.org1,
            transaction_date=date(2026, 1, 20),
            signed_amount=Decimal("50000.00"),
            bank_reference="REF-APPROVE-TX",
        )
        line_item_a = LineItem.objects.create(
            organization=self.org1,
            capital_call=self.capital_call1,
            investor_entity=self.investor1,
            amount_called=Decimal("50000.00"),
        )
        investor2 = InvestorEntity.objects.create(organization=self.org1, name="Summit Partners")
        line_item_b = LineItem.objects.create(
            organization=self.org1,
            capital_call=self.capital_call1,
            investor_entity=investor2,
            amount_called=Decimal("50000.00"),
        )

        Match.objects.create(
            organization=self.org1,
            bank_transaction=tx,
            line_item=line_item_a,
            status=Match.Status.APPROVED,
        )

        # Attempting to approve a second match for the same transaction should fail
        with self.assertRaises((ValidationError, IntegrityError)):
            with transaction.atomic():
                Match.objects.create(
                    organization=self.org1,
                    bank_transaction=tx,
                    line_item=line_item_b,
                    status=Match.Status.APPROVED,
                )

    def test_seed_demo_command_idempotency(self):
        # First execution seeds the demo dataset
        call_command("seed_demo")

        self.assertEqual(Organization.objects.filter(slug__in=["northwind", "bluebird"]).count(), 2)
        self.assertEqual(CapitalCall.objects.filter(organization__slug__in=["northwind", "bluebird"]).count(), 3)
        self.assertEqual(InvestorEntity.objects.filter(organization__slug__in=["northwind", "bluebird"]).count(), 6)

        line_items = LineItem.objects.filter(organization__slug__in=["northwind", "bluebird"])
        self.assertEqual(line_items.count(), 7)
        for item in line_items:
            self.assertEqual(item.status, LineItem.Status.OPEN)
            self.assertEqual(item.amount_funded, Decimal("0.00"))

        # Second execution must not duplicate records
        call_command("seed_demo")

        self.assertEqual(Organization.objects.filter(slug__in=["northwind", "bluebird"]).count(), 2)
        self.assertEqual(CapitalCall.objects.filter(organization__slug__in=["northwind", "bluebird"]).count(), 3)
        self.assertEqual(InvestorEntity.objects.filter(organization__slug__in=["northwind", "bluebird"]).count(), 6)
        self.assertEqual(LineItem.objects.filter(organization__slug__in=["northwind", "bluebird"]).count(), 7)


class BankStatementUploadTests(APITestCase):
    def setUp(self):
        self.org_northwind = Organization.objects.create(name="Northwind Ventures", slug="northwind")
        self.org_bluebird = Organization.objects.create(name="Bluebird Capital", slug="bluebird")
        self.upload_url = "/api/transactions/upload/"

    def _make_csv_file(self, content: str, filename="statement.csv"):
        return SimpleUploadedFile(filename, content.encode("utf-8"), content_type="text/csv")

    def test_successful_csv_upload_and_debit_handling(self):
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-03,WIRE INCOMING RAHUL SHAH TRUST,\"25,000.00\",credit,FT20260403A\n"
            "2026-04-05,BANK FEE WIRE PROCESSING,-25.00,debit,FEE20260405\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["imported_count"], 2)
        self.assertEqual(response.data["ignored_count"], 1)
        self.assertEqual(response.data["needs_review_count"], 1)

        # Verify database records
        credit_tx = BankTransaction.objects.get(bank_reference="FT20260403A", organization=self.org_northwind)
        self.assertEqual(credit_tx.signed_amount, Decimal("25000.00"))
        self.assertEqual(credit_tx.status, BankTransaction.Status.NEEDS_REVIEW)

        debit_tx = BankTransaction.objects.get(bank_reference="FEE20260405", organization=self.org_northwind)
        self.assertEqual(debit_tx.signed_amount, Decimal("-25.00"))
        self.assertEqual(debit_tx.status, BankTransaction.Status.IGNORED)

    def test_missing_required_headers(self):
        # Missing 'reference' header
        csv_content = "date,description,amount,type\n2026-04-03,WIRE INCOMING,25000.00,credit\n"
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["status"], "error")
        self.assertTrue(any("reference" in err["message"] for err in response.data["errors"]))

    def test_invalid_date(self):
        # April 31 does not exist
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-31,WIRE INCOMING,500.00,credit,REF-DATE-BAD\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 400)
        error = response.data["errors"][0]
        self.assertEqual(error["line"], 2)
        self.assertEqual(error["column"], "date")
        self.assertIn("Invalid date", error["message"])

    def test_invalid_zero_and_negative_amounts(self):
        # Non-numeric, zero, and negative credit
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-01,TEST NON NUMERIC,abc,credit,REF-AMT-1\n"
            "2026-04-02,TEST ZERO AMOUNT,0.00,credit,REF-AMT-2\n"
            "2026-04-03,TEST NEGATIVE CREDIT,-500.00,credit,REF-AMT-3\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 400)
        lines = [err["line"] for err in response.data["errors"]]
        self.assertIn(2, lines)
        self.assertIn(3, lines)
        self.assertIn(4, lines)

    def test_missing_required_values(self):
        csv_content = (
            "date,description,amount,type,reference\n"
            ",WIRE INCOMING,25000.00,credit,REF-MISS-DATE\n"
            "2026-04-03,,25000.00,credit,REF-MISS-DESC\n"
            "2026-04-03,WIRE INCOMING,25000.00,credit,\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(len(response.data["errors"]) >= 3)

    def test_duplicate_transactions_within_file(self):
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-03,WIRE 1,25000.00,credit,REF-FILE-DUP\n"
            "2026-04-04,WIRE 2,10000.00,credit,REF-FILE-DUP\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 400)
        error = response.data["errors"][0]
        self.assertEqual(error["line"], 3)
        self.assertEqual(error["column"], "reference")
        self.assertIn("Duplicate transaction reference", error["message"])

    def test_duplicate_transactions_already_in_database(self):
        BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 1),
            description="EXISTING TX",
            signed_amount=Decimal("5000.00"),
            transaction_type="credit",
            bank_reference="REF-ALREADY-IN-DB",
        )

        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-02,NEW WIRE,5000.00,credit,REF-ALREADY-IN-DB\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 400)
        error = response.data["errors"][0]
        self.assertEqual(error["line"], 2)
        self.assertIn("already exists", error["message"])

    def test_organization_isolation_and_x_organization_header(self):
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-02,WIRE,5000.00,credit,REF-CROSS-ORG\n"
        )

        # 1. Missing header
        res_missing = self.client.post(self.upload_url, {"file": self._make_csv_file(csv_content)}, format="multipart")
        self.assertEqual(res_missing.status_code, 400)
        self.assertIn("X-Organization header is required", res_missing.data["error"])

        # 2. Unknown organization
        res_unknown = self.client.post(
            self.upload_url,
            {"file": self._make_csv_file(csv_content)},
            format="multipart",
            HTTP_X_ORGANIZATION="nonexistent-org",
        )
        self.assertEqual(res_unknown.status_code, 404)

        # 3. Same reference in two different organizations is allowed
        res_northwind = self.client.post(
            self.upload_url,
            {"file": self._make_csv_file(csv_content)},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )
        self.assertEqual(res_northwind.status_code, 201)

        res_bluebird = self.client.post(
            self.upload_url,
            {"file": self._make_csv_file(csv_content)},
            format="multipart",
            HTTP_X_ORGANIZATION="bluebird",
        )
        self.assertEqual(res_bluebird.status_code, 201)
        self.assertEqual(BankTransaction.objects.filter(bank_reference="REF-CROSS-ORG").count(), 2)

    def test_atomicity_rejects_all_when_one_row_fails(self):
        # Row 1 valid, Row 2 invalid date, Row 3 valid
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-01,VALID WIRE 1,1000.00,credit,REF-ATOMIC-1\n"
            "2026-04-31,INVALID DATE,2000.00,credit,REF-ATOMIC-2\n"
            "2026-04-03,VALID WIRE 3,3000.00,credit,REF-ATOMIC-3\n"
        )
        csv_file = self._make_csv_file(csv_content)

        initial_count = BankTransaction.objects.filter(organization=self.org_northwind).count()
        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 400)
        final_count = BankTransaction.objects.filter(organization=self.org_northwind).count()
        # No records should be created
        self.assertEqual(final_count, initial_count)

    def test_file_size_and_type_validation(self):
        # 1. Invalid file extension
        txt_file = SimpleUploadedFile("statement.txt", b"some,content", content_type="text/plain")
        res_type = self.client.post(
            self.upload_url,
            {"file": txt_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )
        self.assertEqual(res_type.status_code, 400)
        self.assertIn("Only CSV files", res_type.data["error"])

        # 2. Empty file
        empty_file = SimpleUploadedFile("empty.csv", b"", content_type="text/csv")
        res_empty = self.client.post(
            self.upload_url,
            {"file": empty_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )
        self.assertEqual(res_empty.status_code, 400)

    def test_utf8_bom_handling(self):
        # UTF-8 with BOM (\xef\xbb\xbf)
        csv_content = "date,description,amount,type,reference\n2026-04-01,BOM WIRE,1000.00,credit,REF-BOM-1\n"
        bom_bytes = b"\xef\xbb\xbf" + csv_content.encode("utf-8")
        bom_file = SimpleUploadedFile("bom_statement.csv", bom_bytes, content_type="text/csv")

        response = self.client.post(
            self.upload_url,
            {"file": bom_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(BankTransaction.objects.filter(bank_reference="REF-BOM-1").exists())
