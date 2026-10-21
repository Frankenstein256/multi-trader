"""risk.py - position sizing (pure Python, no broker imports)."""
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal


class RiskError(Exception):
    """Raised when a safe position size cannot be computed."""


def _round_step(value: float, step: float, mode: str) -> float:
    d_step, d_val = Decimal(str(step)), Decimal(str(round(value, 10)))
    rounding = ROUND_FLOOR if mode == "down" else ROUND_HALF_UP
    return float((d_val / d_step).to_integral_value(rounding=rounding) * d_step)


def lots_for_risk(*, equity: float, risk_percent: float, entry: float, sl: float,
                  spec: dict, conv: float = 1.0, max_lot: float | None = None,
                  rounding: str = "nearest") -> float:
    """
    lots = (equity * risk% / 100) / (|entry - sl| * contract_size * conv)
    conv converts the symbol's profit currency into the account currency.
    Result is rounded to the broker volume step and clamped to broker/account limits.
    """
    if equity <= 0:
        raise RiskError("Account equity is not positive")
    if not sl or sl <= 0:
        raise RiskError("A stop-loss is required to size by risk")
    distance = abs(entry - sl)
    if distance <= 0:
        raise RiskError("Stop-loss equals entry price")
    contract = float(spec.get("contract_size") or 0)
    if contract <= 0 or conv <= 0:
        raise RiskError("Broker did not provide contract size / conversion rate")

    raw = (equity * risk_percent / 100.0) / (distance * contract * conv)
    step = float(spec.get("volume_step") or 0.01)
    vmin = float(spec.get("min_volume") or step)
    vmax = float(spec.get("max_volume") or 100.0)
    if max_lot:
        vmax = min(vmax, max_lot)

    lots = _round_step(raw, step, rounding)
    lots = max(lots, vmin)
    if lots > vmax:
        lots = _round_step(vmax, step, "down")
    return round(lots, 8)
