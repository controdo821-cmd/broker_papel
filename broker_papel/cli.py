"""Comando `broker` de la terminal: habla con el servicio que ya está corriendo."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from .comandos import AYUDA
from .config import cargar
from .vistas import sin_html


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("ayuda", "-h", "--help", "help"):
        print(AYUDA)
        print("Servicio:  broker logs  ·  broker reiniciar  ·  broker detener  ·  broker arrancar  ·  broker actualizar")
        return
    if args[0] == "reiniciar-cuenta":
        r = input(f"Esto BORRA posiciones, órdenes e historial de la cuenta '{args[1] if len(args) > 1 else ''}'. "
                  "Escribe SI para continuar: ")
        if r.strip().upper() != "SI":
            print("Cancelado.")
            return
    if args[0] in ("crear-usuario", "cambiar-contrasena"):
        import getpass
        pw = getpass.getpass("Contraseña nueva (mínimo 10 caracteres): ")
        if pw != getpass.getpass("Repítela: "):
            print("No coinciden.")
            return
        args += ["--pw", pw]
    cfg = cargar()
    tok_f = cfg.data_dir / "control.token"
    if not tok_f.exists():
        print("El broker no parece estar corriendo (no hay token). Arráncalo con: broker arrancar")
        sys.exit(1)
    req = urllib.request.Request(f"http://127.0.0.1:{cfg.puerto_control}/cmd",
                                 data=json.dumps({"args": args}).encode(),
                                 headers={"Content-Type": "application/json", "X-Token": tok_f.read_text().strip()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(sin_html(json.loads(r.read())["texto"]))
    except urllib.error.URLError:
        print("No pude hablar con el broker. ¿Está corriendo? Revisa con: broker logs")
        sys.exit(1)


if __name__ == "__main__":
    main()
