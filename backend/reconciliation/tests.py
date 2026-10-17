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
from .services import (
    normalize_reference,
    normalize_name,
    name_matches_description,
    match_credit_transaction,
    run_matching_for_transactions,
    run_matching_for_organization,
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
            transaction_type="credit",
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

    def test_seed_demo_command_idempotency(self):
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
        self.assertEqual(LineItem.objects.filter(organization__slug__in=["northwind", "bluebird"]).count(), 7)


class NormalizationTests(TestCase):
    def test_reference_normalization(self):
        self.assertEqual(normalize_reference("cc-1003"), "CC1003")
        self.assertEqual(normalize_reference("CC-1001"), "CC1001")
        self.assertEqual(normalize_reference("cc1001"), "CC1001")
        self.assertEqual(normalize_reference("  cc-2001!  "), "CC2001")
        self.assertEqual(normalize_reference(""), "")

    def test_name_normalization_and_preservation_of_trust(self):
        self.assertEqual(normalize_name("Haller Capital LLC"), "HALLER CAPITAL")
        self.assertEqual(normalize_name("Meridian Partners LP"), "MERIDIAN PARTNERS")
        self.assertEqual(normalize_name("Global Tech Ltd"), "GLOBAL TECH")
        self.assertEqual(normalize_name("Acme Corp Inc."), "ACME CORP")
        self.assertEqual(normalize_name("Rahul Shah Trust"), "RAHUL SHAH TRUST")
        self.assertEqual(normalize_name("Patel Family Trust"), "PATEL FAMILY TRUST")
        self.assertEqual(normalize_name("Cedar Grove Fund LP"), "CEDAR GROVE FUND")

    def test_name_whole_word_matching_in_description(self):
        self.assertTrue(name_matches_description("Patel Family Trust", "WIRE INCOMING PATEL FAMILY TRUST"))
        self.assertTrue(name_matches_description("Meridian Partners LP", "WIRE INCOMING MERIDIAN PARTNERS"))
        self.assertFalse(name_matches_description("Patel", "WIRE INCOMING PATELSON"))
        self.assertFalse(name_matches_description("Haller Capital", "WIRE INCOMING HALLER CAPITALISTS"))


class BankStatementUploadTests(APITestCase):
    def setUp(self):
        self.org_northwind = Organization.objects.create(name="Northwind Ventures", slug="northwind")
        self.org_bluebird = Organization.objects.create(name="Bluebird Capital", slug="bluebird")
        self.upload_url = "/api/transactions/upload/"

    def _make_csv_file(self, content: str, filename="statement.csv"):
        return SimpleUploadedFile(filename, content.encode("utf-8"), content_type="text/csv")

    def test_missing_required_headers_rejects_whole_file_with_400(self):
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
        self.assertEqual(BankTransaction.objects.filter(organization=self.org_northwind).count(), 0)

    def test_partial_import_with_valid_invalid_and_duplicates(self):
        """
        Valid rows are imported, invalid rows are skipped and reported,
        and duplicates inside file are skipped and reported.
        """
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-01,VALID ROW 1,1000.00,credit,REF-VAL-1\n"
            "2026-04-31,INVALID DATE,2000.00,credit,REF-INV-DATE\n"
            "2026-04-02,VALID ROW 2,3000.00,credit,REF-VAL-2\n"
            "2026-04-02,DUPLICATE IN FILE,3000.00,credit,REF-VAL-1\n"
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
        self.assertEqual(response.data["duplicates_count"], 1)

        errors = response.data["errors"]
        error_lines = [e["line"] for e in errors]
        self.assertIn(3, error_lines)  # line 3 is invalid date
        self.assertIn(5, error_lines)  # line 5 is duplicate REF-VAL-1

        # Valid rows are saved in DB
        self.assertTrue(BankTransaction.objects.filter(bank_reference="REF-VAL-1").exists())
        self.assertTrue(BankTransaction.objects.filter(bank_reference="REF-VAL-2").exists())
        self.assertFalse(BankTransaction.objects.filter(bank_reference="REF-INV-DATE").exists())

    def test_correct_duplicate_counts_and_line_numbers_for_db_duplicates(self):
        BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 1),
            description="EXISTING TX",
            signed_amount=Decimal("5000.00"),
            transaction_type="credit",
            bank_reference="REF-IN-DB",
        )

        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-02,NEW WIRE,1000.00,credit,REF-NEW\n"
            "2026-04-03,EXISTING WIRE,5000.00,credit,REF-IN-DB\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["imported_count"], 1)
        self.assertEqual(response.data["duplicates_count"], 1)
        self.assertEqual(response.data["errors"][0]["line"], 3)

    def test_debit_transactions_saved_as_ignored_and_not_matched(self):
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-05,BANK FEE WIRE PROCESSING,-25.00,debit,REF-FEE-1\n"
        )
        csv_file = self._make_csv_file(csv_content)

        response = self.client.post(
            self.upload_url,
            {"file": csv_file},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )

        self.assertEqual(response.status_code, 201)
        tx = BankTransaction.objects.get(bank_reference="REF-FEE-1")
        self.assertEqual(tx.status, BankTransaction.Status.IGNORED)
        self.assertEqual(tx.matches.count(), 0)

    def test_organization_isolation_in_import(self):
        csv_content = (
            "date,description,amount,type,reference\n"
            "2026-04-02,WIRE,5000.00,credit,REF-SAME\n"
        )

        # Import in Northwind
        res1 = self.client.post(
            self.upload_url,
            {"file": self._make_csv_file(csv_content)},
            format="multipart",
            HTTP_X_ORGANIZATION="northwind",
        )
        self.assertEqual(res1.status_code, 201)

        # Same reference can be imported in Bluebird
        res2 = self.client.post(
            self.upload_url,
            {"file": self._make_csv_file(csv_content)},
            format="multipart",
            HTTP_X_ORGANIZATION="bluebird",
        )
        self.assertEqual(res2.status_code, 201)
        self.assertEqual(BankTransaction.objects.filter(bank_reference="REF-SAME").count(), 2)


