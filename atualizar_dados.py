"""
Baixa as DFPs (anuais) e ITRs (trimestrais) do portal de Dados Abertos da CVM e
gera o arquivo dados.js lido pela página index.html (anos, trimestres e LTM).

Também baixa do Yahoo Finance as cotações diárias (pasta cotacoes/, um arquivo por
ticker) e calcula a variação anual das ações (preço e retorno total).

Uso:
    python atualizar_dados.py                  # baixa só o que falta + os 2 anos mais recentes
    python atualizar_dados.py --tudo           # rebaixa todos os arquivos
    python atualizar_dados.py --sem-cotacoes   # pula o Yahoo Finance (reaproveita o último)

Usa apenas a biblioteca padrão do Python. Os ZIPs ficam em ./cache.
Sempre usa as demonstrações CONSOLIDADAS (arquivos *_con_*).
"""

import csv
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
PASTA = Path(__file__).resolve().parent
CACHE = PASTA / "cache"
SAIDA = PASTA / "dados.js"

INDICADORES = ["receita", "lucro_bruto", "lucro_operacional", "lucro_liquido", "lucro_controladores",
               "fco", "fci", "dividendos"]


def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return texto.lower().strip()


def listar_anos(tipo):
    """Lê o índice do portal e devolve os anos disponíveis (ex.: DFP, FCA)."""
    url = f"{BASE}/{tipo}/DADOS/"
    html = urllib.request.urlopen(url, timeout=60).read().decode("latin-1")
    padrao = rf"{tipo.lower()}_cia_aberta_(\d{{4}})\.zip"
    return sorted({int(a) for a in re.findall(padrao, html)})


def baixar(tipo, ano, forcar):
    CACHE.mkdir(exist_ok=True)
    nome = f"{tipo.lower()}_cia_aberta_{ano}.zip"
    destino = CACHE / nome
    if destino.exists() and not forcar:
        return destino
    print(f"  baixando {nome} ...", flush=True)
    url = f"{BASE}/{tipo}/DADOS/{nome}"
    dados = urllib.request.urlopen(url, timeout=300).read()
    destino.write_bytes(dados)
    return destino


def ler_csv(zf, nome):
    if nome not in zf.namelist():
        return []
    with zf.open(nome) as f:
        texto = io.TextIOWrapper(f, encoding="latin-1", newline="")
        return list(csv.DictReader(texto, delimiter=";"))


# ---------------------------------------------------------------------------
# Identificação das contas (pela descrição, porque bancos/seguradoras usam
# códigos diferentes das empresas não financeiras)
# ---------------------------------------------------------------------------

def extrair_dre(contas):
    """contas: lista [(cd_conta, ds_conta, valor)] de nível 1 (3.xx) e 2 (3.xx.xx)."""
    res = {}
    obs = {}
    conta_ll = None
    nivel2 = [c for c in contas if c[0].count(".") == 2]
    contas = [c for c in contas if c[0].count(".") == 1 and c[0] != "3.99"]
    if not contas:
        return res, obs
    contas.sort(key=lambda c: [int(p) for p in c[0].split(".")])

    res["receita"] = contas[0][2]  # 3.01 é sempre a receita (de vendas, intermediação, seguros...)

    for cd, ds, v in contas:
        d = normalizar(ds)
        if "resultado bruto" in d and "lucro_bruto" not in res:
            res["lucro_bruto"] = v
        if "antes do resultado financeiro" in d and "lucro_operacional" not in res:
            res["lucro_operacional"] = v
        if "consolidado" in d and ("lucro" in d or "resultado" in d):
            res["lucro_liquido"] = v
            conta_ll = cd

    if "lucro_operacional" not in res:
        # Bancos não têm EBIT: usa o resultado antes dos tributos
        for cd, ds, v in contas:
            if "antes dos tributos" in normalizar(ds):
                res["lucro_operacional"] = v
                obs["lucro_operacional"] = "resultado antes dos tributos"
                break

    if "lucro_liquido" not in res:
        res["lucro_liquido"] = contas[-1][2]
        conta_ll = contas[-1][0]

    # lucro atribuído aos sócios da controladora (base do P/L); sem a quebra, usa o consolidado
    res["lucro_controladores"] = res["lucro_liquido"]
    for cd, ds, v in nivel2:
        d = normalizar(ds)
        if cd.startswith(conta_ll + ".") and "controladora" in d and "nao controlador" not in d:
            # algumas empresas deixam a linha zerada; nesse caso fica o consolidado
            if v != 0 or res["lucro_liquido"] == 0:
                res["lucro_controladores"] = v
            break

    return res, obs


