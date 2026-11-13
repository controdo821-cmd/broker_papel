# Broker Papel

Simulador de **Binance Futures USDⓈ-M** para operar con dinero ficticio. Corre 24/7 en tu VPS y lo manejas
desde **Telegram** o desde la **terminal**.

- **Todo es simulación.** Nunca manda órdenes reales y no usa llaves de API. Solo lee datos públicos de Binance.
- Gasta poco: unos 40-60 MB de RAM. Usa WebSockets y casi nada de REST (se frena solo si tu IP va cargada por tus bots).
- No toca nada de tus bots: vive en su carpeta, con su propio Python y su propio servicio.

---

## 1. Instalar (una sola vez)

Pega esto en el VPS:

```bash
cd /root
git clone https://github.com/controdo821-cmd/broker_papel.git
cd broker_papel
bash instalar.sh
```

Al final debe decir que las pruebas pasaron y "Listo".

## 2. Poner tu bot de Telegram

```bash
nano /root/broker_papel/config/secretos.toml
```

1. En `token` pega el token que te dio @BotFather (entre comillas).
2. Guarda con `Ctrl+O`, `Enter`, y sal con `Ctrl+X`.
3. Arranca: `broker arrancar`
4. En Telegram escribe `/id` a tu bot. Te responde tu número de chat.
5. Vuelve a abrir el archivo y ponlo en `chats_autorizados`, por ejemplo `chats_autorizados = [123456789]`.
6. `broker reiniciar` y en Telegram escribe `/start`. Ya ves el menú.

El bot solo responde a los chats de esa lista.

## 3. Nombres de cuentas y ajustes

```bash
nano /root/broker_papel/config/config.toml
broker reiniciar
```

Ahí cambias los nombres de las 3 cuentas, el saldo inicial, las comisiones, el deslizamiento, las alertas y la
hora del respaldo. Cámbialos **antes** del primer arranque; si cambias un nombre después, se crea otra cuenta.

## 4. Uso diario

**Telegram:** `/start` muestra el menú: Resumen, Posiciones (cerrar 25/50/100 %, poner o mover TP/SL), Órdenes
(cancelar), Abrir operación paso a paso, Precio, Hoy, Recargar, Retirar, Cambiar cuenta y Ajustes.
Todo lo que mueve dinero pide confirmación. También puedes escribir cualquier comando de terminal con `/c`,
por ejemplo `/c abrir 1 BTC long 100 x10 tp 120000 sl 95000`.

**Terminal:**

| Comando | Qué hace |
|---|---|
| `broker estado` | Resumen de las 3 cuentas y de la conexión |
| `broker resumen 1` | Saldo, margen y PnL de hoy, semana y mes |
| `broker posiciones 1` | Posiciones con PnL, ROE y liquidación |
| `broker ordenes 1` | Órdenes abiertas |
| `broker abrir 1 BTC long 100 x10` | Long a mercado con 100 USDT de margen a 10x |
| `broker abrir 1 ETH short q0.5 limite 4000 tp 3600 sl 4200` | Short límite de 0.5 ETH con TP y SL |
| `broker cerrar 1 BTC` / `broker cerrar 1 BTC 50` | Cerrar todo o el 50 % |
| `broker tpsl 1 BTC tp 120000 sl 95000` | TP/SL de toda la posición (`tp no` lo quita) |
| `broker orden 1 BTC venta trailing cant=0.01 retroceso=1 activacion=110000 reducir` | Cualquier tipo de orden |
| `broker cancelar 1 15` / `broker cancelar 1 todas` | Cancelar órdenes |
| `broker apalancamiento 1 BTC 20` | Cambiar apalancamiento |
| `broker margen 1 BTC aislado` | Cruzado o aislado |
| `broker margen-aislado 1 BTC +50` | Agregar (o `-50` quitar) margen aislado |
| `broker modo 1 cobertura` | Modo cobertura o unidireccional |
| `broker recargar 1 5000` / `broker retirar 1 2000` | Transferencias (no cuentan como ganancia) |
| `broker reiniciar-cuenta 1 10000` | Borra todo de esa cuenta y la deja con ese saldo |
| `broker historial 1` | Llenados y movimientos de hoy |
| `broker precio SOL` | Último, marca, índice, bid/ask, funding y cuenta regresiva |
| `broker ayuda` | Lista completa |

La cuenta puede ser su nombre o su número (1, 2, 3).

## 5. Página web (opcional, privada)

La web solo escucha dentro del VPS, así que nadie de afuera la ve. Para abrirla desde tu computadora usas un túnel SSH.

1. En el VPS, crea tu usuario (te pide la contraseña, mínimo 10 caracteres):

```bash
broker crear-usuario rodo
broker activar-2fa rodo        # opcional: te da la clave para Google Authenticator
```

Si quieres un usuario que solo vea una cuenta: `broker crear-usuario pedro 2`.

2. En tu computadora (Windows, Mac o Linux), abre una terminal y deja esto corriendo:

```bash
ssh -N -L 8080:127.0.0.1:8080 root@IP_DE_TU_VPS
```

3. Abre en el navegador: http://localhost:8080

