import re
from decimal import Decimal
from django.db import models, transaction
from .models import BankTransaction, LineItem, Match, Organization


def normalize_reference(ref: str) -> str:
    """
    Uppercase, with everything except letters and digits removed.
    Example: 'cc-1003' -> 'CC1003'
    """
    if not ref:
        return ""
    return re.sub(r'[^A-Z0-9]', '', ref.upper())


def normalize_name(name: str) -> str:
    """
    Uppercase, with everything except letters and digits replaced by a space
    and repeated spaces collapsed.
    Then drop a trailing LLC, LP, LTD or INC. TRUST is kept.
    Example: 'Haller Capital LLC' -> 'HALLER CAPITAL'
             'Rahul Shah Trust' -> 'RAHUL SHAH TRUST'
    """
    if not name:
        return ""
    cleaned = re.sub(r'[^A-Z0-9]+', ' ', name.upper()).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    while True:
        new_cleaned = re.sub(r'\b(LLC|LP|LTD|INC)\b$', '', cleaned).strip()
        if new_cleaned == cleaned or not new_cleaned:
            break
        cleaned = new_cleaned
    return cleaned


def name_matches_description(investor_name: str, description: str) -> bool:
    """
    A name is found if it appears as whole words in the normalised description.
    """
    norm_name = normalize_name(investor_name)
    if not norm_name:
        return False
    norm_desc = re.sub(r'[^A-Z0-9]+', ' ', description.upper()).strip()
    norm_desc = re.sub(r'\s+', ' ', norm_desc)
    pattern = r'\b' + re.escape(norm_name) + r'\b'
    return bool(re.search(pattern, norm_desc))


def get_eligible_line_items(bank_transaction: BankTransaction, organization: Organization):
    """
    Eligible line items: same organization, remaining amount above 0.00,
    and call date on or before transaction date.
    """
    return LineItem.objects.filter(
        organization=organization,
        amount_funded__lt=models.F('amount_called'),
        capital_call__call_date__lte=bank_transaction.transaction_date,
    ).select_related('investor_entity', 'capital_call')


def match_credit_transaction(bank_transaction: BankTransaction, organization: Organization) -> None:
    """
    Applies matching rules to a credit transaction:
    1. Debits, ignored, or already reconciled transactions are skipped.
    2. Reference check:
       - 1 candidate and amount <= remaining: suggested match.
       - 1 candidate and amount > remaining: overpayment -> needs_review, no match.
       - >1 candidates: propose all candidates -> needs_review.
       - If any reference candidates found, skip step 3.
    3. Name and amount check (only when 0 reference candidates):
       - 1 candidate: suggested match.
       - >1 candidates: propose all candidates -> needs_review.
       - 0 candidates: needs_review with no matches.
    """
    if bank_transaction.transaction_type == 'debit' or bank_transaction.status in (
        BankTransaction.Status.IGNORED,
        BankTransaction.Status.RECONCILED,
    ):
        return

    # Preserve already approved matches
    if bank_transaction.matches.filter(status=Match.Status.APPROVED).exists():
        return

    eligible_items = list(get_eligible_line_items(bank_transaction, organization))

    # Reference check
    norm_desc_ref = normalize_reference(bank_transaction.description)
    norm_bank_ref = normalize_reference(bank_transaction.bank_reference)

    ref_candidates = []
    for item in eligible_items:
        norm_expected = normalize_reference(item.expected_wire_reference)
        if norm_expected and (norm_expected in norm_desc_ref or norm_expected in norm_bank_ref):
            ref_candidates.append(item)

    if ref_candidates:
        if len(ref_candidates) == 1:
            cand = ref_candidates[0]
            if bank_transaction.signed_amount <= cand.remaining_amount:
                bank_transaction.status = BankTransaction.Status.SUGGESTED
                if bank_transaction.signed_amount == cand.remaining_amount:
                    reason = f"Reference {cand.expected_wire_reference} found; amount equals remaining {cand.remaining_amount:,.2f}"
                else:
                    reason = (
                        f"Reference {cand.expected_wire_reference} found; "
                        f"partial payment of {bank_transaction.signed_amount:,.2f} against remaining {cand.remaining_amount:,.2f}"
                    )
                _set_single_proposed_match(bank_transaction, organization, cand, reason)
                bank_transaction.import_metadata["review_reason"] = ""
            else:
                # Overpayment
                bank_transaction.status = BankTransaction.Status.NEEDS_REVIEW
                bank_transaction.import_metadata["review_reason"] = (
                    f"Overpayment: amount {bank_transaction.signed_amount:,.2f} exceeds "
                    f"remaining {cand.remaining_amount:,.2f} for reference {cand.expected_wire_reference}"
                )
                _clear_proposed_matches(bank_transaction)
        else:
            # Multiple reference candidates
            bank_transaction.status = BankTransaction.Status.NEEDS_REVIEW
            bank_transaction.import_metadata["review_reason"] = (
                f"Multiple reference candidates found ({len(ref_candidates)})"
            )
            _set_multiple_proposed_matches(
                bank_transaction,
                organization,
                ref_candidates,
                reason_template=lambda c: f"Reference {c.expected_wire_reference} found (ambiguous: multiple reference candidates)",
            )

        bank_transaction.save(update_fields=['status', 'import_metadata'])
        return

    # Name and amount check (only when no reference candidates found)
    name_candidates = []
    for item in eligible_items:
        if bank_transaction.signed_amount == item.remaining_amount:
            if name_matches_description(item.investor_entity.name, bank_transaction.description):
                name_candidates.append(item)

    if len(name_candidates) == 1:
        cand = name_candidates[0]
        bank_transaction.status = BankTransaction.Status.SUGGESTED
        reason = (
            f"Investor name '{cand.investor_entity.name}' found; "
            f"amount equals remaining {cand.remaining_amount:,.2f}"
        )
        _set_single_proposed_match(bank_transaction, organization, cand, reason)
        bank_transaction.import_metadata["review_reason"] = ""
    elif len(name_candidates) > 1:
        bank_transaction.status = BankTransaction.Status.NEEDS_REVIEW
        bank_transaction.import_metadata["review_reason"] = (
            f"Multiple name and amount candidates found ({len(name_candidates)})"
        )
        _set_multiple_proposed_matches(
            bank_transaction,
            organization,
            name_candidates,
            reason_template=lambda c: (
                f"Investor name '{c.investor_entity.name}' found; "
                f"amount equals remaining {c.remaining_amount:,.2f} (ambiguous: multiple candidates)"
            ),
        )
    else:
        # None found
        bank_transaction.status = BankTransaction.Status.NEEDS_REVIEW
        bank_transaction.import_metadata["review_reason"] = (
            "No matching line item found by reference or name and amount"
        )
        _clear_proposed_matches(bank_transaction)

    bank_transaction.save(update_fields=['status', 'import_metadata'])


