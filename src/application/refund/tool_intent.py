"""Trusted context for model-visible refund creation without model-supplied credentials."""

from dataclasses import dataclass
from typing import Protocol

from application.common.context import AuthContext
from application.refund.qualification import RefundQuote


@dataclass(frozen=True)
class RefundToolIntent:
    quote_id: str
    confirmation_id: str
    idempotency_key: str


class RefundToolIntentPort(Protocol):
    def register(self, auth: AuthContext, quote: RefundQuote, confirmation_id: str) -> None: ...

    def resolve(self, auth: AuthContext, order_id: str, reason_code: str) -> RefundToolIntent: ...
