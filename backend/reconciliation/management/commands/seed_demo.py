from datetime import date
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction

from reconciliation.models import CapitalCall, InvestorEntity, LineItem, Organization


DEMO_DATA = [
    {
        "org_slug": "northwind",
        "org_name": "Northwind Ventures",
        "call_number": "#1",
        "call_date": date(2026, 3, 25),
        "due_date": date(2026, 4, 10),
        "investor_name": "Haller Capital LLC",
        "reference": "CC-1001",
        "amount_called": Decimal("27500.00"),
    },
    {
        "org_slug": "northwind",
        "org_name": "Northwind Ventures",
        "call_number": "#1",
        "call_date": date(2026, 3, 25),
        "due_date": date(2026, 4, 10),
        "investor_name": "Rahul Shah Trust",
        "reference": "CC-1002",
        "amount_called": Decimal("25000.00"),
    },
    {
        "org_slug": "northwind",
        "org_name": "Northwind Ventures",
        "call_number": "#1",
        "call_date": date(2026, 3, 25),
        "due_date": date(2026, 4, 10),
        "investor_name": "Patel Family Trust",
        "reference": "CC-1003",
        "amount_called": Decimal("12500.00"),
    },
    {
        "org_slug": "northwind",
        "org_name": "Northwind Ventures",
        "call_number": "#1",
        "call_date": date(2026, 3, 25),
        "due_date": date(2026, 4, 10),
        "investor_name": "Meridian Partners LP",
        "reference": "CC-1004",
        "amount_called": Decimal("15000.00"),
    },
    {
        "org_slug": "northwind",
        "org_name": "Northwind Ventures",
        "call_number": "#1",
        "call_date": date(2026, 3, 25),
        "due_date": date(2026, 4, 10),
        "investor_name": "Oakridge Holdings LLC",
        "reference": "CC-1005",
        "amount_called": Decimal("15000.00"),
    },
    {
        "org_slug": "northwind",
        "org_name": "Northwind Ventures",
        "call_number": "#2",
        "call_date": date(2026, 4, 1),
        "due_date": date(2026, 4, 20),
        "investor_name": "Oakridge Holdings LLC",
        "reference": "CC-1006",
        "amount_called": Decimal("15000.00"),
    },
    {
        "org_slug": "bluebird",
        "org_name": "Bluebird Capital",
        "call_number": "#1",
        "call_date": date(2026, 4, 1),
        "due_date": date(2026, 4, 15),
        "investor_name": "Cedar Grove Fund LP",
        "reference": "CC-2001",
        "amount_called": Decimal("40000.00"),
    },
]


class Command(BaseCommand):
    help = "Seeds initial demo organizations, capital calls, investors, and line items."

    @transaction.atomic
    def handle(self, *args, **options):
        orgs = {}
        calls = {}
        investors = {}

        for item in DEMO_DATA:
            slug = item["org_slug"]
            if slug not in orgs:
                org, _ = Organization.objects.update_or_create(
                    slug=slug,
                    defaults={"name": item["org_name"]},
                )
                orgs[slug] = org

            org = orgs[slug]

            call_key = (slug, item["call_number"])
            if call_key not in calls:
                call, _ = CapitalCall.objects.update_or_create(
                    organization=org,
                    call_number=item["call_number"],
                    defaults={
                        "call_date": item["call_date"],
                        "due_date": item["due_date"],
                    },
                )
                calls[call_key] = call

            inv_key = (slug, item["investor_name"])
            if inv_key not in investors:
                inv, _ = InvestorEntity.objects.get_or_create(
                    organization=org,
                    name=item["investor_name"],
                )
                investors[inv_key] = inv

            LineItem.objects.get_or_create(
                organization=org,
                expected_wire_reference=item["reference"],
                defaults={
                    "capital_call": calls[call_key],
                    "investor_entity": investors[inv_key],
                    "amount_called": item["amount_called"],
                    "amount_funded": Decimal("0.00"),
                    "status": LineItem.Status.OPEN,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully seeded demo data: {Organization.objects.count()} organizations, "
                f"{CapitalCall.objects.count()} capital calls, "
                f"{InvestorEntity.objects.count()} investor entities, "
                f"{LineItem.objects.count()} line items."
            )
        )
