from django.urls import path
from .views import (
    TransactionCSVUploadView,
    ApproveMatchView,
    RejectMatchView,
    IgnoreTransactionView,
    OrganizationListView,
    DashboardSummaryView,
    TransactionListView,
    LineItemListView,
)

urlpatterns = [
    path('organizations/', OrganizationListView.as_view(), name='organization-list'),
    path('dashboard/summary/', DashboardSummaryView.as_view(), name='dashboard-summary'),
    path('transactions/', TransactionListView.as_view(), name='transaction-list'),
    path('line-items/', LineItemListView.as_view(), name='line-item-list'),
    path('transactions/upload/', TransactionCSVUploadView.as_view(), name='transaction-csv-upload'),
    path('matches/<int:match_id>/approve/', ApproveMatchView.as_view(), name='match-approve'),
    path('matches/<int:match_id>/reject/', RejectMatchView.as_view(), name='match-reject'),
    path('transactions/<int:transaction_id>/ignore/', IgnoreTransactionView.as_view(), name='transaction-ignore'),
]
