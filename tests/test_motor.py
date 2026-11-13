"""Pruebas del motor: llenados, comisiones, TP/SL, liquidación, funding, rechazos."""
import pytest

from broker_papel.modelos import BinanceError
from conftest import mover


def saldo(m, c="a"):
    return m.cuenta(c).saldo


def test_market_long_y_cierre_con_ganancia(motor):
    motor.m.libros["BTCUSDT"] = (9_999.9, 10_000.0)
    o = motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1)
    assert o.estado == "FILLED" and o.precio_prom == 10_000.0       # compra al ask
    assert saldo(motor) == pytest.approx(10_000 - 0.5)               # taker 0.05 % de 1,000
    motor.m.libros["BTCUSDT"] = (11_000.0, 11_000.1)
    motor.cerrar_posicion("a", "BTCUSDT")                            # vende al bid
    assert saldo(motor) == pytest.approx(10_000 - 0.5 + 100 - 0.55)
    assert not motor.posiciones_abiertas(motor.cuenta("a"))
    r = motor.db.one("SELECT * FROM posiciones_cerradas")
    assert r["pnl"] == pytest.approx(100) and r["lado"] == "LONG"


def test_deslizamiento_segun_tamano(motor):
    motor.cfg.deslizamiento_base = 0.0001
    motor.cfg.deslizamiento_por_50k = 0.0001
    motor.m.libros["BTCUSDT"] = (9_999.9, 10_000.0)
    p_chico = motor.precio_mercado("BTCUSDT", "BUY", 0.01)
    p_grande = motor.precio_mercado("BTCUSDT", "BUY", 50)       # 500k notional
    assert p_chico == pytest.approx(10_001.1, abs=0.11)
    assert p_grande > p_chico


def test_limite_se_llena_al_cruzar_y_paga_maker(motor):
    o = motor.colocar("a", "BTCUSDT", "BUY", "LIMIT", cantidad=0.1, precio=9_900)
    assert o.estado == "NEW"
    mover(motor, "BTCUSDT", 9_900)            # solo toca: no se llena (modo "cruza")
    assert o.id in motor.ordenes
    mover(motor, "BTCUSDT", 9_899.9)          # cruza
    assert o.id not in motor.ordenes
    assert saldo(motor) == pytest.approx(10_000 - 0.1 * 9_900 * 0.0002)
    p = motor.cuenta("a").posiciones[("BTCUSDT", "BOTH")]
    assert p.entrada == 9_900 and p.amt == pytest.approx(0.1)


def test_limite_modo_toca(motor):
    motor.cfg.llenado_limite = "toca"
    o = motor.colocar("a", "BTCUSDT", "SELL", "LIMIT", cantidad=0.1, precio=10_100)
    mover(motor, "BTCUSDT", 10_100)
    assert o.id not in motor.ordenes


def test_post_only_rechazada_si_cruza(motor):
    motor.m.libros["BTCUSDT"] = (9_999.9, 10_000.0)
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "BTCUSDT", "BUY", "LIMIT", cantidad=0.1, precio=10_000.0, tif="GTX")
    assert e.value.code == -5022


def test_ioc_y_fok(motor):
    motor.m.libros["BTCUSDT"] = (9_999.9, 10_000.0)
    o = motor.colocar("a", "BTCUSDT", "BUY", "LIMIT", cantidad=0.1, precio=9_000, tif="IOC")
    assert o.estado == "EXPIRED"
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "BTCUSDT", "BUY", "LIMIT", cantidad=0.1, precio=9_000, tif="FOK")
    assert e.value.code == -5021
    o = motor.colocar("a", "BTCUSDT", "BUY", "LIMIT", cantidad=0.1, precio=10_050, tif="IOC")
    assert o.estado == "FILLED" and o.precio_prom == 10_000.0     # mejor precio disponible


def test_stop_market_por_ultimo_y_por_marca(motor):
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1)
    sl = motor.colocar("a", "BTCUSDT", "SELL", "STOP_MARKET", cantidad=0.1, precio_stop=9_500,
                       solo_reducir=True, disparo="MARK_PRICE")
    mover(motor, "BTCUSDT", 9_400, marca=False)       # el último cae, pero la marca no
    assert sl.id in motor.ordenes
    mover(motor, "BTCUSDT", 9_400, ultimo=False)
    assert sl.id not in motor.ordenes and sl.estado == "FILLED"
    assert not motor.posiciones_abiertas(motor.cuenta("a"))