def _set_single_proposed_match(
    bank_transaction: BankTransaction,
    organization: Organization,
    line_item: LineItem,
    reason: str,
) -> None:
    match, created = Match.objects.get_or_create(
        bank_transaction=bank_transaction,
        line_item=line_item,
        defaults={
            'organization': organization,
            'match_reason': reason,
            'status': Match.Status.PROPOSED,
        },
    )
    if not created and match.status == Match.Status.PROPOSED:
        if match.match_reason != reason:
            match.match_reason = reason
            match.save(update_fields=['match_reason'])

    bank_transaction.matches.filter(status=Match.Status.PROPOSED).exclude(line_item=line_item).delete()


def _set_multiple_proposed_matches(
    bank_transaction: BankTransaction,
    organization: Organization,
    line_items: list[LineItem],
    reason_template,
) -> None:
    line_item_ids = [item.id for item in line_items]
    for item in line_items:
        reason = reason_template(item)
        match, created = Match.objects.get_or_create(
            bank_transaction=bank_transaction,
            line_item=item,
            defaults={
                'organization': organization,
                'match_reason': reason,
                'status': Match.Status.PROPOSED,
            },
        )
        if not created and match.status == Match.Status.PROPOSED:
            if match.match_reason != reason:
                match.match_reason = reason
                match.save(update_fields=['match_reason'])

    bank_transaction.matches.filter(status=Match.Status.PROPOSED).exclude(line_item_id__in=line_item_ids).delete()


def _clear_proposed_matches(bank_transaction: BankTransaction) -> None:
    bank_transaction.matches.filter(status=Match.Status.PROPOSED).delete()


def run_matching_for_transactions(transactions: list[BankTransaction], organization: Organization) -> None:
    for tx in transactions:
        match_credit_transaction(tx, organization)


def run_matching_for_organization(organization: Organization) -> int:
    pending_credits = BankTransaction.objects.filter(
        organization=organization,
        transaction_type='credit',
    ).exclude(
        status__in=[BankTransaction.Status.IGNORED, BankTransaction.Status.RECONCILED],
    )
    count = 0
    for tx in pending_credits:
        match_credit_transaction(tx, organization)
        count += 1
    return count