def extrair_dfc(contas):
    # 6.01 (operacional) e 6.02 (investimento) são fixos em todos os layouts,
    # mas a descrição varia (ex.: seguradoras chamam 6.01 de "Atividades Seguradora")
    res = {}
    for cd, ds, v in contas:
        if cd == "6.01":
            res["fco"] = v
        elif cd == "6.02":
            res["fci"] = v
    return res


def extrair_dva(contas):
    """Dividendos + JCP referentes ao exercício, da DVA (Remuneração de Capitais
    Próprios). O código do grupo varia (7.08.04, 7.09.04, 7.11.04...), por isso a
    busca é pela descrição."""
    grupo = None
    for cd, ds, v in contas:
        if cd.count(".") == 2 and "capitais proprios" in normalizar(ds):
            grupo = cd
            break
    if grupo is None:
        return {}
    total, achou = 0.0, False
    for cd, ds, v in contas:
        if not cd.startswith(grupo + ".") or cd.count(".") != 3:
            continue
        d = normalizar(ds)
        if "nao controlador" in d or "retid" in d:
            continue
        if "dividend" in d or "capital proprio" in d:
            total += v
            achou = True
    return {"dividendos": abs(total)} if achou else {}


# ---------------------------------------------------------------------------

def processar_composicao(caminho, doc, ano, acoes):
    """acoes[cnpj] = (data_ref, versao, ON, PN) já descontando ações em tesouraria."""
    with zipfile.ZipFile(caminho) as zf:
        linhas = ler_csv(zf, f"{doc}_cia_aberta_composicao_capital_{ano}.csv")
    for r in linhas:
        try:
            on = int(r["QT_ACAO_ORDIN_CAP_INTEGR"] or 0) - int(r["QT_ACAO_ORDIN_TESOURO"] or 0)
            pn = int(r["QT_ACAO_PREF_CAP_INTEGR"] or 0) - int(r["QT_ACAO_PREF_TESOURO"] or 0)
        except ValueError:
            continue
        chave = (r["DT_REFER"], int(r["VERSAO"] or 0))
        atual = acoes.get(r["CNPJ_CIA"])
        if on + pn > 0 and (atual is None or chave > atual[:2]):
            acoes[r["CNPJ_CIA"]] = (r["DT_REFER"], chave[1], on, pn)


def processar_pl(caminho, doc, ano, pl):
    """pl[cvm][data] = {"total": (v, prio), "min": (v, prio)}: patrimônio líquido consolidado
    e a parte dos não controladores, em R$ mil, em cada data de balanço."""
    with zipfile.ZipFile(caminho) as zf:
        linhas = ler_csv(zf, f"{doc}_cia_aberta_BPP_con_{ano}.csv")
    versao = {}
    for r in linhas:
        k = (r["CD_CVM"], r["DT_REFER"])
        versao[k] = max(versao.get(k, 0), int(r["VERSAO"] or 0))
    # a conta do PL varia (2.03 em geral; 2.07/2.08 em bancos): acha pela descrição
    conta_pl = {}
    for r in linhas:
        cd = r["CD_CONTA"]
        if cd.startswith("2.") and cd.count(".") == 1 and "patrimonio liquido" in normalizar(r["DS_CONTA"]):
            conta_pl[(r["CD_CVM"], r["DT_REFER"], r["DT_FIM_EXERC"])] = cd
    for r in linhas:
        cd = r["CD_CONTA"]
        base = conta_pl.get((r["CD_CVM"], r["DT_REFER"], r["DT_FIM_EXERC"]))
        if not base or not (cd == base or (cd.startswith(base + ".") and cd.count(".") == 2)):
            continue
        if int(r["VERSAO"] or 0) != versao[(r["CD_CVM"], r["DT_REFER"])]:
            continue
        if cd == base:
            campo = "total"
        elif "nao controlador" in normalizar(r["DS_CONTA"]):
            campo = "min"
        else:
            continue
        cvm = r["CD_CVM"].lstrip("0")
        valor = float(r["VL_CONTA"] or 0)
        if r["ESCALA_MOEDA"].upper() == "UNIDADE":
            valor /= 1000
        prio = (2 if normalizar(r["ORDEM_EXERC"]).startswith("ultimo") else 0) + (1 if doc == "dfp" else 0)
        reg = pl.setdefault(cvm, {}).setdefault(r["DT_FIM_EXERC"], {})
        if campo not in reg or prio > reg[campo][1]:
            reg[campo] = (valor, prio)