def test_stop_inmediato_rechazado(motor):
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "BTCUSDT", "SELL", "STOP_MARKET", cantidad=0.1, precio_stop=10_500)
    assert e.value.code == -2021


def test_stop_limit(motor):
    o = motor.colocar("a", "BTCUSDT", "BUY", "STOP", cantidad=0.1, precio=10_210, precio_stop=10_200)
    mover(motor, "BTCUSDT", 10_150)
    assert not o.disparada
    mover(motor, "BTCUSDT", 10_200)   # dispara; el límite 10,210 >= precio actual -> se llena
    assert o.estado == "FILLED"
    assert o.precio_prom <= 10_210


def test_take_profit_limit_queda_puesta(motor):
    motor.colocar("a", "BTCUSDT", "SELL", "MARKET", cantidad=0.1)   # short
    tp = motor.colocar("a", "BTCUSDT", "BUY", "TAKE_PROFIT", cantidad=0.1, precio=9_500, precio_stop=9_600,
                       solo_reducir=True)
    mover(motor, "BTCUSDT", 9_600)
    assert tp.disparada and tp.estado == "NEW"      # límite 9,500 aún no se cruza
    mover(motor, "BTCUSDT", 9_499)
    assert tp.estado == "FILLED"
    assert not motor.posiciones_abiertas(motor.cuenta("a"))


def test_trailing_stop(motor):
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1)
    t = motor.colocar("a", "BTCUSDT", "SELL", "TRAILING_STOP_MARKET", cantidad=0.1, retroceso=1.0,
                      activacion=10_500, solo_reducir=True)
    mover(motor, "BTCUSDT", 10_400)
    assert not t.activado
    mover(motor, "BTCUSDT", 10_600)
    assert t.activado
    mover(motor, "BTCUSDT", 11_000)
    mover(motor, "BTCUSDT", 10_900.1)     # retroceso < 1 %
    assert t.estado == "NEW"
    mover(motor, "BTCUSDT", 10_890)       # 1 % debajo de 11,000
    assert t.estado == "FILLED"


def test_tpsl_adjuntos_al_abrir(motor):
    o = motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1, tp=11_000, sl=9_000)
    abiertas = motor.ordenes_abiertas("a")
    assert {x.tipo for x in abiertas} == {"TAKE_PROFIT_MARKET", "STOP_MARKET"}
    mover(motor, "BTCUSDT", 11_000)
    assert not motor.posiciones_abiertas(motor.cuenta("a"))
    assert motor.ordenes_abiertas("a") == []          # el SL se cancela al cerrar la posición
    assert o.estado == "FILLED"


def test_tpsl_de_posicion(motor):
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.2)
    motor.poner_tpsl("a", "BTCUSDT", tp=12_000, sl=9_000)
    motor.poner_tpsl("a", "BTCUSDT", sl=9_500)          # mover el SL
    sls = [o for o in motor.ordenes_abiertas("a") if o.tipo == "STOP_MARKET"]
    assert len(sls) == 1 and sls[0].precio_stop == 9_500
    mover(motor, "BTCUSDT", 9_500)
    assert not motor.posiciones_abiertas(motor.cuenta("a"))


def test_rechazos_como_binance(motor):
    casos = [
        (dict(cantidad=0.0001), -1111),             # demasiados decimales
        (dict(cantidad=0.005, precio=10_000.05), -1111),
        (dict(cantidad=0.005), -4164),              # notional < 100
        (dict(cantidad=0.1, precio=11_000), -4016),  # > 5 % arriba de la marca
        (dict(cantidad=50), -2027),                 # 500k > máximo a 20x? no: margen
    ]
    for kw, code in casos[:4]:
        with pytest.raises(BinanceError) as e:
            motor.colocar("a", "BTCUSDT", "BUY", "LIMIT", precio=kw.pop("precio", 9_000), **kw)
        assert e.value.code == code, kw
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=30)    # 300k a 20x = 15k margen > 10k
    assert e.value.code == -2019
    motor.cambiar_apalancamiento("a", "BTCUSDT", 125)
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=6)     # 60k > 50k máximo a 125x
    assert e.value.code == -2027
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "BTCUSDT", "SELL", "MARKET", cantidad=0.1, solo_reducir=True)
    assert e.value.code == -2022
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "XXXUSDT", "SELL", "MARKET", cantidad=1)
    assert e.value.code == -1121


