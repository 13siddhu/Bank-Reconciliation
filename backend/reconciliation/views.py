import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BankTransaction, Organization


MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
REQUIRED_HEADERS = ['date', 'description', 'amount', 'type', 'reference']


class TransactionCSVUploadView(APIView):
    """
    POST /api/transactions/upload/
    Uploads a bank statement CSV and validates all rows atomically.
    Requires 'X-Organization' header with organization slug.
    """
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        # 1. Resolve organization from X-Organization header
        org_slug = request.headers.get('X-Organization') or request.META.get('HTTP_X_ORGANIZATION')
        if not org_slug:
            return Response(
                {"error": "X-Organization header is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            organization = Organization.objects.get(slug=org_slug.strip())
        except Organization.DoesNotExist:
            return Response(
                {"error": f"Organization '{org_slug}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # 2. File validation
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

        # 3. Safe decoding (utf-8-sig handles UTF-8 BOM transparently)
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

        # 4. Header validation
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

        # 5. Row-level validation
        errors = []
        records_to_create = []
        seen_references = {}  # reference -> first physical CSV line number

        for row in reader:
            line_no = reader.line_num

            # Skip blank lines
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

            # Reference
            if not raw_reference:
                errors.append({
                    "line": line_no,
                    "column": "reference",
                    "message": "Transaction reference is required.",
                })
                row_has_error = True
            elif raw_reference in seen_references:
                errors.append({
                    "line": line_no,
                    "column": "reference",
                    "message": f"Duplicate transaction reference '{raw_reference}' within uploaded file (first seen on line {seen_references[raw_reference]}).",
                })
                row_has_error = True
            else:
                seen_references[raw_reference] = line_no

            # Date
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

            # Transaction type
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

            # Monetary amount
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
                        if parsed_amount < 0:
                            errors.append({
                                "line": line_no,
                                "column": "amount",
                                "message": f"Credit transaction amount must be positive, got '{raw_amount}'.",
                            })
                            row_has_error = True
                        else:
                            signed_amount = parsed_amount
                    elif tx_type == 'debit':
                        signed_amount = -abs(parsed_amount)

            # Description
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

        # 6. Check duplicates already existing in database for this organization
        if seen_references:
            existing_db_refs = set(
                BankTransaction.objects.filter(
                    organization=organization,
                    bank_reference__in=seen_references.keys(),
                ).values_list('bank_reference', flat=True)
            )
            for ref in existing_db_refs:
                errors.append({
                    "line": seen_references[ref],
                    "column": "reference",
                    "message": f"Transaction reference '{ref}' already exists for organization '{organization.slug}'.",
                })

        if not records_to_create and not errors:
            return Response(
                {"error": "CSV file contains no valid data rows to import."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 7. All-or-nothing: if any errors occurred, reject entire upload
        if errors:
            errors.sort(key=lambda x: (x.get("line", 0), x.get("column", "")))
            return Response(
                {
                    "status": "error",
                    "message": "Validation failed for uploaded CSV.",
                    "errors": errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 8. All-or-nothing atomic write
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
        except IntegrityError:
            return Response(
                {
                    "status": "error",
                    "message": "Database constraint violation occurred during import. A transaction with this reference may have been concurrently imported.",
                    "errors": [
                        {
                            "line": 0,
                            "column": "reference",
                            "message": "Duplicate transaction reference detected by database constraint.",
                        }
                    ],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "status": "success",
                "message": f"Successfully imported {len(created_transactions)} bank transactions.",
                "imported_count": len(created_transactions),
                "ignored_count": sum(1 for r in records_to_create if r["status"] == BankTransaction.Status.IGNORED),
                "needs_review_count": sum(1 for r in records_to_create if r["status"] == BankTransaction.Status.NEEDS_REVIEW),
            },
            status=status.HTTP_201_CREATED,
        )
