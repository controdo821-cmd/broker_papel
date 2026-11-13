"""Cálculos puros estilo Binance Futures USDⓈ-M (sin estado, fáciles de probar)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from typing import Iterable, Optional


@dataclass
class Bracket:
    """Escalón de la tabla de margen de mantenimiento (leverage bracket)."""
    floor: float      # notional desde
    cap: float        # notional hasta
    mmr: float        # tasa de margen de mantenimiento
    cum: float        # monto de mantenimiento (cum)
    max_lev: int      # apalancamiento máximo en este escalón


# Tabla genérica de respaldo si no se puede bajar la real (parecida a una alt mediana).
DEFAULT_BRACKETS = [
    Bracket(0, 5_000, 0.01, 0, 50),
    Bracket(5_000, 25_000, 0.025, 75, 20),
    Bracket(25_000, 100_000, 0.05, 700, 10),
    Bracket(100_000, 250_000, 0.10, 5_700, 5),
    Bracket(250_000, 1_000_000, 0.125, 11_950, 4),
    Bracket(1_000_000, 3_000_000, 0.25, 136_950, 2),
    Bracket(3_000_000, 1e18, 0.5, 886_950, 1),
]


def bracket_for(brackets: list[Bracket], notional: float) -> Bracket:
    notional = abs(notional)
    for b in brackets:
        if b.floor <= notional < b.cap:
            return b
    return brackets[-1] if notional >= brackets[-1].floor else brackets[0]


def maint_margin(brackets: list[Bracket], notional: float) -> float:
    """Margen de mantenimiento = notional * mmr - cum."""
    notional = abs(notional)
    b = bracket_for(brackets, notional)
    return max(0.0, notional * b.mmr - b.cum)


def max_leverage(brackets: list[Bracket]) -> int:
    return max(b.max_lev for b in brackets)


def max_notional_at_leverage(brackets: list[Bracket], leverage: int) -> float:
    """Posición máxima (notional) permitida con ese apalancamiento."""
    caps = [b.cap for b in brackets if b.max_lev >= leverage]
    return max(caps) if caps else 0.0


def unrealized_pnl(amt: float, entry: float, mark: float) -> float:
    """amt con signo: + long, - short."""
    return amt * (mark - entry)


def roe(amt: float, entry: float, mark: float, leverage: int) -> float:
    """ROE % = PnL no realizado / margen inicial a precio de entrada."""
    im = abs(amt) * entry / leverage
    return 0.0 if im == 0 else unrealized_pnl(amt, entry, mark) / im * 100


def fee(qty: float, price: float, rate: float) -> float:
    return abs(qty) * price * rate


def funding_payment(amt: float, mark: float, rate: float) -> float:
    """Lo que recibe la cuenta (+) o paga (-). Tasa positiva: long paga a short."""
    return -amt * mark * rate


@dataclass
class Leg:
    side: int        # +1 long, -1 short
    qty: float       # tamaño absoluto
    entry: float


def liquidation_price(wallet: float, other_mm: float, other_upnl: float,
                      legs: Iterable[Leg], brackets: list[Bracket],
                      mark: Optional[float] = None) -> Optional[float]:
    """Fórmula oficial de Binance:

    LP = (WB - TMM1 + UPNL1 + Σcum - Σ side*qty*entry) / (Σ qty*mmr - Σ side*qty)

    WB: saldo de la billetera (cruzado) o margen aislado (aislado).
    TMM1/UPNL1: margen de mantenimiento y PnL de las OTRAS posiciones (solo cruzado).
    El escalón (mmr, cum) depende del notional al precio de liquidación, así que se itera.
    Devuelve None si no hay precio de liquidación (p. ej. riesgo cero).
    """
    legs = [l for l in legs if l.qty > 0]
    if not legs:
        return None
    ref = mark if mark else legs[0].entry
    lp = ref
    for _ in range(20):
        num = wallet - other_mm + other_upnl
        den = 0.0
        for l in legs:
            b = bracket_for(brackets, l.qty * max(lp, 0.0))
            num += b.cum - l.side * l.qty * l.entry
            den += l.qty * b.mmr - l.side * l.qty
        if abs(den) < 1e-15:
            return None
        new = num / den
        if new <= 0:
            return None
        if abs(new - lp) <= 1e-9 * max(1.0, abs(new)):
            lp = new
            break
        lp = new
    return lp if lp > 0 else None


# ---------- redondeo con pasos (tick/step) ----------

def _d(x) -> Decimal:
    return Decimal(str(x))


def decimals_of(step: float) -> int:
    d = _d(step).normalize()
    return max(0, -d.as_tuple().exponent)


def round_step(value: float, step: float, mode: str = "down") -> float:
    v, s = _d(value), _d(step)
    if s == 0:
        return value
    rounding = {"down": ROUND_DOWN, "up": ROUND_UP, "near": ROUND_HALF_UP}[mode]
    n = (v / s).quantize(Decimal(1), rounding=rounding)
    return float(n * s)


def is_multiple(value: float, step: float) -> bool:
    if step == 0:
        return True
    return (_d(value) % _d(step)) == 0


def too_many_decimals(value: float, step: float) -> bool:
    v = _d(value).normalize()
    return max(0, -v.as_tuple().exponent) > decimals_of(step)


def fmt_num(x: float, step: float | None = None) -> str:
    if x is None:
        return "--"
    if step:
        return f"{x:.{decimals_of(step)}f}"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.2f}"
    if ax >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".") or "0"
