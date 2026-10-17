from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.core.management import call_command
from django.test import TestCase

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
