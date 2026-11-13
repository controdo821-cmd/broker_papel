"""Pruebas de las fórmulas contra ejemplos y propiedades de la documentación de Binance."""
import pytest

from broker_papel import calc
from broker_papel.calc import Leg
from conftest import BTC_BRACKETS


def test_pnl_y_roe():
    # Binance: PnL long = (marca - entrada) * cantidad. ROE = PnL / margen inicial.
    assert calc.unrealized_pnl(1, 10_000, 11_000) == 1_000
    assert calc.unrealized_pnl(-1, 10_000, 11_000) == -1_000
    assert calc.roe(1, 10_000, 11_000, 10) == pytest.approx(100)   # +10 % precio x10 = +100 %
    assert calc.roe(-2, 10_000, 9_500, 20) == pytest.approx(100)


def test_comisiones():
    # 1 BTC a 10,000: taker 0.05 % = 5 USDT, maker 0.02 % = 2 USDT
    assert calc.fee(1, 10_000, 0.0005) == pytest.approx(5)
    assert calc.fee(1, 10_000, 0.0002) == pytest.approx(2)


def test_funding_signo():
    # Tasa positiva: el long paga, el short cobra. Ejemplo de Binance: 0.01 % sobre 10,000 = 1 USDT
    assert calc.funding_payment(1, 10_000, 0.0001) == pytest.approx(-1)
    assert calc.funding_payment(-1, 10_000, 0.0001) == pytest.approx(1)
    assert calc.funding_payment(1, 10_000, -0.0001) == pytest.approx(1)


def test_margen_mantenimiento_brackets():
    # Binance: MM = notional * mmr - cum
    assert calc.maint_margin(BTC_BRACKETS, 10_000) == pytest.approx(40)            # 0.4 %
    assert calc.maint_margin(BTC_BRACKETS, 100_000) == pytest.approx(450)          # 0.5 % - 50
    assert calc.maint_margin(BTC_BRACKETS, 1_000_000) == pytest.approx(8_700)      # 1 % - 1300
    assert calc.max_leverage(BTC_BRACKETS) == 125
    assert calc.max_notional_at_leverage(BTC_BRACKETS, 125) == 50_000
    assert calc.max_notional_at_leverage(BTC_BRACKETS, 20) == 15_000_000


def test_liquidacion_aislado_long_ejemplo():
    # 1 BTC long a 10,000 con 10x aislado (margen 1,000), mmr 0.4 %:
    # LP = (1000 - 10000) / (0.004 - 1) = 9036.1446
    lp = calc.liquidation_price(1_000, 0, 0, [Leg(1, 1, 10_000)], BTC_BRACKETS)
    assert lp == pytest.approx(9036.1446, abs=1e-3)


def test_liquidacion_aislado_short_ejemplo():
    # 1 BTC short a 10,000 con 10x: LP = (1000 + 10000) / (0.004 + 1) = 10956.17
    lp = calc.liquidation_price(1_000, 0, 0, [Leg(-1, 1, 10_000)], BTC_BRACKETS)
    assert lp == pytest.approx(10956.1753, abs=1e-3)


@pytest.mark.parametrize("lado,qty,entrada,wb,otros_mm,otros_upnl", [
    (1, 3, 30_000, 20_000, 0, 0),
    (-1, 5, 30_000, 30_000, 100, -500),
    (1, 100, 30_000, 400_000, 2_000, 1_500),     # cae en otro bracket
    (-1, 40, 25_000, 150_000, 0, 0),
])
def test_liquidacion_cruzada_propiedad(lado, qty, entrada, wb, otros_mm, otros_upnl):
    """Definición de Binance: al precio de liquidación, balance de margen == margen de mantenimiento."""
    lp = calc.liquidation_price(wb, otros_mm, otros_upnl, [Leg(lado, qty, entrada)], BTC_BRACKETS)
    assert lp is not None
    balance = wb - otros_mm + otros_upnl + lado * qty * (lp - entrada)
    mm = calc.maint_margin(BTC_BRACKETS, qty * lp)
    assert balance == pytest.approx(mm, rel=1e-6, abs=1e-6)


def test_liquidacion_cobertura_dos_lados():
    # long 2 y short 1 en cruzado: al LP el balance == MM de ambas piernas
    legs = [Leg(1, 2, 10_000), Leg(-1, 1, 10_500)]
    lp = calc.liquidation_price(3_000, 0, 0, legs, BTC_BRACKETS)
    bal = 3_000 + 2 * (lp - 10_000) - 1 * (lp - 10_500)
    mm = sum(calc.maint_margin(BTC_BRACKETS, l.qty * lp) for l in legs)
    assert bal == pytest.approx(mm, rel=1e-6)


def test_sin_liquidacion_si_sobra_margen():
    # long 1x con saldo enorme: no hay precio de liquidación
    assert calc.liquidation_price(1_000_000, 0, 0, [Leg(1, 1, 10_000)], BTC_BRACKETS) is None


def test_redondeos():
    assert calc.round_step(0.0129, 0.001) == pytest.approx(0.012)
    assert calc.round_step(100.06, 0.1, "up") == pytest.approx(100.1)
    assert calc.is_multiple(0.003, 0.001)
    assert not calc.is_multiple(0.0035, 0.001)
    assert calc.too_many_decimals(0.0001, 0.001)
