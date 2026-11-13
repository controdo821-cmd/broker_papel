import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker_papel import calc  # noqa: E402
from broker_papel.config import Config, CuentaCfg  # noqa: E402
from broker_papel.db import DB  # noqa: E402
from broker_papel.modelos import SymbolInfo  # noqa: E402
from broker_papel.motor import Motor  # noqa: E402

BTC_BRACKETS = [
    calc.Bracket(0, 50_000, 0.004, 0, 125),
    calc.Bracket(50_000, 250_000, 0.005, 50, 100),
    calc.Bracket(250_000, 3_000_000, 0.01, 1_300, 50),
    calc.Bracket(3_000_000, 15_000_000, 0.025, 46_300, 20),
    calc.Bracket(15_000_000, 30_000_000, 0.05, 421_300, 10),
    calc.Bracket(30_000_000, 80_000_000, 0.1, 1_921_300, 5),
    calc.Bracket(80_000_000, 100_000_000, 0.125, 3_921_300, 4),
    calc.Bracket(100_000_000, 200_000_000, 0.15, 6_421_300, 3),
    calc.Bracket(200_000_000, 300_000_000, 0.25, 26_421_300, 2),
    calc.Bracket(300_000_000, 500_000_000, 0.5, 101_421_300, 1),
]


class FakeMercado:
    en_vivo = True

    def __init__(self):
        self.simbolos = {
            "BTCUSDT": SymbolInfo("BTCUSDT", tick=0.1, paso=0.001, min_cant=0.001, paso_mkt=0.001,
                                  min_cant_mkt=0.001, min_notional=100, mult_arriba=1.05, mult_abajo=0.95),
            "ETHUSDT": SymbolInfo("ETHUSDT", tick=0.01, paso=0.001, min_cant=0.001, paso_mkt=0.001,
                                  min_cant_mkt=0.001, min_notional=20),
        }
        self.marcas = {"BTCUSDT": 10_000.0, "ETHUSDT": 2_000.0}
        self.ultimos = dict(self.marcas)
        self.libros = {}

    def simbolo(self, s):
        return self.simbolos.get(s)

    def brackets(self, s):
        return BTC_BRACKETS if s == "BTCUSDT" else calc.DEFAULT_BRACKETS

    def marca(self, s):
        return self.marcas.get(s)

    def ultimo(self, s):
        return self.ultimos.get(s)

    def libro(self, s):
        return self.libros.get(s)


class Reloj:
    t = 1_700_000_000_000

    def __call__(self):
        return self.t


@pytest.fixture
def motor(tmp_path):
    cfg = Config(cuentas=[CuentaCfg("a"), CuentaCfg("b"), CuentaCfg("c")], saldo_inicial=10_000,
                 deslizamiento_base=0.0, deslizamiento_por_50k=0.0, data_dir=tmp_path)
    m = Motor(DB(tmp_path / "t.db"), FakeMercado(), cfg, reloj=Reloj())
    m.eventos = []
    m.oyentes.append(m.eventos.append)
    return m


def mover(motor, simbolo, precio, marca=True, ultimo=True):
    if marca:
        motor.m.marcas[simbolo] = precio
    if ultimo:
        motor.m.ultimos[simbolo] = precio
    if ultimo:
        motor.en_precio(simbolo, "last", precio)
    if marca:
        motor.en_precio(simbolo, "mark", precio)