Ahí tienes gráfica de velas con tus entradas, TP, SL, liquidación y órdenes marcadas, todos los tipos de orden,
posiciones, órdenes, historiales, estadísticas, calendario de PnL, recargas, retiros y exportar a Excel o CSV.
Tras 5 contraseñas malas el usuario se bloquea 15 minutos. Las sesiones vencen con 2 horas sin uso o a las 24 horas.

Otros: `broker usuarios`, `broker cambiar-contrasena rodo`, `broker quitar-2fa rodo`, `broker borrar-usuario rodo`.

Desde el teléfono también funciona con apps de SSH que hagan túneles (por ejemplo Termius), pero para el
teléfono lo más cómodo es Telegram.

## 6. Estadísticas, calendario y reporte diario

- Telegram: botones **Estadísticas**, **Calendario** (imagen como la de Binance, con ◀ ▶ para cambiar de mes),
  **Curva de saldo**, **Exportar** (te manda el Excel o el CSV) y **Gráfica** en cada posición.
- Reporte diario: llega a las 8:00 por defecto. Cámbialo con `/c reporte 09:30` o apágalo con `/c reporte no`.
- Terminal: `broker estadisticas 1 mes`, `broker calendario 1 2026-11`, `broker anio 1`, `broker exportar 1 excel`.

## 7. API compatible con Binance (para probar tus bots)

Tus bots pueden hablarle a este broker como si fuera Binance Futures, sin dinero real.

1. Actívala en `config/config.toml`, sección `[api]`, con `activa = true`, y luego `broker reiniciar`.
2. Crea una llave falsa: `broker crear-llave 3` (te da API key y secret de la cuenta 3).
3. En tu bot cambia la URL base de `https://fapi.binance.com` a `http://127.0.0.1:8090` y usa esa llave.

Rutas: `order` (crear, ver, cancelar), `openOrders`, `allOpenOrders`, `allOrders`, `positionRisk`, `balance`, `account`
(v2 y v3), `leverage`, `marginType`, `positionSide/dual`, `positionMargin`, `userTrades`, `income`, `leverageBracket`,
`commissionRate`, y las públicas `ping`, `time`, `exchangeInfo`, `premiumIndex`, `ticker/price`, `ticker/bookTicker`, `klines`.
La firma HMAC, el `timestamp`/`recvWindow` y los errores son como los de Binance.
Lo que no tiene: el WebSocket de usuario (listenKey responde, pero no manda eventos). Para precios en vivo tus bots siguen usando
los WebSockets públicos de Binance como siempre.

## 8. Arrancar, detener, ver qué pasa

```bash
broker arrancar      # arranca el servicio
broker detener       # lo detiene
broker reiniciar     # lo reinicia
broker servicio      # estado del servicio
broker logs          # ver el registro en vivo (sal con Ctrl+C)
broker memoria       # cuánta RAM usa
broker pruebas       # correr las pruebas de los cálculos
```

Arranca solo cuando se reinicia el VPS. Si se cae un rato, al volver revisa las velas de 1 minuto de ese hueco y
ejecuta con la hora correcta lo que se habría llenado (límites, stops, TP, liquidaciones y funding).

## 9. Actualizar

```bash
broker actualizar
```

Baja la versión nueva, instala lo que falte y reinicia. Tus datos y tu configuración no se tocan.

## 10. Respaldos

- Automático cada día a la hora de `config.toml` (por defecto 3:00), guarda 14 días en `/root/broker_papel/data/respaldos/`.
- Manual: `broker respaldo`
- Restaurar uno:

```bash
broker detener
cp /root/broker_papel/data/respaldos/broker-AAAAMMDD-HHMMSS.db /root/broker_papel/data/broker.db
rm -f /root/broker_papel/data/broker.db-wal /root/broker_papel/data/broker.db-shm
broker arrancar
```

## 11. Cómo simula (igual que Binance, con estas reglas)

- **Market:** compra al ask y vende al bid del momento, más el deslizamiento configurable según el tamaño. Paga taker.
- **Límite:** se llena a tu precio cuando el último precio lo **cruza** (o lo toca, si lo cambias en la configuración). Paga maker.
  Si al ponerla ya cruza el libro, se llena al momento como taker. Post only se rechaza si fuera taker. IOC y FOK como en Binance.
- **Stops, TP y trailing:** se disparan con precio de marca o con último precio, a elegir (los TP/SL de Telegram usan marca).
- **Rechazos:** paso de cantidad y de precio, mínimos y máximos, notional mínimo, límite de precio ±5 %, margen insuficiente y
  posición máxima según el apalancamiento (tabla real de Binance). El error muestra el código y el mensaje de Binance.
- **Liquidación:** cuando el balance de margen llega al margen de mantenimiento (tabla real por moneda). En aislado se pierde el
  margen de esa posición; en cruzado se cobra la comisión de liquidación configurable.
- **Funding:** con la tasa real de cada moneda y en su horario (8 h, 4 h o 1 h, lo que use esa moneda).
- **Deslistado:** si una moneda deja de operar, se cierran sus posiciones al último precio y te avisa.
- **Tabla de margen de mantenimiento:** se baja una vez al día de la página pública de Binance (no requiere llave). Si un día
  falla, usa la última copia guardada.
