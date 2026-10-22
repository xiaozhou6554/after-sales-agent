"""支付退款执行冲突：守恒、版本、预留与渠道状态约束。"""


class PaymentConflict(Exception):
    """执行被服务端拒绝或渠道状态冲突；HTTP 层统一转 STATE_CONFLICT。"""