def test_unidireccional_voltea(motor):
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1)
    mover(motor, "BTCUSDT", 10_100)
    motor.colocar("a", "BTCUSDT", "SELL", "MARKET", cantidad=0.3)
    p = motor.cuenta("a").posiciones[("BTCUSDT", "BOTH")]
    assert p.amt == pytest.approx(-0.2) and p.entrada == 10_100
    r = motor.db.one("SELECT pnl FROM posiciones_cerradas")
    assert r["pnl"] == pytest.approx(10)


def test_modo_cobertura(motor):
    motor.cambiar_modo_posicion("a", True)
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1, lado_pos="LONG")
    motor.colocar("a", "BTCUSDT", "SELL", "MARKET", cantidad=0.1, lado_pos="SHORT")
    c = motor.cuenta("a")
    assert len(motor.posiciones_abiertas(c)) == 2
    with pytest.raises(BinanceError) as e:
        motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1, lado_pos="BOTH")
    assert e.value.code == -4061
    motor.cerrar_posicion("a", "BTCUSDT", lado_pos="SHORT")
    assert [p.lado for p in motor.posiciones_abiertas(c)] == ["LONG"]


def test_liquidacion_aislada_pierde_todo_el_margen(motor):
    motor.cambiar_tipo_margen("a", "BTCUSDT", "aislado")
    motor.cambiar_apalancamiento("a", "BTCUSDT", 10)
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=1)        # margen 1,000
    c = motor.cuenta("a")
    d = motor.detalle_pos(c, motor.posiciones_abiertas(c)[0])
    assert d["liquidacion"] == pytest.approx(9036.14, abs=0.01)
    antes = c.saldo
    mover(motor, "BTCUSDT", 9_040)
    assert motor.posiciones_abiertas(c)
    mover(motor, "BTCUSDT", 9_036)
    assert not motor.posiciones_abiertas(c)
    assert c.saldo == pytest.approx(antes - 1_000)                   # se perdió el margen aislado
    assert any(e["tipo"] == "liquidacion" for e in motor.eventos)


def test_liquidacion_cruzada(motor):
    motor.cambiar_apalancamiento("a", "BTCUSDT", 20)
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=15)       # 150k notional, margen 7.5k
    c = motor.cuenta("a")
    lp = motor.detalle_pos(c, motor.posiciones_abiertas(c)[0])["liquidacion"]
    mover(motor, "BTCUSDT", round(lp + 5, 1))
    assert motor.posiciones_abiertas(c)
    mover(motor, "BTCUSDT", round(lp - 1, 1))
    assert not motor.posiciones_abiertas(c)
    assert c.saldo >= 0


def test_funding(motor):
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=1)
    motor.colocar("b", "BTCUSDT", "SELL", "MARKET", cantidad=1)
    sa, sb = saldo(motor, "a"), saldo(motor, "b")
    t = motor.reloj() + 1000
    motor.aplicar_funding("BTCUSDT", 0.0001, t, 10_000)
    motor.aplicar_funding("BTCUSDT", 0.0001, t, 10_000)    # no se cobra dos veces
    assert saldo(motor, "a") == pytest.approx(sa - 1)
    assert saldo(motor, "b") == pytest.approx(sb + 1)


def test_transferencias_no_son_ganancia(motor):
    motor.transferir("a", 500)
    motor.transferir("a", -200)
    assert saldo(motor) == pytest.approx(10_300)
    assert motor.pnl_desde(motor.cuenta("a"), 0) == 0
    with pytest.raises(BinanceError):
        motor.transferir("a", -1_000_000)


def test_persistencia(motor, tmp_path):
    from broker_papel.motor import Motor
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1)
    motor.colocar("a", "BTCUSDT", "BUY", "LIMIT", cantidad=0.1, precio=9_000)
    m2 = Motor(motor.db, motor.m, motor.cfg, reloj=motor.reloj)
    c = m2.cuenta("a")
    assert len(m2.posiciones_abiertas(c)) == 1
    assert len(m2.ordenes_abiertas("a")) == 1
    assert c.saldo == pytest.approx(motor.cuenta("a").saldo)


def test_reiniciar_cuenta(motor):
    motor.colocar("a", "BTCUSDT", "BUY", "MARKET", cantidad=0.1)
    motor.reiniciar_cuenta("a", 5_000)
    c = motor.cuenta("a")
    assert c.saldo == 5_000 and not motor.posiciones_abiertas(c)
