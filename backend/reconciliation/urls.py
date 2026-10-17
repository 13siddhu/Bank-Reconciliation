from django.urls import path
from .views import (
    TransactionCSVUploadView,
    ApproveMatchView,
    RejectMatchView,
    IgnoreTransactionView,
)

urlpatterns = [
    path('transactions/upload/', TransactionCSVUploadView.as_view(), name='transaction-csv-upload'),
    path('matches/<int:match_id>/approve/', ApproveMatchView.as_view(), name='match-approve'),
    path('matches/<int:match_id>/reject/', RejectMatchView.as_view(), name='match-reject'),
    path('transactions/<int:transaction_id>/ignore/', IgnoreTransactionView.as_view(), name='transaction-ignore'),
]