def pl_controladores(hist):
    """{data: PL dos controladores} = PL consolidado - não controladores."""
    out = {}
    for data, reg in hist.items():
        if "total" in reg:
            out[data] = reg["total"][0] - reg.get("min", (0, 0))[0]
    return out


def valor_mercado(emp, acoes, cotacoes, pl):
    """Valor de mercado (R$ mil) = ON x preço ON + PN x preço PN, no último fechamento."""
    comp = acoes.get(emp["_cnpj"])
    tk = next((t for t in emp["tickers"] if cotacoes.get(t, {}).get("_p")), None)
    if not comp or not tk:
        return None
    _, _, on, pn = comp
    raiz = tk[:4]
    preco = lambda *ts: next((cotacoes[t]["_p"] for t in ts if cotacoes.get(t, {}).get("_p")), None)
    raizes = {t[:4] for t in emp["tickers"]}
    if raiz + "3" not in cotacoes and raiz + "4" not in cotacoes:
        # ex.: ticker principal é de outra raiz; tenta as demais
        raiz = next((r for r in raizes if r + "3" in cotacoes or r + "4" in cotacoes), raiz)
    p_on = preco(raiz + "3")
    p_pn = preco(raiz + "4", raiz + "5", raiz + "6")
    aprox = False
    if on and p_on is None:
        p_on, aprox = p_pn, True
    if pn and p_pn is None:
        p_pn, aprox = p_on, True
    if (on and p_on is None) or (pn and p_pn is None):
        return None
    mcap = (on * (p_on or 0) + pn * (p_pn or 0)) / 1000  # R$ mil
    # algumas empresas (ex.: Itaú, Taesa, Eletrobras) informam a quantidade de ações em
    # milhares. Nesse caso o P/VPA calculado fica ~1000x menor: abaixo de 0,03 não existe na prática.
    pl_recente = pl[max(pl)] if pl else None
    if pl_recente and pl_recente > 0:
        errado = mcap / pl_recente < 0.03
    else:
        base = emp.get("ltm") and emp["ltm"]["v"] or (emp["anos"][max(emp["anos"])] if emp["anos"] else {})
        receita = abs(base.get("receita") or 0)
        errado = bool(receita) and mcap / receita < 0.003
    if errado:
        on, pn, mcap = on * 1000, pn * 1000, mcap * 1000
    return {"data": cotacoes[tk]["_d"], "acoes_on": on, "acoes_pn": pn,
            "preco_on": p_on, "preco_pn": p_pn, "mcap": round(mcap, 1), "aprox": aprox,
            "ref_acoes": comp[0]}


