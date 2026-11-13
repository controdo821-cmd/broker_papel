"""Pruebas de la web (login, 2FA, bloqueo, operar) y de la API compatible con Binance (firma HMAC)."""
import hashlib
import hmac
import time
from urllib.parse import urlencode

import pyotp
import pytest

from broker_papel.api_binance import ApiBinance
from broker_papel.auth import Auth
from broker_papel.comandos import Comandos
from broker_papel.informes import Informes
from broker_papel.vistas import Vistas
from broker_papel.web import Web


def armar(motor):
    async def asegurar(s):
        return None
    motor.m.asegurar = asegurar
    motor.m.datos_marca = lambda s: {"p": motor.m.marcas.get(s), "i": 0, "r": 0.0001, "T": 0}
    motor.cfg.web_cookie_segura = False
    v = Vistas(motor, motor.m, motor.cfg)
    inf = Informes(motor, motor.m, motor.cfg, v)
    auth = Auth(motor.db, motor)
    cmd = Comandos(motor, motor.m, v, informes=inf, auth=auth)
    return v, inf, auth, cmd


async def test_web_login_2fa_bloqueo_y_operar(motor, aiohttp_client):
    v, inf, auth, cmd = armar(motor)
    auth.crear("rodo", "contrasena-larga", "a")
    sec, _ = auth.activar_2fa("rodo")
    cli = await aiohttp_client(Web(motor.cfg, motor, motor.m, v, inf, auth, cmd).app())
    H = {"X-BP": "1"}

    r = await cli.get("/", allow_redirects=False)
    assert r.status == 302
    r = await cli.post("/api/login", json={"usuario": "rodo", "contrasena": "contrasena-larga"}, headers=H)
    assert r.status == 401 and "Authenticator" in (await r.json())["error"]
    r = await cli.post("/api/login", json={"usuario": "rodo", "contrasena": "contrasena-larga",
                                           "codigo": pyotp.TOTP(sec).now()}, headers=H)
    assert r.status == 200
    assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
    r = await cli.get("/")
    assert r.status == 200 and "Content-Security-Policy" in r.headers

    yo = await (await cli.get("/api/yo")).json()
    assert [c["nombre"] for c in yo["cuentas"]] == ["a"]        # solo ve su cuenta
    r = await cli.get("/api/cuenta?c=b")
    assert r.status == 403

    # sin la cabecera anti-CSRF no se acepta
    r = await cli.post("/api/orden", json={"s": "BTC", "lado": "BUY", "tipo": "MARKET", "cantidad": 0.1})
    assert r.status == 400
    r = await cli.post("/api/orden", json={"s": "BTC", "lado": "BUY", "tipo": "MARKET", "monto": 100, "tp": 11000}, headers=H)
    j = await r.json()
    assert r.status == 200, j
    assert j["orden"]["estado"] == "FILLED"
    c = await (await cli.get("/api/cuenta")).json()
    assert len(c["posiciones"]) == 1 and len(c["ordenes"]) == 1
    r = await cli.post("/api/orden", json={"s": "BTC", "lado": "BUY", "tipo": "LIMIT", "cantidad": 0.0001, "precio": 9000}, headers=H)
    assert r.status == 400 and (await r.json())["code"] == -1111
    r = await cli.post("/api/cerrar", json={"s": "BTCUSDT", "pct": 100}, headers=H)
    assert r.status == 200
    for t in ("ordenes", "llenados", "transacciones", "cerradas"):
        assert (await cli.get(f"/api/historial?tipo={t}")).status == 200
    assert (await cli.get("/api/estadisticas?rango=todo")).status == 200
    assert (await cli.get("/api/calendario?anio=2026&mes=11")).status == 200
    r = await cli.get("/api/exportar?f=xlsx")
    assert r.status == 200 and (await r.read())[:2] == b"PK"
    await cli.post("/api/logout", json={}, headers=H)
    assert (await cli.get("/api/yo")).status == 401

    # bloqueo tras 5 intentos fallidos
    for _ in range(5):
        await cli.post("/api/login", json={"usuario": "rodo", "contrasena": "mala-mala-mala"}, headers=H)
    r = await cli.post("/api/login", json={"usuario": "rodo", "contrasena": "contrasena-larga",
                                           "codigo": pyotp.TOTP(sec).now()}, headers=H)
    assert r.status == 401 and "bloqueado" in (await r.json())["error"]


