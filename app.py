"""
Mini-API Flecha · Cazati
Recebe o XML autorizado da NF-e (vindo da Sysemp), gera o PDF (DANFE),
guarda PDF + XML no Supabase Storage e devolve links assinados (14 dias).

Rotas:
  GET  /health   -> teste de vida (sem chave)
  POST /nf       -> gera PDF, sobe arquivos, devolve links + dados da nota
  POST /limpeza  -> apaga arquivos com mais de RETENCAO_DIAS dias

Autenticação: header  X-Api-Key: <API_KEY>
"""
import base64
import io
import os
import re
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request
from brazilfiscalreport.danfe import Danfe

API_KEY = os.environ.get("API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = os.environ.get("SUPABASE_BUCKET", "notas-fiscais")
RETENCAO_DIAS = int(os.environ.get("RETENCAO_DIAS", "14"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"  # testa sem Supabase

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}
app = Flask(__name__)


# ---------- utilidades ----------
def autorizado():
    return API_KEY and secrets.compare_digest(request.headers.get("X-Api-Key", ""), API_KEY)


def ler_xml(payload):
    """Aceita JSON {"xml_base64": "..."} ou {"xml": "..."} ou o XML cru no corpo."""
    if payload:
        if payload.get("xml_base64"):
            return base64.b64decode(payload["xml_base64"]).decode("utf-8")
        if payload.get("xml"):
            return payload["xml"]
    corpo = request.get_data(as_text=True)
    if corpo.strip().startswith("<"):
        return corpo
    raise ValueError("Envie xml_base64, xml ou o XML no corpo")


def txt(raiz, caminho):
    el = raiz.find(caminho, NS)
    return el.text.strip() if el is not None and el.text else ""


def dados_da_nota(xml):
    raiz = ET.fromstring(xml)
    inf = raiz.find(".//n:infNFe", NS)
    if inf is None:
        raise ValueError("XML não é uma NF-e")
    chave = (inf.get("Id") or "").replace("NFe", "")
    return {
        "chave": chave,
        "numero": txt(inf, "n:ide/n:nNF"),
        "serie": txt(inf, "n:ide/n:serie"),
        "emissao": txt(inf, "n:ide/n:dhEmi"),
        "pedido": txt(inf, "n:compra/n:xPed") or txt(inf, "n:det/n:prod/n:xPed"),
        "cliente_nome": txt(inf, "n:dest/n:xNome"),
        "cliente_cpf": txt(inf, "n:dest/n:CPF") or txt(inf, "n:dest/n:CNPJ"),
        "cliente_email": txt(inf, "n:dest/n:email"),
        "cliente_fone": txt(inf, "n:dest/n:enderDest/n:fone"),
        "transportadora": txt(inf, "n:transp/n:transporta/n:xNome"),
        "valor": txt(inf, "n:total/n:ICMSTot/n:vNF"),
        "autorizada": txt(raiz, ".//n:protNFe/n:infProt/n:cStat") == "100",
    }


def gerar_pdf(xml):
    buf = io.BytesIO()
    Danfe(xml=xml).output(buf)
    return buf.getvalue()


def sb_headers(extra=None):
    h = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
    h.update(extra or {})
    return h


def subir(caminho, conteudo, tipo):
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{caminho}",
        headers=sb_headers({"Content-Type": tipo, "x-upsert": "true"}),
        data=conteudo, timeout=30,
    )
    r.raise_for_status()


def link_assinado(caminho, nome_download):
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/sign/{BUCKET}/{caminho}",
        headers=sb_headers({"Content-Type": "application/json"}),
        json={"expiresIn": RETENCAO_DIAS * 86400}, timeout=30,
    )
    r.raise_for_status()
    url = f"{SUPABASE_URL}/storage/v1{r.json()['signedURL']}"
    return f"{url}&download={nome_download}"


# ---------- rotas ----------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.post("/nf")
def nf():
    if not autorizado():
        return jsonify(erro="não autorizado"), 401
    try:
        xml = ler_xml(request.get_json(silent=True))
        dados = dados_da_nota(xml)
    except Exception as e:
        return jsonify(erro=f"XML inválido: {e}"), 400
    if not dados["autorizada"]:
        return jsonify(erro="NF não autorizada pela SEFAZ (cStat != 100)", dados=dados), 422

    try:
        pdf = gerar_pdf(xml)
    except Exception as e:
        return jsonify(erro=f"Falha ao gerar PDF: {e}", dados=dados), 500

    nome = f"NF_{dados['numero']}"
    # sufixo aleatório: nome do arquivo não é adivinhável
    base = f"{nome}_{secrets.token_hex(6)}"
    resposta = {
        "dados": dados,
        "arquivo_pdf": f"{nome}.pdf",
        "arquivo_xml": f"{nome}.xml",
        "pdf_base64": base64.b64encode(pdf).decode(),  # anexo do e-mail no Make
        "validade_dias": RETENCAO_DIAS,
    }
    if DRY_RUN:
        resposta.update(link_pdf="(dry-run)", link_xml="(dry-run)")
        return jsonify(resposta)

    try:
        subir(f"{base}.pdf", pdf, "application/pdf")
        subir(f"{base}.xml", xml.encode("utf-8"), "application/xml")
        resposta["link_pdf"] = link_assinado(f"{base}.pdf", f"{nome}.pdf")
        resposta["link_xml"] = link_assinado(f"{base}.xml", f"{nome}.xml")
    except Exception as e:
        return jsonify(erro=f"Falha no Supabase: {e}", dados=dados), 502
    return jsonify(resposta)


@app.post("/limpeza")
def limpeza():
    if not autorizado():
        return jsonify(erro="não autorizado"), 401
    limite = datetime.now(timezone.utc) - timedelta(days=RETENCAO_DIAS)
    apagados, offset = [], 0
    while True:
        r = requests.post(
            f"{SUPABASE_URL}/storage/v1/object/list/{BUCKET}",
            headers=sb_headers({"Content-Type": "application/json"}),
            json={"prefix": "", "limit": 1000, "offset": offset,
                  "sortBy": {"column": "created_at", "order": "asc"}},
            timeout=30,
        )
        r.raise_for_status()
        itens = r.json()
        if not itens:
            break
        for it in itens:
            criado = it.get("created_at")
            if not it.get("id") or not criado:
                continue  # pasta
            dt = datetime.fromisoformat(re.sub(r"Z$", "+00:00", criado))
            if dt < limite:
                apagados.append(it["name"])
        if len(itens) < 1000:
            break
        offset += 1000
    for i in range(0, len(apagados), 500):
        requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{BUCKET}",
            headers=sb_headers({"Content-Type": "application/json"}),
            json={"prefixes": apagados[i:i + 500]}, timeout=30,
        ).raise_for_status()
    return jsonify(apagados=len(apagados))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