def processar_doc(caminho, doc, ano, periodos, nomes):
    """Lê DRE, DFC e DVA consolidados de um ZIP da DFP ("dfp") ou ITR ("itr") e
    preenche periodos[cvm][(dt_ini, dt_fim)] = {indicador: (valor em R$ mil, prioridade)}.

    A DFP traz o exercício (12 meses); a ITR traz o acumulado no ano (3, 6, 9 meses)
    e, na DRE, também o trimestre isolado. Trimestres e LTM são calculados depois."""
    with zipfile.ZipFile(caminho) as zf:
        arquivos = {
            "dre": ler_csv(zf, f"{doc}_cia_aberta_DRE_con_{ano}.csv"),
            "dfc": ler_csv(zf, f"{doc}_cia_aberta_DFC_MI_con_{ano}.csv")
                   + ler_csv(zf, f"{doc}_cia_aberta_DFC_MD_con_{ano}.csv"),
            "dva": ler_csv(zf, f"{doc}_cia_aberta_DVA_con_{ano}.csv"),
        }

    for tipo, linhas in arquivos.items():
        # só a versão mais recente de cada documento (empresa + data de referência)
        versao = {}
        for r in linhas:
            k = (r["CD_CVM"], r["DT_REFER"])
            versao[k] = max(versao.get(k, 0), int(r["VERSAO"] or 0))

        # DRE/DFC: só nível 1 (3.xx / 6.xx); DVA: até o nível 4 (ex.: 7.08.04.01)
        grupos = {}
        for r in linhas:
            n = r["CD_CONTA"].count(".")
            limite = {"dre": 2, "dva": 3}.get(tipo, 1)
            if n > limite or (tipo == "dfc" and n != 1):
                continue
            if int(r["VERSAO"] or 0) != versao[(r["CD_CVM"], r["DT_REFER"])]:
                continue
            if not r.get("DT_INI_EXERC"):
                continue
            cvm = r["CD_CVM"].lstrip("0")
            ultimo = normalizar(r["ORDEM_EXERC"]).startswith("ultimo")
            # prioridade: dado do próprio período (ÚLTIMO) > comparativo (PENÚLTIMO); DFP > ITR
            prio = (2 if ultimo else 0) + (1 if doc == "dfp" else 0)
            chave = (cvm, r["DT_INI_EXERC"], r["DT_FIM_EXERC"], prio)
            valor = float(r["VL_CONTA"] or 0)
            if r["ESCALA_MOEDA"].upper() == "UNIDADE":
                valor /= 1000
            grupos.setdefault(chave, {})[r["CD_CONTA"]] = (r["CD_CONTA"], r["DS_CONTA"], valor)
            nomes.setdefault(cvm, {})["nome"] = r["DENOM_CIA"]
            nomes[cvm]["cnpj"] = r["CNPJ_CIA"]

        for (cvm, ini, fim, prio), contas in grupos.items():
            contas = list(contas.values())
            if tipo == "dre":
                valores, obs = extrair_dre(contas)
            elif tipo == "dva":
                valores, obs = extrair_dva(contas), {}
            else:
                valores, obs = extrair_dfc(contas), {}
            registro = periodos.setdefault(cvm, {}).setdefault((ini, fim), {})
            for k, val in valores.items():
                atual = registro.get(k)
                if atual is None or prio > atual[1]:
                    registro[k] = (val, prio)
            for k, o in obs.items():
                registro.setdefault("_obs", {})[k] = o


def meses(ini, fim):
    a, b = date.fromisoformat(ini), date.fromisoformat(fim)
    return (b.year - a.year) * 12 + b.month - a.month + 1


def chave_tri(fim):
    d = date.fromisoformat(fim)
    return f"{d.year}T{(d.month - 1) // 3 + 1}"


