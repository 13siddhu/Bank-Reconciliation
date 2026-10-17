from django.urls import path
from .views import TransactionCSVUploadView

urlpatterns = [
    path('transactions/upload/', TransactionCSVUploadView.as_view(), name='transaction-csv-upload'),
]
