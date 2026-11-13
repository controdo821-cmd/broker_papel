"""Usuarios de la web: contraseñas con scrypt, 2FA (Google Authenticator), bloqueo y sesiones."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

ITER_N, ITER_R, ITER_P = 2 ** 14, 8, 1
MAX_FALLOS, BLOQUEO_S = 5, 15 * 60
SESION_INACTIVA_S, SESION_MAX_S = 2 * 3600, 24 * 3600


def hash_contrasena(pw: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=sal, n=ITER_N, r=ITER_R, p=ITER_P, dklen=32)
    return f"scrypt${sal.hex()}${h.hex()}"


def verificar_contrasena(pw: str, guardado: str) -> bool:
    try:
        _, sal, h = guardado.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(sal), n=ITER_N, r=ITER_R, p=ITER_P, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db, motor):
        self.db, self.motor = db, motor

    # ---------------- usuarios
    def crear(self, usuario: str, pw: str, cuenta=None) -> str:
        usuario = usuario.strip().lower()
        if not usuario.isalnum():
            raise ValueError("El usuario solo puede tener letras y números.")
        if len(pw) < 10:
            raise ValueError("La contraseña debe tener al menos 10 caracteres.")
        cid = self.motor.cuenta(cuenta).id if cuenta not in (None, "", "todas") else None
        if self.db.one("SELECT 1 FROM usuarios WHERE usuario=?", (usuario,)):
            raise ValueError("Ese usuario ya existe.")
        self.db.ex("INSERT INTO usuarios(usuario,hash,cuenta,creado) VALUES(?,?,?,?)",
                   (usuario, hash_contrasena(pw), cid, int(time.time())))
        return usuario

    def cambiar(self, usuario: str, pw: str):
        if len(pw) < 10:
            raise ValueError("La contraseña debe tener al menos 10 caracteres.")
        if not self.db.one("SELECT 1 FROM usuarios WHERE usuario=?", (usuario.lower(),)):
            raise ValueError("No existe ese usuario.")
        self.db.ex("UPDATE usuarios SET hash=?, fallos=0, bloqueado_hasta=0 WHERE usuario=?", (hash_contrasena(pw), usuario.lower()))
        self.db.ex("DELETE FROM sesiones WHERE usuario=?", (usuario.lower(),))

    def borrar(self, usuario: str):
        self.db.ex("DELETE FROM usuarios WHERE usuario=?", (usuario.lower(),))
        self.db.ex("DELETE FROM sesiones WHERE usuario=?", (usuario.lower(),))

    def activar_2fa(self, usuario: str) -> tuple[str, str]:
        import pyotp
        if not self.db.one("SELECT 1 FROM usuarios WHERE usuario=?", (usuario.lower(),)):
            raise ValueError("No existe ese usuario.")
        sec = pyotp.random_base32()
        self.db.ex("UPDATE usuarios SET totp=? WHERE usuario=?", (sec, usuario.lower()))
        uri = pyotp.TOTP(sec).provisioning_uri(name=usuario.lower(), issuer_name="Broker Papel")
        return sec, uri

    def quitar_2fa(self, usuario: str):
        self.db.ex("UPDATE usuarios SET totp=NULL WHERE usuario=?", (usuario.lower(),))

    def lista(self) -> list[dict]:
        return self.db.all("SELECT usuario, cuenta, totp IS NOT NULL AS dosfa, bloqueado_hasta FROM usuarios ORDER BY usuario")

    # ---------------- login y sesiones
    def login(self, usuario: str, pw: str, codigo: str = "", ip: str = "") -> tuple[str | None, str]:
        usuario = (usuario or "").strip().lower()
        u = self.db.one("SELECT * FROM usuarios WHERE usuario=?", (usuario,))
        ahora = int(time.time())
        if not u:
            verificar_contrasena(pw, hash_contrasena("x" * 12))   # mismo tiempo de respuesta
            return None, "Usuario o contraseña incorrectos."
        if u["bloqueado_hasta"] > ahora:
            return None, f"Usuario bloqueado por intentos fallidos. Intenta en {(u['bloqueado_hasta'] - ahora) // 60 + 1} min."
        ok = verificar_contrasena(pw, u["hash"])
        if ok and u["totp"]:
            import pyotp
            ok = bool(codigo) and pyotp.TOTP(u["totp"]).verify(codigo.strip(), valid_window=1)
            if not ok and verificar_contrasena(pw, u["hash"]):
                msg = "Falta el código de Google Authenticator o es incorrecto."
            else:
                msg = "Usuario o contraseña incorrectos."
        else:
            msg = "Usuario o contraseña incorrectos."
        if not ok:
            fallos = u["fallos"] + 1
            bloq = ahora + BLOQUEO_S if fallos >= MAX_FALLOS else 0
            self.db.ex("UPDATE usuarios SET fallos=?, bloqueado_hasta=? WHERE usuario=?",
                       (0 if bloq else fallos, bloq, usuario))
            return None, msg if not bloq else "Demasiados intentos. Usuario bloqueado 15 minutos."
        self.db.ex("UPDATE usuarios SET fallos=0, bloqueado_hasta=0 WHERE usuario=?", (usuario,))
        token = secrets.token_urlsafe(32)
        self.db.ex("INSERT INTO sesiones VALUES(?,?,?,?,?)", (_h(token), usuario, ahora, ahora, ip))
        return token, "ok"

    def sesion(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self.db.one("SELECT * FROM sesiones WHERE token=?", (_h(token),))
        ahora = int(time.time())
        if not s:
            return None
        if ahora - s["ultimo"] > SESION_INACTIVA_S or ahora - s["creada"] > SESION_MAX_S:
            self.db.ex("DELETE FROM sesiones WHERE token=?", (_h(token),))
            return None
        u = self.db.one("SELECT usuario, cuenta FROM usuarios WHERE usuario=?", (s["usuario"],))
        if not u:
            return None
        if ahora - s["ultimo"] > 30:
            self.db.ex("UPDATE sesiones SET ultimo=? WHERE token=?", (ahora, _h(token)))
        return u

    def logout(self, token: str | None):
        if token:
            self.db.ex("DELETE FROM sesiones WHERE token=?", (_h(token),))

    # ---------------- llaves de API falsas (fase API compatible)
    def crear_llave(self, cuenta) -> tuple[str, str]:
        c = self.motor.cuenta(cuenta)
        llave, secreto = secrets.token_hex(32), secrets.token_hex(32)
        self.db.ex("INSERT INTO llaves_api VALUES(?,?,?,?)", (llave, secreto, c.id, int(time.time())))
        return llave, secreto

    def llaves(self) -> list[dict]:
        return self.db.all("SELECT llave, cuenta, creada FROM llaves_api ORDER BY creada")

    def borrar_llave(self, prefijo: str) -> int:
        return self.db.ex("DELETE FROM llaves_api WHERE llave LIKE ?", (prefijo + "%",)).rowcount