class MatchingEngineTests(TestCase):
    def setUp(self):
        self.org_northwind = Organization.objects.create(name="Northwind Ventures", slug="northwind")
        self.org_bluebird = Organization.objects.create(name="Bluebird Capital", slug="bluebird")

        # Northwind Capital Call #1
        self.call1 = CapitalCall.objects.create(
            organization=self.org_northwind,
            call_number="#1",
            call_date=date(2026, 3, 25),
            due_date=date(2026, 4, 10),
        )

        # Investors
        self.inv_haller = InvestorEntity.objects.create(organization=self.org_northwind, name="Haller Capital LLC")
        self.inv_rahul = InvestorEntity.objects.create(organization=self.org_northwind, name="Rahul Shah Trust")
        self.inv_patel = InvestorEntity.objects.create(organization=self.org_northwind, name="Patel Family Trust")
        self.inv_meridian = InvestorEntity.objects.create(organization=self.org_northwind, name="Meridian Partners LP")
        self.inv_oakridge = InvestorEntity.objects.create(organization=self.org_northwind, name="Oakridge Holdings LLC")

        # Line items
        self.li_haller = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call1,
            investor_entity=self.inv_haller,
            expected_wire_reference="CC-1001",
            amount_called=Decimal("27500.00"),
            amount_funded=Decimal("0.00"),
        )
        self.li_rahul = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call1,
            investor_entity=self.inv_rahul,
            expected_wire_reference="CC-1002",
            amount_called=Decimal("25000.00"),
            amount_funded=Decimal("0.00"),
        )
        self.li_patel = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call1,
            investor_entity=self.inv_patel,
            expected_wire_reference="CC-1003",
            amount_called=Decimal("12500.00"),
            amount_funded=Decimal("0.00"),
        )
        self.li_meridian = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call1,
            investor_entity=self.inv_meridian,
            expected_wire_reference="CC-1004",
            amount_called=Decimal("15000.00"),
            amount_funded=Decimal("0.00"),
        )
        self.li_oakridge = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call1,
            investor_entity=self.inv_oakridge,
            expected_wire_reference="CC-1005",
            amount_called=Decimal("15000.00"),
            amount_funded=Decimal("0.00"),
        )

    def test_reference_match_single_candidate_and_partial_payment(self):
        # Full payment with reference
        tx1 = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE INCOMING RAHUL SHAH TRUST REF CC-1002",
            signed_amount=Decimal("25000.00"),
            transaction_type="credit",
            bank_reference="FT20260403A",
        )
        match_credit_transaction(tx1, self.org_northwind)
        tx1.refresh_from_db()
        self.assertEqual(tx1.status, BankTransaction.Status.SUGGESTED)
        self.assertEqual(tx1.matches.count(), 1)
        match1 = tx1.matches.first()
        self.assertEqual(match1.line_item, self.li_rahul)
        self.assertEqual(match1.status, Match.Status.PROPOSED)
        self.assertIn("CC-1002", match1.match_reason)

        # Partial payment with reference
        tx2 = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 4),
            description="ACH CREDIT HALLER CAPITAL LLC CC-1001",
            signed_amount=Decimal("20000.00"),
            transaction_type="credit",
            bank_reference="ACH20260404B",
        )
        match_credit_transaction(tx2, self.org_northwind)
        tx2.refresh_from_db()
        self.assertEqual(tx2.status, BankTransaction.Status.SUGGESTED)
        self.assertEqual(tx2.matches.count(), 1)
        match2 = tx2.matches.first()
        self.assertEqual(match2.line_item, self.li_haller)
        self.assertEqual(match2.status, Match.Status.PROPOSED)
        self.assertIn("partial payment", match2.match_reason)

    def test_reference_overpayment_becomes_needs_review_with_no_matches(self):
        # Remaining is 12,500.00; payment is 13,000.00 with reference CC-1003
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 15),
            description="WIRE INCOMING PATEL FAMILY TRUST CC-1003",
            signed_amount=Decimal("13000.00"),
            transaction_type="credit",
            bank_reference="FT20260415J",
        )
        match_credit_transaction(tx, self.org_northwind)
        tx.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx.matches.count(), 0)
        self.assertIn("Overpayment", tx.import_metadata.get("review_reason", ""))

    def test_multiple_reference_candidates_proposes_all_and_marks_needs_review(self):
        # Create a second line item with reference CC-1005A that also matches a wire mentioning CC-1005
        li_extra = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call1,
            investor_entity=self.inv_oakridge,
            expected_wire_reference="CC-1005",
            amount_called=Decimal("10000.00"),
            amount_funded=Decimal("0.00"),
        )
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 11),
            description="WIRE WITH REF CC-1005",
            signed_amount=Decimal("10000.00"),
            transaction_type="credit",
            bank_reference="FT-AMBIG-REF",
        )
        match_credit_transaction(tx, self.org_northwind)
        tx.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx.matches.count(), 2)
        for m in tx.matches.all():
            self.assertEqual(m.status, Match.Status.PROPOSED)
            self.assertIn("ambiguous", m.match_reason)

    def test_name_and_amount_match_single_candidate(self):
        # No reference in description, but name 'PATEL FAMILY TRUST' and amount 12500.00 matches
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 7),
            description="WIRE INCOMING PATEL FAMILY TRUST",
            signed_amount=Decimal("12500.00"),
            transaction_type="credit",
            bank_reference="FT20260407C",
        )
        match_credit_transaction(tx, self.org_northwind)
        tx.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.SUGGESTED)
        self.assertEqual(tx.matches.count(), 1)
        match = tx.matches.first()
        self.assertEqual(match.line_item, self.li_patel)
        self.assertIn("Investor name", match.match_reason)

    def test_name_and_amount_multiple_candidates(self):
        # Call #2 with same investor Oakridge Holdings and same amount 15000.00
        call2 = CapitalCall.objects.create(
            organization=self.org_northwind,
            call_number="#2",
            call_date=date(2026, 4, 1),
            due_date=date(2026, 4, 20),
        )
        li_oakridge_2 = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=call2,
            investor_entity=self.inv_oakridge,
            expected_wire_reference="CC-1006",
            amount_called=Decimal("15000.00"),
            amount_funded=Decimal("0.00"),
        )

        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 11),
            description="WIRE INCOMING OAKRIDGE HOLDINGS LLC",
            signed_amount=Decimal("15000.00"),
            transaction_type="credit",
            bank_reference="FT20260411G",
        )
        match_credit_transaction(tx, self.org_northwind)
        tx.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx.matches.count(), 2)

    def test_name_and_amount_absent_candidates(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 9),
            description="UNKNOWN INCOMING WIRE",
            signed_amount=Decimal("9999.99"),
            transaction_type="credit",
            bank_reference="FT20260409D",
        )
        match_credit_transaction(tx, self.org_northwind)
        tx.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx.matches.count(), 0)

    def test_reference_matching_takes_precedence_over_name_matching(self):
        # Description has name 'Patel Family Trust' (which has 12500) BUT reference CC-1001 (Haller)
        # Reference check must take precedence!
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 4),
            description="PATEL FAMILY TRUST REF CC-1001",
            signed_amount=Decimal("20000.00"),
            transaction_type="credit",
            bank_reference="REF-PRECEDENCE",
        )
        match_credit_transaction(tx, self.org_northwind)
        tx.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.SUGGESTED)
        self.assertEqual(tx.matches.count(), 1)
        self.assertEqual(tx.matches.first().line_item, self.li_haller)

    def test_eligible_line_item_call_date_and_remaining_amount_rules(self):
        # 1. Call date in future relative to transaction date: ineligible
        call_future = CapitalCall.objects.create(
            organization=self.org_northwind,
            call_number="#99",
            call_date=date(2026, 5, 1),
            due_date=date(2026, 5, 20),
        )
        li_future = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=call_future,
            investor_entity=self.inv_rahul,
            expected_wire_reference="CC-9901",
            amount_called=Decimal("10000.00"),
            amount_funded=Decimal("0.00"),
        )
        tx_early = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 1),  # earlier than 2026-05-01
            description="WIRE WITH CC-9901",
            signed_amount=Decimal("10000.00"),
            transaction_type="credit",
            bank_reference="REF-EARLY",
        )
        match_credit_transaction(tx_early, self.org_northwind)
        tx_early.refresh_from_db()
        self.assertEqual(tx_early.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx_early.matches.count(), 0)

        # 2. Line item with remaining_amount = 0: ineligible
        self.li_rahul.amount_funded = Decimal("25000.00")
        self.li_rahul.status = LineItem.Status.FUNDED
        self.li_rahul.save()

        tx_funded = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 5),
            description="WIRE INCOMING RAHUL SHAH REF CC-1002",
            signed_amount=Decimal("25000.00"),
            transaction_type="credit",
            bank_reference="REF-FUNDED-CALL",
        )
        match_credit_transaction(tx_funded, self.org_northwind)
        tx_funded.refresh_from_db()
        self.assertEqual(tx_funded.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx_funded.matches.count(), 0)

    def test_organization_isolation_in_matching(self):
        # Bluebird line item with reference CC-2001
        call_blue = CapitalCall.objects.create(
            organization=self.org_bluebird,
            call_number="#1",
            call_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        inv_cedar = InvestorEntity.objects.create(organization=self.org_bluebird, name="Cedar Grove Fund LP")
        li_cedar = LineItem.objects.create(
            organization=self.org_bluebird,
            capital_call=call_blue,
            investor_entity=inv_cedar,
            expected_wire_reference="CC-2001",
            amount_called=Decimal("40000.00"),
            amount_funded=Decimal("0.00"),
        )

        # Uploaded in Northwind with CC-2001 in description
        tx_north = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 16),
            description="WIRE INCOMING CEDAR GROVE FUND CC-2001",
            signed_amount=Decimal("40000.00"),
            transaction_type="credit",
            bank_reference="FT20260416K",
        )
        match_credit_transaction(tx_north, self.org_northwind)
        tx_north.refresh_from_db()
        # Must not match Bluebird's line item!
        self.assertEqual(tx_north.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(tx_north.matches.count(), 0)

    def test_repeated_matching_idempotency_no_duplicate_proposals(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE INCOMING RAHUL SHAH TRUST REF CC-1002",
            signed_amount=Decimal("25000.00"),
            transaction_type="credit",
            bank_reference="FT-IDEMPOTENT",
        )
        match_credit_transaction(tx, self.org_northwind)
        self.assertEqual(Match.objects.filter(bank_transaction=tx).count(), 1)

        # Second matching run on same transaction
        match_credit_transaction(tx, self.org_northwind)
        self.assertEqual(Match.objects.filter(bank_transaction=tx).count(), 1)

    def test_approved_matches_remain_unchanged_on_rematch(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE INCOMING RAHUL SHAH TRUST REF CC-1002",
            signed_amount=Decimal("25000.00"),
            transaction_type="credit",
            bank_reference="FT-APPROVED-TX",
            status=BankTransaction.Status.RECONCILED,
        )
        approved_match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.li_rahul,
            status=Match.Status.APPROVED,
            match_reason="Approved earlier",
        )

        # Re-run matching
        match_credit_transaction(tx, self.org_northwind)
        tx.refresh_from_db()
        approved_match.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.RECONCILED)
        self.assertEqual(approved_match.status, Match.Status.APPROVED)
        self.assertEqual(approved_match.match_reason, "Approved earlier")


