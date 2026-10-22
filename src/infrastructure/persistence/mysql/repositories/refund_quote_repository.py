"""退款报价的不可变持久化；报价不是余额占用或退款申请。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from application.refund.qualification import RefundLineAllocation, RefundQuote


def _db_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("退款报价时间必须携带时区")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class MySqlRefundQuoteRepository:
    def __init__(self, engine, connection: Connection | None = None) -> None:
        self._engine = engine
        self._connection = connection

    def bind(self, connection: Connection) -> "MySqlRefundQuoteRepository":
        return MySqlRefundQuoteRepository(self._engine, connection)

    @contextmanager
    def _transaction(self):
        if self._connection is not None:
            yield self._connection
        else:
            with self._engine.begin() as connection:
                yield connection

    @contextmanager
    def _read(self):
        if self._connection is not None:
            yield self._connection
        else:
            with self._engine.connect() as connection:
                yield connection

    def save(self, quote: RefundQuote) -> None:
        if (
            quote.amount_minor <= 0
            or quote.amount_minor != quote.goods_minor + quote.shipping_minor
        ):
            raise ValueError("退款报价金额组成不一致")
        if quote.lines and (
            len({line.line_id for line in quote.lines}) != len(quote.lines)
            or any(
                not line.line_id
                or line.quantity <= 0
                or line.goods_minor < 0
                or line.shipping_minor < 0
                for line in quote.lines
            )
            or sum(line.goods_minor for line in quote.lines) != quote.goods_minor
            or sum(line.shipping_minor for line in quote.lines) != quote.shipping_minor
        ):
            raise ValueError("退款报价逐行组成不一致")
        with self._transaction() as conn:
            conn.execute(
                text(
                    "INSERT INTO refund_quote (tenant_id, quote_id, order_id,"
                    " actor_subject_id, customer_id, reason_code, eligibility,"
                    " amount_minor, goods_minor, shipping_minor, currency, policy_id,"
                    " policy_version, order_version, payment_version, expires_at, created_at)"
                    " VALUES (:tenant_id, :quote_id, :order_id, :subject_id,"
                    " :beneficiary_customer_id, :reason_code, :eligibility,"
                    " :amount_minor, :goods_minor, :shipping_minor, :currency, :policy_id,"
                    " :policy_version, :order_version, :payment_version, :expires_at, :created_at)"
                ),
                {
                    **quote.__dict__,
                    "expires_at": _db_time(quote.expires_at),
                    "created_at": _db_time(datetime.now(timezone.utc)),
                },
            )
            for line in quote.lines:
                conn.execute(
                    text(
                        "INSERT INTO refund_quote_line"
                        " (tenant_id,quote_id,line_id,quantity,goods_minor,shipping_minor)"
                        " VALUES (:tenant_id,:quote_id,:line_id,:quantity,"
                        ":goods_minor,:shipping_minor)"
                    ),
                    {"tenant_id": quote.tenant_id, "quote_id": quote.quote_id, **line.__dict__},
                )

    def load(self, tenant_id: str, quote_id: str) -> RefundQuote | None:
        with self._read() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT quote_id, tenant_id, order_id, actor_subject_id, customer_id,"
                        " reason_code, eligibility, amount_minor, goods_minor, shipping_minor,"
                        " currency, policy_id, policy_version, order_version, payment_version,"
                        " expires_at FROM refund_quote"
                        " WHERE tenant_id = :tenant_id AND quote_id = :quote_id"
                    ),
                    {"tenant_id": tenant_id, "quote_id": quote_id},
                )
                .mappings()
                .first()
            )
            lines = (
                []
                if row is None
                else conn.execute(
                    text(
                        "SELECT line_id,quantity,goods_minor,shipping_minor FROM refund_quote_line"
                        " WHERE tenant_id=:tenant_id AND quote_id=:quote_id ORDER BY line_id"
                    ),
                    {"tenant_id": tenant_id, "quote_id": quote_id},
                )
                .mappings()
                .all()
            )
        if row is None:
            return None
        return RefundQuote(
            quote_id=row["quote_id"],
            tenant_id=row["tenant_id"],
            order_id=row["order_id"],
            subject_id=row["actor_subject_id"],
            beneficiary_customer_id=row["customer_id"],
            reason_code=row["reason_code"],
            eligibility=row["eligibility"],
            amount_minor=int(row["amount_minor"]),
            goods_minor=int(row["goods_minor"]),
            shipping_minor=int(row["shipping_minor"]),
            currency=row["currency"],
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            order_version=row["order_version"],
            payment_version=row["payment_version"],
            expires_at=row["expires_at"].replace(tzinfo=timezone.utc),
            lines=tuple(
                RefundLineAllocation(
                    line["line_id"],
                    int(line["quantity"]),
                    int(line["goods_minor"]),
                    int(line["shipping_minor"]),
                )
                for line in lines
            ),
        )