def firmar(secreto, params):
    q = urlencode(params)
    return q + "&signature=" + hmac.new(secreto.encode(), q.encode(), hashlib.sha256).hexdigest()


async def test_api_compatible_binance(motor, aiohttp_client):
    v, inf, auth, cmd = armar(motor)
    llave, secreto = auth.crear_llave("c")
    cli = await aiohttp_client(ApiBinance(motor.cfg, motor, motor.m, auth).app())
    H = {"X-MBX-APIKEY": llave, "Content-Type": "application/x-www-form-urlencoded"}
    ts = lambda: int(time.time() * 1000)

    assert (await cli.get("/fapi/v1/ping")).status == 200
    r = await cli.post("/fapi/v1/order", data=firmar("otro", {"symbol": "BTCUSDT", "timestamp": ts()}), headers=H)
    assert (await r.json())["code"] == -1022
    r = await cli.get("/fapi/v2/balance?" + firmar(secreto, {"timestamp": ts()}), headers={"X-MBX-APIKEY": "x"})
    assert (await r.json())["code"] == -2015

    r = await cli.post("/fapi/v1/leverage", data=firmar(secreto, {"symbol": "BTCUSDT", "leverage": 10, "timestamp": ts()}), headers=H)
    assert (await r.json())["leverage"] == 10
    r = await cli.post("/fapi/v1/order", data=firmar(secreto, {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                                                               "quantity": "0.050", "newClientOrderId": "mibot1", "timestamp": ts()}), headers=H)
    o = await r.json()
    assert o["status"] == "FILLED" and o["clientOrderId"] == "mibot1" and o["executedQty"] == "0.050"
    r = await cli.post("/fapi/v1/order", data=firmar(secreto, {"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
                                                               "stopPrice": "9500", "closePosition": "true",
                                                               "workingType": "MARK_PRICE", "timestamp": ts()}), headers=H)
    sl = await r.json()
    assert sl["status"] == "NEW" and sl["closePosition"] is True
    r = await cli.get("/fapi/v1/openOrders?" + firmar(secreto, {"symbol": "BTCUSDT", "timestamp": ts()}), headers=H)
    assert len(await r.json()) == 1
    r = await cli.get("/fapi/v2/positionRisk?" + firmar(secreto, {"timestamp": ts()}), headers=H)
    pr = await r.json()
    assert pr[0]["positionAmt"] == "0.050" and pr[0]["leverage"] == "10"
    r = await cli.get("/fapi/v2/account?" + firmar(secreto, {"timestamp": ts()}), headers=H)
    assert float((await r.json())["totalWalletBalance"]) < 10_000
    r = await cli.request("DELETE", "/fapi/v1/order?" + firmar(secreto, {"symbol": "BTCUSDT", "orderId": sl["orderId"], "timestamp": ts()}), headers=H)
    assert (await r.json())["status"] == "CANCELED"
    r = await cli.post("/fapi/v1/order", data=firmar(secreto, {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                                                               "quantity": "0.0001", "timestamp": ts()}), headers=H)
    assert (await r.json())["code"] == -1111
    r = await cli.get("/fapi/v1/income?" + firmar(secreto, {"timestamp": ts()}), headers=H)
    assert {x["incomeType"] for x in await r.json()} >= {"TRANSFER", "COMMISSION"}
    r = await cli.get("/fapi/v1/order?" + firmar(secreto, {"symbol": "BTCUSDT", "origClientOrderId": "mibot1", "timestamp": ts()}), headers=H)
    assert (await r.json())["orderId"] == o["orderId"]
    viejo = ts() - 60_000
    r = await cli.get("/fapi/v2/balance?" + firmar(secreto, {"timestamp": viejo}), headers=H)
    assert (await r.json())["code"] == -1021