class ReconciliationActionsAPITests(APITestCase):
    def setUp(self):
        self.org_northwind = Organization.objects.create(name="Northwind Ventures", slug="northwind")
        self.org_bluebird = Organization.objects.create(name="Bluebird Capital", slug="bluebird")

        self.call = CapitalCall.objects.create(
            organization=self.org_northwind,
            call_number="#1",
            call_date=date(2026, 3, 25),
            due_date=date(2026, 4, 10),
        )

        self.investor = InvestorEntity.objects.create(
            organization=self.org_northwind,
            name="Rahul Shah Trust",
        )

        self.line_item = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call,
            investor_entity=self.investor,
            expected_wire_reference="CC-1002",
            amount_called=Decimal("25000.00"),
            amount_funded=Decimal("0.00"),
            status=LineItem.Status.OPEN,
        )

    def test_successful_approval_and_correct_funding_totals(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE INCOMING RAHUL SHAH TRUST REF CC-1002",
            signed_amount=Decimal("25000.00"),
            transaction_type="credit",
            bank_reference="FT-APPROVE-1",
            status=BankTransaction.Status.SUGGESTED,
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
            status=Match.Status.PROPOSED,
            match_reason="Exact match",
        )

        url = f"/api/matches/{match.id}/approve/"
        response = self.client.post(url, HTTP_X_ORGANIZATION="northwind")

        self.assertEqual(response.status_code, 200)
        self.line_item.refresh_from_db()
        tx.refresh_from_db()
        match.refresh_from_db()

        self.assertEqual(self.line_item.amount_funded, Decimal("25000.00"))
        self.assertEqual(self.line_item.remaining_amount, Decimal("0.00"))
        self.assertEqual(self.line_item.status, LineItem.Status.FUNDED)
        self.assertEqual(tx.status, BankTransaction.Status.RECONCILED)
        self.assertEqual(match.status, Match.Status.APPROVED)

    def test_partial_funding_and_full_funding_status_transitions(self):
        # Line item for 20,000.00
        li = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call,
            investor_entity=self.investor,
            expected_wire_reference="CC-PARTIAL",
            amount_called=Decimal("20000.00"),
            amount_funded=Decimal("0.00"),
            status=LineItem.Status.OPEN,
        )

        # Payment 1: 15,000.00 (partial)
        tx1 = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 1),
            description="WIRE PARTIAL 1",
            signed_amount=Decimal("15000.00"),
            transaction_type="credit",
            bank_reference="FT-P1",
            status=BankTransaction.Status.SUGGESTED,
        )
        m1 = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx1,
            line_item=li,
            status=Match.Status.PROPOSED,
        )

        res1 = self.client.post(f"/api/matches/{m1.id}/approve/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res1.status_code, 200)
        li.refresh_from_db()
        self.assertEqual(li.amount_funded, Decimal("15000.00"))
        self.assertEqual(li.remaining_amount, Decimal("5000.00"))
        self.assertEqual(li.status, LineItem.Status.PARTIALLY_FUNDED)

        # Payment 2: 5,000.00 (completes funding)
        tx2 = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 2),
            description="WIRE PARTIAL 2",
            signed_amount=Decimal("5000.00"),
            transaction_type="credit",
            bank_reference="FT-P2",
            status=BankTransaction.Status.SUGGESTED,
        )
        m2 = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx2,
            line_item=li,
            status=Match.Status.PROPOSED,
        )

        res2 = self.client.post(f"/api/matches/{m2.id}/approve/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res2.status_code, 200)
        li.refresh_from_db()
        self.assertEqual(li.amount_funded, Decimal("20000.00"))
        self.assertEqual(li.remaining_amount, Decimal("0.00"))
        self.assertEqual(li.status, LineItem.Status.FUNDED)

    def test_overpayment_rejected_with_409_and_no_database_changes(self):
        # Line item has remaining 5,000.00
        self.line_item.amount_called = Decimal("25000.00")
        self.line_item.amount_funded = Decimal("20000.00")
        self.line_item.status = LineItem.Status.PARTIALLY_FUNDED
        self.line_item.save()

        # Transaction tries to fund 10,000.00
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE OVERPAY",
            signed_amount=Decimal("10000.00"),
            transaction_type="credit",
            bank_reference="FT-OVER",
            status=BankTransaction.Status.NEEDS_REVIEW,
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
            status=Match.Status.PROPOSED,
        )

        url = f"/api/matches/{match.id}/approve/"
        response = self.client.post(url, HTTP_X_ORGANIZATION="northwind")

        self.assertEqual(response.status_code, 409)
        self.assertIn("exceeds remaining", response.data["error"])

        self.line_item.refresh_from_db()
        tx.refresh_from_db()
        match.refresh_from_db()

        # No database records changed
        self.assertEqual(self.line_item.amount_funded, Decimal("20000.00"))
        self.assertEqual(match.status, Match.Status.PROPOSED)
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)

    def test_competing_proposals_rejected_after_approval(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE AMBIGUOUS",
            signed_amount=Decimal("15000.00"),
            transaction_type="credit",
            bank_reference="FT-COMPETING",
            status=BankTransaction.Status.NEEDS_REVIEW,
        )
        inv2 = InvestorEntity.objects.create(organization=self.org_northwind, name="Another LP")
        li2 = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call,
            investor_entity=inv2,
            expected_wire_reference="CC-COMP-2",
            amount_called=Decimal("15000.00"),
            amount_funded=Decimal("0.00"),
            status=LineItem.Status.OPEN,
        )

        match1 = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
            status=Match.Status.PROPOSED,
        )
        match2 = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=li2,
            status=Match.Status.PROPOSED,
        )

        response = self.client.post(f"/api/matches/{match1.id}/approve/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(response.status_code, 200)

        match1.refresh_from_db()
        match2.refresh_from_db()
        tx.refresh_from_db()

        self.assertEqual(match1.status, Match.Status.APPROVED)
        self.assertEqual(match2.status, Match.Status.REJECTED)
        self.assertEqual(tx.status, BankTransaction.Status.RECONCILED)

    def test_double_approval_prevented(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE ONCE",
            signed_amount=Decimal("25000.00"),
            transaction_type="credit",
            bank_reference="FT-DOUBLE",
            status=BankTransaction.Status.SUGGESTED,
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
            status=Match.Status.PROPOSED,
        )

        url = f"/api/matches/{match.id}/approve/"
        res1 = self.client.post(url, HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res1.status_code, 200)

        # Second approval must return 409
        res2 = self.client.post(url, HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res2.status_code, 409)

        self.line_item.refresh_from_db()
        self.assertEqual(self.line_item.amount_funded, Decimal("25000.00"))

    def test_rejection_with_and_without_other_proposed_matches_remaining(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE MULTI MATCH",
            signed_amount=Decimal("10000.00"),
            transaction_type="credit",
            bank_reference="FT-REJECT-TX",
            status=BankTransaction.Status.NEEDS_REVIEW,
        )
        inv2 = InvestorEntity.objects.create(organization=self.org_northwind, name="Second LP")
        li2 = LineItem.objects.create(
            organization=self.org_northwind,
            capital_call=self.call,
            investor_entity=inv2,
            expected_wire_reference="CC-REJ-2",
            amount_called=Decimal("10000.00"),
            amount_funded=Decimal("0.00"),
        )
        m1 = Match.objects.create(organization=self.org_northwind, bank_transaction=tx, line_item=self.line_item, status=Match.Status.PROPOSED)
        m2 = Match.objects.create(organization=self.org_northwind, bank_transaction=tx, line_item=li2, status=Match.Status.PROPOSED)

        # 1. Reject first match: m2 is still proposed, so tx status preserved
        res1 = self.client.post(f"/api/matches/{m1.id}/reject/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res1.status_code, 200)
        m1.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(m1.status, Match.Status.REJECTED)
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)
        self.assertEqual(self.line_item.amount_funded, Decimal("0.00"))

        # 2. Reject second match: no proposed matches remain -> tx becomes needs_review
        res2 = self.client.post(f"/api/matches/{m2.id}/reject/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res2.status_code, 200)
        m2.refresh_from_db()
        tx.refresh_from_db()
        self.assertEqual(m2.status, Match.Status.REJECTED)
        self.assertEqual(tx.status, BankTransaction.Status.NEEDS_REVIEW)

    def test_ignore_action_rejects_outstanding_proposals(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE TO IGNORE",
            signed_amount=Decimal("10000.00"),
            transaction_type="credit",
            bank_reference="FT-IGNORE-TX",
            status=BankTransaction.Status.SUGGESTED,
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
            status=Match.Status.PROPOSED,
        )

        res = self.client.post(f"/api/transactions/{tx.id}/ignore/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res.status_code, 200)

        tx.refresh_from_db()
        match.refresh_from_db()
        self.assertEqual(tx.status, BankTransaction.Status.IGNORED)
        self.assertEqual(match.status, Match.Status.REJECTED)
        self.assertEqual(self.line_item.amount_funded, Decimal("0.00"))

    def test_ignoring_reconciled_transaction_returns_409(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            description="WIRE RECONCILED",
            signed_amount=Decimal("25000.00"),
            transaction_type="credit",
            bank_reference="FT-RECONCILED",
            status=BankTransaction.Status.RECONCILED,
        )

        res = self.client.post(f"/api/transactions/{tx.id}/ignore/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res.status_code, 409)
        self.assertIn("reconciled", res.data["error"])

    def test_missing_organization_header_returns_400(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            signed_amount=Decimal("1000.00"),
            bank_reference="FT-NO-HDR",
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
        )

        res_app = self.client.post(f"/api/matches/{match.id}/approve/")
        self.assertEqual(res_app.status_code, 400)

        res_rej = self.client.post(f"/api/matches/{match.id}/reject/")
        self.assertEqual(res_rej.status_code, 400)

        res_ign = self.client.post(f"/api/transactions/{tx.id}/ignore/")
        self.assertEqual(res_ign.status_code, 400)

    def test_cross_organization_access_returns_404(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            signed_amount=Decimal("1000.00"),
            bank_reference="FT-CROSS-ORG",
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
        )

        # Northwind resource accessed under Bluebird header returns 404
        res_app = self.client.post(f"/api/matches/{match.id}/approve/", HTTP_X_ORGANIZATION="bluebird")
        self.assertEqual(res_app.status_code, 404)

        res_rej = self.client.post(f"/api/matches/{match.id}/reject/", HTTP_X_ORGANIZATION="bluebird")
        self.assertEqual(res_rej.status_code, 404)

        res_ign = self.client.post(f"/api/transactions/{tx.id}/ignore/", HTTP_X_ORGANIZATION="bluebird")
        self.assertEqual(res_ign.status_code, 404)

    def test_invalid_or_repeated_state_transitions_do_not_change_funding(self):
        tx = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            signed_amount=Decimal("5000.00"),
            bank_reference="FT-REPEAT-STATE",
            status=BankTransaction.Status.NEEDS_REVIEW,
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx,
            line_item=self.line_item,
            status=Match.Status.REJECTED,
        )

        # Reject an already rejected match returns 409
        res_rej = self.client.post(f"/api/matches/{match.id}/reject/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res_rej.status_code, 409)

        # Approve a rejected match returns 409
        res_app = self.client.post(f"/api/matches/{match.id}/approve/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res_app.status_code, 409)

        self.line_item.refresh_from_db()
        self.assertEqual(self.line_item.amount_funded, Decimal("0.00"))

    def test_debit_transactions_cannot_be_approved(self):
        tx_debit = BankTransaction.objects.create(
            organization=self.org_northwind,
            transaction_date=date(2026, 4, 3),
            signed_amount=Decimal("-25.00"),
            transaction_type="debit",
            bank_reference="FT-DEBIT-ACTION",
            status=BankTransaction.Status.IGNORED,
        )
        match = Match.objects.create(
            organization=self.org_northwind,
            bank_transaction=tx_debit,
            line_item=self.line_item,
            status=Match.Status.PROPOSED,
        )

        res = self.client.post(f"/api/matches/{match.id}/approve/", HTTP_X_ORGANIZATION="northwind")
        self.assertEqual(res.status_code, 409)
        self.assertIn("Debit transactions cannot be approved", res.data["error"])
        self.line_item.refresh_from_db()
        self.assertEqual(self.line_item.amount_funded, Decimal("0.00"))
