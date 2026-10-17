import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BankTransaction, LineItem, Match, Organization
from .services import run_matching_for_transactions


MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
REQUIRED_HEADERS = ['date', 'description', 'amount', 'type', 'reference']


def get_organization_from_request(request):
    """
    Resolves organization from the X-Organization header.
    Returns (organization, None) if successful.
    Returns (None, error_response) if missing (400) or unknown (404).
    """
    org_slug = request.headers.get('X-Organization') or request.META.get('HTTP_X_ORGANIZATION')
    if not org_slug:
        return None, Response(
            {"error": "X-Organization header is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        organization = Organization.objects.get(slug=org_slug.strip())
        return organization, None
    except Organization.DoesNotExist:
        return None, Response(
            {"error": f"Organization '{org_slug}' not found."},
            status=status.HTTP_404_NOT_FOUND,
        )


class TransactionCSVUploadView(APIView):
    """
    POST /api/transactions/upload/
    Uploads a bank statement CSV and processes rows.
    - Missing required headers reject the entire file (HTTP 400).
    - Invalid data rows are skipped and reported with their CSV line number.
    - Duplicate rows within file and against database are skipped and reported.
    - Valid rows are imported.
    - Debits are marked ignored; credits immediately run through matching suggestions.
    """
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        organization, err_response = get_organization_from_request(request)
        if err_response:
            return err_response

        # File validation
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response(
                {"error": "No file provided. Please upload a file with key 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not uploaded_file.name.lower().endswith('.csv'):
            return Response(
                {"error": "Invalid file type. Only CSV files (.csv) are accepted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if uploaded_file.size > MAX_FILE_SIZE:
            return Response(
                {"error": f"File size exceeds maximum allowed size of {MAX_FILE_SIZE // (1024 * 1024)}MB."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if uploaded_file.size == 0:
            return Response(
                {"error": "Uploaded file is empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Safe decoding (utf-8-sig handles UTF-8 BOM transparently)
        try:
            content_bytes = uploaded_file.read()
            decoded_content = content_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            return Response(
                {"error": "Invalid file encoding. File must be UTF-8 encoded."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not decoded_content.strip():
            return Response(
                {"error": "Uploaded file is empty or contains only whitespace."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Header validation
        stream = io.StringIO(decoded_content)
        reader = csv.reader(stream)

        try:
            header_row = next(reader)
        except StopIteration:
            return Response(
                {"error": "CSV file contains no rows."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not header_row or not any(h.strip() for h in header_row):
            return Response(
                {"error": "CSV file contains no header row."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        header_map = {col.strip().lower(): idx for idx, col in enumerate(header_row)}
        missing_headers = [h for h in REQUIRED_HEADERS if h not in header_map]

        if missing_headers:
            return Response(
                {
                    "status": "error",
                    "message": f"Missing required CSV header(s): {', '.join(missing_headers)}",
                    "errors": [
                        {
                            "line": 1,
                            "column": "header",
                            "message": f"Missing required header: {h}",
                        }
                        for h in missing_headers
                    ],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Row-level validation and duplicate checking
        errors = []
        records_to_create = []
        seen_references_in_file = set()
        duplicates_count = 0

        existing_db_refs = set(
            BankTransaction.objects.filter(
                organization=organization
            ).values_list('bank_reference', flat=True)
        )

        for row in reader:
            line_no = reader.line_num

            # Skip blank lines silently
            if not row or not any(cell.strip() for cell in row):
                continue

            if max(header_map.values()) >= len(row):
                errors.append({
                    "line": line_no,
                    "column": "row",
                    "message": f"Row has insufficient columns ({len(row)} found, expected {len(REQUIRED_HEADERS)}).",
                })
                continue

            raw_date = row[header_map['date']].strip()
            raw_description = row[header_map['description']].strip()
            raw_amount = row[header_map['amount']].strip()
            raw_type = row[header_map['type']].strip()
            raw_reference = row[header_map['reference']].strip()

            row_has_error = False

            # Reference validation & duplicate detection
            if not raw_reference:
                errors.append({
                    "line": line_no,
                    "column": "reference",
                    "message": "Transaction reference is required.",
                })
                row_has_error = True
            elif raw_reference in seen_references_in_file:
                duplicates_count += 1
                errors.append({
                    "line": line_no,
                    "column": "reference",
                    "message": f"Duplicate transaction reference '{raw_reference}' within uploaded file.",
                })
                row_has_error = True
            elif raw_reference in existing_db_refs:
                duplicates_count += 1
                errors.append({
                    "line": line_no,
                    "column": "reference",
                    "message": f"Transaction reference '{raw_reference}' already exists for organization '{organization.slug}'.",
                })
                row_has_error = True
            else:
                seen_references_in_file.add(raw_reference)

            # Date validation
            parsed_date = None
            if not raw_date:
                errors.append({
                    "line": line_no,
                    "column": "date",
                    "message": "Transaction date is required.",
                })
                row_has_error = True
            else:
                try:
                    parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                except ValueError:
                    errors.append({
                        "line": line_no,
                        "column": "date",
                        "message": f"Invalid date '{raw_date}'. Expected format YYYY-MM-DD.",
                    })
                    row_has_error = True

            # Transaction type validation
            tx_type = raw_type.lower()
            if not tx_type:
                errors.append({
                    "line": line_no,
                    "column": "type",
                    "message": "Transaction type is required.",
                })
                row_has_error = True
            elif tx_type not in ('credit', 'debit'):
                errors.append({
                    "line": line_no,
                    "column": "type",
                    "message": f"Invalid transaction type '{raw_type}'. Must be 'credit' or 'debit'.",
                })
                row_has_error = True

            # Monetary amount validation
            parsed_amount = None
            signed_amount = None
            if not raw_amount:
                errors.append({
                    "line": line_no,
                    "column": "amount",
                    "message": "Transaction amount is required.",
                })
                row_has_error = True
            else:
                clean_amount = raw_amount.replace(',', '').replace('$', '').strip()
                try:
                    parsed_amount = Decimal(clean_amount)
                    if not parsed_amount.is_finite():
                        raise InvalidOperation("Non-finite decimal")
                except (InvalidOperation, TypeError):
                    errors.append({
                        "line": line_no,
                        "column": "amount",
                        "message": f"Invalid monetary amount '{raw_amount}'.",
                    })
                    row_has_error = True
                else:
                    if parsed_amount == 0:
                        errors.append({
                            "line": line_no,
                            "column": "amount",
                            "message": "Transaction amount cannot be zero.",
                        })
                        row_has_error = True
                    elif tx_type == 'credit':
                        if parsed_amount <= 0:
                            errors.append({
                                "line": line_no,
                                "column": "amount",
                                "message": f"Credit transaction amount must be positive, got '{raw_amount}'.",
                            })
                            row_has_error = True
                        else:
                            signed_amount = parsed_amount
                    elif tx_type == 'debit':
                        if parsed_amount >= 0:
                            errors.append({
                                "line": line_no,
                                "column": "amount",
                                "message": f"Debit transaction amount must be negative, got '{raw_amount}'.",
                            })
                            row_has_error = True
                        else:
                            signed_amount = parsed_amount

            # Description validation
            if not raw_description:
                errors.append({
                    "line": line_no,
                    "column": "description",
                    "message": "Transaction description is required.",
                })
                row_has_error = True

            if not row_has_error:
                status_choice = (
                    BankTransaction.Status.IGNORED
                    if tx_type == 'debit'
                    else BankTransaction.Status.NEEDS_REVIEW
                )
                records_to_create.append({
                    "line_no": line_no,
                    "organization": organization,
                    "transaction_date": parsed_date,
                    "description": raw_description,
                    "signed_amount": signed_amount,
                    "transaction_type": tx_type,
                    "bank_reference": raw_reference,
                    "status": status_choice,
                    "import_metadata": {
                        "source": "csv_upload",
                        "csv_line": line_no,
                        "raw_amount": raw_amount,
                        "raw_type": raw_type,
                    },
                })

        # Database transaction: persist valid rows and run matching suggestions on credits
        created_transactions = []
        if records_to_create:
            try:
                with transaction.atomic():
                    created_transactions = BankTransaction.objects.bulk_create([
                        BankTransaction(
                            organization=rec["organization"],
                            transaction_date=rec["transaction_date"],
                            description=rec["description"],
                            signed_amount=rec["signed_amount"],
                            transaction_type=rec["transaction_type"],
                            bank_reference=rec["bank_reference"],
                            status=rec["status"],
                            import_metadata=rec["import_metadata"],
                        )
                        for rec in records_to_create
                    ])
                    credit_transactions = [
                        tx for tx in created_transactions
                        if tx.transaction_type == 'credit' and tx.status != BankTransaction.Status.IGNORED
                    ]
                    if credit_transactions:
                        run_matching_for_transactions(credit_transactions, organization)
            except IntegrityError as exc:
                return Response(
                    {
                        "status": "error",
                        "message": f"Database integrity error during transaction save: {exc}",
                        "errors": [
                            {
                                "line": 0,
                                "column": "database",
                                "message": "Concurrent duplicate or integrity error.",
                            }
                        ],
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        errors.sort(key=lambda x: (x.get("line", 0), x.get("column", "")))
        imported_count = len(created_transactions)

        return Response(
            {
                "status": "success" if imported_count > 0 else ("empty" if not errors else "partial"),
                "message": (
                    f"Successfully processed CSV: {imported_count} imported, "
                    f"{duplicates_count} duplicate(s) skipped, {len(errors)} error(s)."
                ),
                "imported_count": imported_count,
                "duplicates_count": duplicates_count,
                "errors": errors,
            },
            status=status.HTTP_201_CREATED if imported_count > 0 else status.HTTP_200_OK,
        )


class ApproveMatchView(APIView):
    """
    POST /api/matches/<int:match_id>/approve/
    Approves a proposed match in an atomic transaction:
    - Verifies match belongs to organization and is still 'proposed'
    - Locks Match, BankTransaction, and LineItem using select_for_update()
    - Re-checks transaction amount against line item remaining amount (returns 409 if exceeds)
    - Prevents double-funding (returns 409 if already reconciled or match already processed)
    - Adds amount to line item funded amount, updates status ('funded' or 'partially_funded')
    - Marks chosen match 'approved'
    - Rejects other proposed matches for the same transaction
    - Marks transaction 'reconciled'
    """
    def post(self, request, match_id, *args, **kwargs):
        organization, err_response = get_organization_from_request(request)
        if err_response:
            return err_response

        with transaction.atomic():
            try:
                match = (
                    Match.objects.select_for_update()
                    .get(id=match_id, organization=organization)
                )
            except Match.DoesNotExist:
                return Response(
                    {"error": f"Match with id {match_id} not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            bank_tx = (
                BankTransaction.objects.select_for_update()
                .get(id=match.bank_transaction_id, organization=organization)
            )
            line_item = (
                LineItem.objects.select_for_update()
                .get(id=match.line_item_id, organization=organization)
            )

            # State validations
            if match.status != Match.Status.PROPOSED:
                return Response(
                    {"error": f"Match #{match_id} is already '{match.status}'. Only proposed matches can be approved."},
                    status=status.HTTP_409_CONFLICT,
                )

            if bank_tx.status == BankTransaction.Status.RECONCILED:
                return Response(
                    {"error": f"Transaction #{bank_tx.id} is already reconciled."},
                    status=status.HTTP_409_CONFLICT,
                )

            if bank_tx.transaction_type == 'debit' or bank_tx.signed_amount <= 0:
                return Response(
                    {"error": "Debit transactions cannot be approved."},
                    status=status.HTTP_409_CONFLICT,
                )

            if bank_tx.status == BankTransaction.Status.IGNORED:
                return Response(
                    {"error": "Ignored transactions cannot be approved."},
                    status=status.HTTP_409_CONFLICT,
                )

            # Re-check remaining amount against transaction amount
            remaining = line_item.remaining_amount
            amount_to_fund = bank_tx.signed_amount

            if amount_to_fund > remaining:
                return Response(
                    {
                        "error": (
                            f"Transaction amount ({amount_to_fund}) exceeds remaining "
                            f"line item amount ({remaining})."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            # Add transaction's credit amount to amount_funded exactly once
            new_amount_funded = line_item.amount_funded + amount_to_fund
            line_item.amount_funded = new_amount_funded
            if new_amount_funded >= line_item.amount_called:
                line_item.status = LineItem.Status.FUNDED
            else:
                line_item.status = LineItem.Status.PARTIALLY_FUNDED
            line_item.save(update_fields=['amount_funded', 'status'])

            # Mark chosen match approved
            match.status = Match.Status.APPROVED
            match.save(update_fields=['status'])

            # Mark competing proposed matches for this transaction rejected
            bank_tx.matches.filter(status=Match.Status.PROPOSED).exclude(id=match.id).update(
                status=Match.Status.REJECTED
            )

            # Mark transaction reconciled
            bank_tx.status = BankTransaction.Status.RECONCILED
            bank_tx.save(update_fields=['status'])

        return Response(
            {
                "status": "success",
                "message": f"Match #{match_id} approved successfully.",
                "match_id": match.id,
                "match_status": match.status,
                "transaction_id": bank_tx.id,
                "transaction_status": bank_tx.status,
                "line_item_id": line_item.id,
                "line_item_status": line_item.status,
                "amount_funded": str(line_item.amount_funded),
                "remaining_amount": str(line_item.remaining_amount),
            },
            status=status.HTTP_200_OK,
        )


class RejectMatchView(APIView):
    """
    POST /api/matches/<int:match_id>/reject/
    Rejects a proposed match:
    - Only allows rejection of a match that is currently 'proposed' (returns 409 otherwise)
    - Marks match 'rejected'
    - If no other proposed matches remain for that transaction, updates transaction to 'needs_review'
    - If other proposed matches remain, preserves current transaction status
    - Never modifies amount_funded
    """
    def post(self, request, match_id, *args, **kwargs):
        organization, err_response = get_organization_from_request(request)
        if err_response:
            return err_response

        with transaction.atomic():
            try:
                match = (
                    Match.objects.select_for_update()
                    .get(id=match_id, organization=organization)
                )
            except Match.DoesNotExist:
                return Response(
                    {"error": f"Match with id {match_id} not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            bank_tx = (
                BankTransaction.objects.select_for_update()
                .get(id=match.bank_transaction_id, organization=organization)
            )

            if match.status != Match.Status.PROPOSED:
                return Response(
                    {"error": f"Match #{match_id} is already '{match.status}'. Only proposed matches can be rejected."},
                    status=status.HTTP_409_CONFLICT,
                )

            # Mark match rejected
            match.status = Match.Status.REJECTED
            match.save(update_fields=['status'])

            # Check if any proposed matches remain for this transaction
            remaining_proposed = bank_tx.matches.filter(status=Match.Status.PROPOSED).exists()
            if not remaining_proposed:
                if bank_tx.status != BankTransaction.Status.RECONCILED:
                    bank_tx.status = BankTransaction.Status.NEEDS_REVIEW
                    bank_tx.save(update_fields=['status'])

        return Response(
            {
                "status": "success",
                "message": f"Match #{match_id} rejected successfully.",
                "match_id": match.id,
                "match_status": match.status,
                "transaction_id": bank_tx.id,
                "transaction_status": bank_tx.status,
                "remaining_proposed_matches": remaining_proposed,
            },
            status=status.HTTP_200_OK,
        )


class IgnoreTransactionView(APIView):
    """
    POST /api/transactions/<int:transaction_id>/ignore/
    Marks a transaction ignored:
    - Allows ignoring credit transactions in 'suggested' or 'needs_review' status
    - On a reconciled transaction, returns HTTP 409
    - Marks transaction 'ignored'
    - Marks all outstanding proposed matches 'rejected'
    - Does not modify line item funding totals
    """
    def post(self, request, transaction_id, *args, **kwargs):
        organization, err_response = get_organization_from_request(request)
        if err_response:
            return err_response

        with transaction.atomic():
            try:
                bank_tx = (
                    BankTransaction.objects.select_for_update()
                    .get(id=transaction_id, organization=organization)
                )
            except BankTransaction.DoesNotExist:
                return Response(
                    {"error": f"Transaction with id {transaction_id} not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if bank_tx.status == BankTransaction.Status.RECONCILED:
                return Response(
                    {"error": "Cannot ignore a reconciled transaction."},
                    status=status.HTTP_409_CONFLICT,
                )

            if bank_tx.status == BankTransaction.Status.IGNORED:
                return Response(
                    {"error": f"Transaction #{transaction_id} is already ignored."},
                    status=status.HTTP_409_CONFLICT,
                )

            # Mark transaction ignored
            bank_tx.status = BankTransaction.Status.IGNORED
            bank_tx.save(update_fields=['status'])

            # Mark outstanding proposed matches rejected
            bank_tx.matches.filter(status=Match.Status.PROPOSED).update(
                status=Match.Status.REJECTED
            )

        return Response(
            {
                "status": "success",
                "message": f"Transaction #{transaction_id} marked as ignored.",
                "transaction_id": bank_tx.id,
                "transaction_status": bank_tx.status,
            },
            status=status.HTTP_200_OK,
        )


class OrganizationListView(APIView):
    """
    GET /api/organizations/
    Returns list of available organizations for the organization switcher.
    """
    def get(self, request, *args, **kwargs):
        orgs = Organization.objects.all().order_by('name')
        data = [{"id": o.id, "slug": o.slug, "name": o.name} for o in orgs]
        return Response(data, status=status.HTTP_200_OK)


class DashboardSummaryView(APIView):
    """
    GET /api/dashboard/summary/
    Returns summary statistics for the organization:
    - Transaction counts by status
    - Line item counts and financial totals
    """
    def get(self, request, *args, **kwargs):
        organization, err = get_organization_from_request(request)
        if err:
            return err

        txs = BankTransaction.objects.filter(organization=organization)
        total_tx = txs.count()
        suggested_count = txs.filter(status=BankTransaction.Status.SUGGESTED).count()
        needs_review_count = txs.filter(status=BankTransaction.Status.NEEDS_REVIEW).count()
        reconciled_count = txs.filter(status=BankTransaction.Status.RECONCILED).count()
        ignored_count = txs.filter(status=BankTransaction.Status.IGNORED).count()

        line_items = LineItem.objects.filter(organization=organization)
        total_line_items = line_items.count()
        total_called = sum((li.amount_called for li in line_items), Decimal('0.00'))
        total_funded = sum((li.amount_funded for li in line_items), Decimal('0.00'))
        total_remaining = sum((li.remaining_amount for li in line_items), Decimal('0.00'))

        return Response({
            "organization": {
                "slug": organization.slug,
                "name": organization.name,
            },
            "transactions": {
                "total": total_tx,
                "suggested": suggested_count,
                "needs_review": needs_review_count,
                "reconciled": reconciled_count,
                "ignored": ignored_count,
            },
            "line_items": {
                "total": total_line_items,
                "total_called": str(total_called),
                "total_funded": str(total_funded),
                "total_remaining": str(total_remaining),
            }
        }, status=status.HTTP_200_OK)


class TransactionListView(APIView):
    """
    GET /api/transactions/
    Returns list of transactions for the organization, newest first.
    Includes matches with details of associated line items.
    """
    def get(self, request, *args, **kwargs):
        organization, err = get_organization_from_request(request)
        if err:
            return err

        status_filter = request.query_params.get('status')
        queryset = (
            BankTransaction.objects.filter(organization=organization)
            .prefetch_related('matches__line_item__investor_entity', 'matches__line_item__capital_call')
            .order_by('-transaction_date', '-id')
        )
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        results = []
        for tx in queryset:
            matches_data = []
            for m in tx.matches.all():
                li = m.line_item
                matches_data.append({
                    "id": m.id,
                    "status": m.status,
                    "match_reason": m.match_reason,
                    "line_item": {
                        "id": li.id,
                        "capital_call": li.capital_call.call_number,
                        "investor_name": li.investor_entity.name,
                        "expected_wire_reference": li.expected_wire_reference,
                        "amount_called": str(li.amount_called),
                        "amount_funded": str(li.amount_funded),
                        "remaining_amount": str(li.remaining_amount),
                        "status": li.status,
                    }
                })

            results.append({
                "id": tx.id,
                "transaction_date": tx.transaction_date.isoformat(),
                "description": tx.description,
                "signed_amount": str(tx.signed_amount),
                "transaction_type": tx.transaction_type,
                "bank_reference": tx.bank_reference,
                "status": tx.status,
                "review_reason": tx.import_metadata.get("review_reason", ""),
                "matches": matches_data,
            })

        return Response(results, status=status.HTTP_200_OK)


class LineItemListView(APIView):
    """
    GET /api/line-items/
    Returns list of capital call line items for the organization.
    """
    def get(self, request, *args, **kwargs):
        organization, err = get_organization_from_request(request)
        if err:
            return err

        line_items = (
            LineItem.objects.filter(organization=organization)
            .select_related('investor_entity', 'capital_call')
            .order_by('capital_call__call_number', 'id')
        )

        results = [
            {
                "id": li.id,
                "capital_call": {
                    "call_number": li.capital_call.call_number,
                    "call_date": li.capital_call.call_date.isoformat(),
                    "due_date": li.capital_call.due_date.isoformat(),
                },
                "investor_name": li.investor_entity.name,
                "expected_wire_reference": li.expected_wire_reference,
                "amount_called": str(li.amount_called),
                "amount_funded": str(li.amount_funded),
                "remaining_amount": str(li.remaining_amount),
                "status": li.status,
            }
            for li in line_items
        ]

        return Response(results, status=status.HTTP_200_OK)
