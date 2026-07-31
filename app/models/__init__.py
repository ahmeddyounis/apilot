"""SQLAlchemy models. Importing this package registers all tables on Base.metadata."""

from app.models.api_key import ApiKey
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.embedding import InvoiceEmbedding
from app.models.fraud import VendorPaymentFingerprint
from app.models.idempotency import IdempotencyKey
from app.models.invoice import Invoice, InvoiceFile, InvoiceLineItem, InvoiceStatus
from app.models.llm_call import LlmCall
from app.models.matching import MatchingResult
from app.models.po import PoLineItem, PurchaseOrder
from app.models.receipt import Receipt, ReceiptLineItem
from app.models.review import FieldCorrection, ReviewAction
from app.models.vendor import Vendor

__all__ = [
    "ApiKey",
    "AuditLog",
    "Base",
    "FieldCorrection",
    "IdempotencyKey",
    "Invoice",
    "InvoiceEmbedding",
    "InvoiceFile",
    "InvoiceLineItem",
    "InvoiceStatus",
    "LlmCall",
    "MatchingResult",
    "PoLineItem",
    "PurchaseOrder",
    "Receipt",
    "ReceiptLineItem",
    "ReviewAction",
    "Vendor",
    "VendorPaymentFingerprint",
]