def montar_series(por_periodo):
    """A partir dos períodos (ini, fim) de uma empresa, devolve:
    anos {ano: {ind: v}}, tri {AAAATn: {ind: v}}, ltm {ref, fim, v} ou None, obs."""
    anos, obs, anos_fim = {}, {}, {}
    ultimo_ano_fim = ""
    tri, tri_fim, origem = {}, {}, {}
    por_ini = {}
    for (ini, fim), reg in por_periodo.items():
        obs.update(reg.get("_obs", {}))
        vals = {k: v[0] for k, v in reg.items() if k != "_obs"}
        m = meses(ini, fim)
        por_ini.setdefault(ini, {})[m] = (fim, vals)
        if m >= 12:
            anos[int(fim[:4])] = {k: round(v, 1) for k, v in vals.items()}
            anos_fim[int(fim[:4])] = fim
            ultimo_ano_fim = max(ultimo_ano_fim, fim)

    # trimestres: período de 3 meses direto, ou diferença entre acumulados do mesmo exercício
    for ini, por_meses in por_ini.items():
        for m, (fim, vals) in por_meses.items():
            if m not in (3, 6, 9, 12):
                continue
            ch = chave_tri(fim)
            direto = m == 3
            if direto:
                q = dict(vals)
            elif m - 3 in por_ini[ini]:
                ant = por_ini[ini][m - 3][1]
                q = {k: vals[k] - ant[k] for k in vals if k in ant}
            else:
                continue
            reg = tri.setdefault(ch, {})
            for k, v in q.items():
                # valor direto (trimestre isolado) prevalece sobre o calculado
                if k not in reg or (direto and not origem.get((ch, k))):
                    reg[k] = round(v, 1)
                    origem[(ch, k)] = direto
            tri_fim[ch] = fim

    # LTM: soma dos últimos 4 trimestres consecutivos, se forem mais recentes que o último ano
    ltm = None
    if tri:
        def idx(ch):
            a, q = ch.split("T")
            return int(a) * 4 + int(q) - 1
        ult = max(tri, key=idx)
        ultimas = [c for c in tri if idx(ult) - 3 <= idx(c) <= idx(ult)]
        if len(ultimas) == 4 and tri_fim[ult] > ultimo_ano_fim:
            v = {}
            for k in INDICADORES:
                if all(k in tri[c] for c in ultimas):
                    v[k] = round(sum(tri[c][k] for c in ultimas), 1)
            if v:
                ltm = {"ref": ult, "fim": tri_fim[ult], "v": v}

    return anos, tri, ltm, obs, anos_fim


def processar_fca(caminho, ano, tickers_por_cnpj, info_por_cnpj):
    with zipfile.ZipFile(caminho) as zf:
        vm = ler_csv(zf, f"fca_cia_aberta_valor_mobiliario_{ano}.csv")
        geral = ler_csv(zf, f"fca_cia_aberta_geral_{ano}.csv")
    for r in vm:
        cod = (r.get("Codigo_Negociacao") or "").strip().upper()
        cnpj = r.get("CNPJ_Companhia", "")
        if cod and cnpj and re.fullmatch(r"[A-Z0-9]{4}\d{1,2}[A-Z]?", cod):
            tickers_por_cnpj.setdefault(cnpj, {})[cod] = ano
    for r in geral:
        cnpj = r.get("CNPJ_Companhia", "")
        if cnpj:
            info_por_cnpj[cnpj] = {
                "setor": r.get("Setor_Atividade", ""),
                "cvm": (r.get("Codigo_CVM") or "").lstrip("0"),
            }


# ---------------------------------------------------------------------------
# Cotações (Yahoo Finance)
# ---------------------------------------------------------------------------

# Cotações diárias desde 2009 (início das DFPs no portal da CVM)
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}.SA?interval=1d&period1=1230768000&period2={}&events=div"
ARQ_COTACOES = CACHE / "cotacoes.json"
PASTA_COT = PASTA / "cotacoes"


def sig(x):
    """Arredonda para 5 algarismos significativos (bom para preços de centavos a milhares)."""
    return float(f"{x:.5g}")


