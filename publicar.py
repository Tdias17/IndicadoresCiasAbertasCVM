"""
Publica a página no GitHub Pages (envia index.html, dados.js e cotacoes/ para o GitHub).

Uso, depois de rodar o atualizar_dados.py:
    python publicar.py

O site atualiza sozinho em 1 a 2 minutos depois do envio.
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent


def git(*args):
    return subprocess.run(["git", *args], cwd=PASTA, text=True, capture_output=True)


def main():
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("Nada mudou desde a última publicação.")
        return
    msg = f"Atualiza dados ({datetime.now():%d/%m/%Y %H:%M})"
    r = git("commit", "-m", msg)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit(1)
    print("Enviando para o GitHub...")
    r = git("push")
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit(1)
    url = git("remote", "get-url", "origin").stdout.strip()
    print(f"Publicado. O site atualiza em 1 a 2 minutos. ({url})")


if __name__ == "__main__":
    main()
