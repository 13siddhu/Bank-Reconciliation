from django.contrib import admin
from .models import (
    Organization,
    InvestorEntity,
    CapitalCall,
    LineItem,
    BankTransaction,
    Match,
)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'created_at')
    search_fields = ('name', 'slug')


@admin.register(InvestorEntity)
class InvestorEntityAdmin(admin.ModelAdmin):
    list_display = ('name', 'organization', 'created_at')
    list_filter = ('organization',)
    search_fields = ('name',)


@admin.register(CapitalCall)
class CapitalCallAdmin(admin.ModelAdmin):
    list_display = ('call_number', 'organization', 'call_date', 'due_date')
    list_filter = ('organization',)
    search_fields = ('call_number',)


@admin.register(LineItem)
class LineItemAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'organization',
        'capital_call',
        'investor_entity',
        'amount_called',
        'amount_funded',
        'status',
    )
    list_filter = ('organization', 'status')
    search_fields = ('expected_wire_reference',)


@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'bank_reference',
        'organization',
        'transaction_date',
        'signed_amount',
        'transaction_type',
        'status',
    )
    list_filter = ('organization', 'status', 'transaction_type')
    search_fields = ('bank_reference', 'description')


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'organization',
        'bank_transaction',
        'line_item',
        'status',
        'match_reason',
    )
    list_filter = ('organization', 'status')