def baixar_ticker(ticker):
    """Baixa a série diária do ticker, grava cotacoes/<TICKER>.js e devolve a
    variação anual {ano: [var_preco_%, retorno_total_%]} (só anos completos)."""
    url = YAHOO.format(ticker, int(datetime.now().timestamp()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())["chart"]["result"][0]
        ts = r["timestamp"]
        close = r["indicators"]["quote"][0]["close"]      # ajustado só por desdobramentos
        adj = r["indicators"]["adjclose"][0]["adjclose"]  # + dividendos/JCP reinvestidos
        proventos = (r.get("events") or {}).get("dividends") or {}
    except Exception:
        return None

    datas, cs, adjs = [], [], []
    for t, c, a in zip(ts, close, adj):
        if c is None or a is None or c <= 0:
            continue
        # horário de São Paulo (UTC-3) para não cair no dia anterior
        datas.append(datetime.fromtimestamp(t - 3 * 3600, timezone.utc).date())
        cs.append(sig(c))
        adjs.append(sig(a))
    if len(datas) < 2:
        return None

    # formato compacto: data inicial + diferença em dias entre pregões
    d0 = datas[0]
    serie = {
        "t0": d0.isoformat(),
        "d": [(datas[i] - datas[i - 1]).days if i else 0 for i in range(len(datas))],
        "c": cs,
        "a": adjs,
    }
    PASTA_COT.mkdir(exist_ok=True)
    (PASTA_COT / f"{ticker}.js").write_text(
        "(window.COT=window.COT||{})[" + json.dumps(ticker) + "]=" +
        json.dumps(serie, separators=(",", ":")) + ";\n", encoding="utf-8")

    hoje = datetime.now()
    tri_atual = hoje.year * 4 + (hoje.month - 1) // 3
    fim_de_ano, fim_de_tri = {}, {}
    for d, c, a in zip(datas, cs, adjs):
        if d.year < hoje.year:
            fim_de_ano[d.year] = (c, a)  # o último pregão do período sobrescreve os anteriores
        t = d.year * 4 + (d.month - 1) // 3
        if t < tri_atual:
            fim_de_tri[t] = (c, a)
    out = {}
    for ano in sorted(fim_de_ano):
        if ano - 1 in fim_de_ano:
            c0, a0 = fim_de_ano[ano - 1]
            c1, a1 = fim_de_ano[ano]
            out[str(ano)] = [round((c1 / c0 - 1) * 100, 2), round((a1 / a0 - 1) * 100, 2)]
    for t in sorted(fim_de_tri):
        if t - 1 in fim_de_tri:
            c0, a0 = fim_de_tri[t - 1]
            c1, a1 = fim_de_tri[t]
            out[f"{t // 4}T{t % 4 + 1}"] = [round((c1 / c0 - 1) * 100, 2), round((a1 / a0 - 1) * 100, 2)]

    # último fechamento e proventos (dividendos + JCP) por ação nos últimos 12 meses
    ultimo_dia = datetime(datas[-1].year, datas[-1].month, datas[-1].day, tzinfo=timezone.utc).timestamp()
    out["_p"] = cs[-1]
    out["_d"] = datas[-1].isoformat()
    out["_div12"] = round(sum(ev.get("amount", 0) for ev in proventos.values()
                              if ultimo_dia - 365 * 86400 < ev.get("date", 0) <= ultimo_dia + 86400), 6)
    return out


def baixar_cotacoes(tickers):
    print(f"Cotações diárias (Yahoo Finance): {len(tickers)} tickers...", flush=True)
    res = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, v in zip(tickers, ex.map(baixar_ticker, tickers)):
            if v is not None:
                res[t] = v
    print(f"  {len(res)} tickers com cotação -> pasta cotacoes/")
    CACHE.mkdir(exist_ok=True)
    ARQ_COTACOES.write_text(json.dumps(res), encoding="utf-8")
    return res


def prioridade_ticker(t):
    # units (11) costumam ser as mais líquidas, depois PN (4), depois ON (3)
    sufixo = re.sub(r"^[A-Z0-9]{4}", "", t)
    return {"11": 0, "4": 1, "3": 2}.get(sufixo, 3)


def main():
    forcar_tudo = "--tudo" in sys.argv

    print("Consultando anos disponíveis na CVM...")
    anos_dfp = listar_anos("DFP")
    anos_itr = listar_anos("ITR")
    anos_fca = listar_anos("FCA")
    recentes = set(anos_dfp[-2:]) | set(anos_itr[-2:]) | set(anos_fca[-2:])

    periodos, nomes, acoes, pl = {}, {}, {}, {}
    for doc, anos_doc in (("DFP", anos_dfp), ("ITR", anos_itr)):
        print(f"{doc}: {anos_doc[0]}-{anos_doc[-1]}")
        for ano in anos_doc:
            caminho = baixar(doc, ano, forcar_tudo or ano in recentes)
            processar_doc(caminho, doc.lower(), ano, periodos, nomes)
            processar_composicao(caminho, doc.lower(), ano, acoes)
            processar_pl(caminho, doc.lower(), ano, pl)
            print(f"  {ano} processado", flush=True)

    tickers_por_cnpj, info_por_cnpj = {}, {}
    print(f"FCA (tickers): {anos_fca[0]}-{anos_fca[-1]}")
    for ano in anos_fca:
        caminho = baixar("FCA", ano, forcar_tudo or ano in recentes)
        processar_fca(caminho, ano, tickers_por_cnpj, info_por_cnpj)

    pl_ctrl = {cvm: pl_controladores(h) for cvm, h in pl.items()}

    # monta saída
    empresas, mapa_tickers = {}, {}
    for cvm, por_periodo in periodos.items():
        cnpj = nomes[cvm]["cnpj"]
        tks = tickers_por_cnpj.get(cnpj, {})
        anos, tri, ltm, obs_out, anos_fim = montar_series(por_periodo)
        # PL dos controladores no fim de cada exercício e de cada trimestre (base do ROE)
        plc = pl_ctrl.get(cvm, {})
        pl_out = {
            "anos": {str(a): round(plc[f], 1) for a, f in anos_fim.items() if f in plc},
            "tri": {chave_tri(d): round(v, 1) for d, v in sorted(plc.items())},
        }
        anos_out = {str(a): v for a, v in sorted(anos.items()) if v}
        tri_out = {c: v for c, v in sorted(tri.items()) if v}
        if not anos_out and not tri_out:
            continue
        empresas[cvm] = {
            "nome": nomes[cvm]["nome"],
            "setor": info_por_cnpj.get(cnpj, {}).get("setor", ""),
            # tickers ainda listados primeiro; entre eles, o provável mais líquido
            "tickers": sorted(tks, key=lambda t: (-tks[t], prioridade_ticker(t), t)),
            "obs": obs_out,
            "anos": anos_out,
            "tri": tri_out,       # {"2025T2": {...}} valores do trimestre isolado
            "ltm": ltm,           # últimos 12 meses (soma dos 4 últimos trimestres) ou null
            "pl": pl_out,         # PL dos controladores (R$ mil) no fim de cada ano / trimestre
            "_cnpj": cnpj,
        }
        for t, ano_t in tks.items():
            atual = mapa_tickers.get(t)
            if atual is None or ano_t >= atual[1]:
                mapa_tickers[t] = (cvm, ano_t)

    if "--sem-cotacoes" in sys.argv and ARQ_COTACOES.exists():
        cotacoes = json.loads(ARQ_COTACOES.read_text(encoding="utf-8"))
    else:
        # empresas com unit (ex.: KLBN11): baixa também ON e PN para o valor de mercado
        lista = set(mapa_tickers)
        for t in mapa_tickers:
            if t.endswith("11"):
                lista |= {t[:4] + "3", t[:4] + "4"}
        cotacoes = baixar_cotacoes(sorted(lista))

    # valor de mercado no último fechamento
    for cvm, emp in empresas.items():
        emp["mercado"] = valor_mercado(emp, acoes, cotacoes, pl_ctrl.get(cvm))
        del emp["_cnpj"]

    saida = {
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "unidade": "R$ mil",
        "empresas": empresas,
        "tickers": {t: v[0] for t, v in sorted(mapa_tickers.items())},
        # {ticker: {"2025": [var. preço %, retorno total %], "2025T2": [...],
        #           "_p": último fechamento, "_d": data, "_div12": proventos por ação em 12 meses}}
        "cotacoes": cotacoes,
    }
    SAIDA.write_text(
        "window.DADOS = " + json.dumps(saida, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    tam = SAIDA.stat().st_size / 1e6
    print(f"\nOK: {len(empresas)} empresas, {len(mapa_tickers)} tickers -> {SAIDA.name} ({tam:.1f} MB)")


if __name__ == "__main__":
    main()
