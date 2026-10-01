"""
Gera a versão para publicar como Artifact do Claude (link acessível pelo celular)
em ./artifact/. Remove a estrutura <html>/<head>/<body> (o Artifact adiciona a
sua) e esconde o botão de Excel (downloads são bloqueados dentro do Artifact).
"""
import re
import shutil
from pathlib import Path

PASTA = Path(__file__).resolve().parent
DEST = PASTA / "artifact"
DEST.mkdir(exist_ok=True)

html = (PASTA / "index.html").read_text(encoding="utf-8")
for padrao in [r"<!doctype html>\s*", r"<html[^>]*>\s*", r"</?head>\s*", r"</?body>\s*",
               r"</html>\s*", r"<meta[^>]*>\s*"]:
    html = re.sub(padrao, "", html, flags=re.I)
html = html.replace("</style>", "  #exportar { display: none; }\n</style>", 1)
(DEST / "index.html").write_text(html, encoding="utf-8")
shutil.copy(PASTA / "dados.js", DEST / "dados.js")
shutil.rmtree(DEST / "cotacoes", ignore_errors=True)
shutil.copytree(PASTA / "cotacoes", DEST / "cotacoes")
print("OK ->", DEST)
