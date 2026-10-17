from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import models


class Organization(models.Model):
    slug = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class InvestorEntity(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='investor_entities',
    )
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'name'],
                name='unique_investor_name_per_org',
            )
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class CapitalCall(models.Model):
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='capital_calls',
    )
    call_number = models.CharField(max_length=50)
    call_date = models.DateField()
    due_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-call_date', 'call_number']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'call_number'],
                name='unique_call_number_per_org',
            ),
            models.CheckConstraint(
                condition=models.Q(due_date__gte=models.F('call_date')),
                name='check_call_due_date_gte_call_date',
            ),
        ]

    def __str__(self):
        return f"{self.call_number} ({self.organization.slug})"

    def clean(self):
        super().clean()
        if self.call_date and self.due_date and self.due_date < self.call_date:
            raise ValidationError({'due_date': 'Due date cannot be earlier than call date.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class LineItem(models.Model):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        PARTIALLY_FUNDED = 'partially_funded', 'Partially Funded'
        FUNDED = 'funded', 'Funded'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='line_items',
    )
    capital_call = models.ForeignKey(
        CapitalCall,
        on_delete=models.CASCADE,
        related_name='line_items',
    )
    investor_entity = models.ForeignKey(
        InvestorEntity,
        on_delete=models.CASCADE,
        related_name='line_items',
    )
    expected_wire_reference = models.CharField(max_length=255, blank=True, db_index=True)
    amount_called = models.DecimalField(max_digits=15, decimal_places=2)
    amount_funded = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_called__gt=0),
                name='check_line_item_amount_called_gt_zero',
            ),
            models.CheckConstraint(
                condition=models.Q(amount_funded__gte=0),
                name='check_line_item_amount_funded_gte_zero',
            ),
            models.CheckConstraint(
                condition=models.Q(amount_funded__lte=models.F('amount_called')),
                name='check_line_item_amount_funded_lte_called',
            ),
        ]

    def __str__(self):
        return f"{self.capital_call.call_number} - {self.investor_entity.name} (${self.amount_called})"

    @property
    def remaining_amount(self):
        if self.amount_called is None:
            return Decimal('0.00')
        return self.amount_called - (self.amount_funded or Decimal('0.00'))

    def clean(self):
        super().clean()
        if self.capital_call_id and self.capital_call.organization_id != self.organization_id:
            raise ValidationError({'capital_call': 'Capital call must belong to the same organization.'})
        if self.investor_entity_id and self.investor_entity.organization_id != self.organization_id:
            raise ValidationError({'investor_entity': 'Investor entity must belong to the same organization.'})
        if self.amount_called is not None and self.amount_called <= 0:
            raise ValidationError({'amount_called': 'Amount called must be greater than zero.'})
        if self.amount_funded is not None and self.amount_funded < 0:
            raise ValidationError({'amount_funded': 'Amount funded cannot be negative.'})
        if self.amount_called is not None and self.amount_funded is not None and self.amount_funded > self.amount_called:
            raise ValidationError({'amount_funded': 'Amount funded cannot exceed amount called.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class BankTransaction(models.Model):
    class Status(models.TextChoices):
        SUGGESTED = 'suggested', 'Suggested'
        NEEDS_REVIEW = 'needs_review', 'Needs Review'
        RECONCILED = 'reconciled', 'Reconciled'
        IGNORED = 'ignored', 'Ignored'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='bank_transactions',
    )
    transaction_date = models.DateField(db_index=True)
    description = models.TextField(blank=True)
    signed_amount = models.DecimalField(max_digits=15, decimal_places=2)
    transaction_type = models.CharField(max_length=50, blank=True)
    bank_reference = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEEDS_REVIEW,
        db_index=True,
    )
    import_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-transaction_date', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'bank_reference'],
                name='unique_org_bank_reference',
            )
        ]

    def __str__(self):
        return f"{self.bank_reference} ({self.signed_amount})"

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Match(models.Model):
    class Status(models.TextChoices):
        PROPOSED = 'proposed', 'Proposed'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='matches',
    )
    bank_transaction = models.ForeignKey(
        BankTransaction,
        on_delete=models.CASCADE,
        related_name='matches',
    )
    line_item = models.ForeignKey(
        LineItem,
        on_delete=models.CASCADE,
        related_name='matches',
    )
    match_reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PROPOSED,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(
                fields=['bank_transaction', 'line_item'],
                name='unique_bank_tx_line_item_match',
            ),
            models.UniqueConstraint(
                fields=['bank_transaction'],
                condition=models.Q(status='approved'),
                name='unique_approved_match_per_transaction',
            ),
        ]

    def __str__(self):
        return f"Match: {self.bank_transaction.bank_reference} -> LineItem #{self.line_item_id} ({self.status})"

    def clean(self):
        super().clean()
        if self.bank_transaction_id and self.bank_transaction.organization_id != self.organization_id:
            raise ValidationError({'bank_transaction': 'Bank transaction must belong to the same organization.'})
        if self.line_item_id and self.line_item.organization_id != self.organization_id:
            raise ValidationError({'line_item': 'Line item must belong to the same organization.'})
        if self.status == self.Status.APPROVED and self.bank_transaction_id:
            qs = Match.objects.filter(bank_transaction=self.bank_transaction, status=self.Status.APPROVED)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError({'status': 'An approved match already exists for this bank transaction.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
