#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot de WhatsApp (via Evolution API) que dispara a aprovação de pedidos.

Fluxo:
  1. A Evolution API recebe as mensagens do WhatsApp (número dedicado) e envia
     um webhook (POST) para este servidor a cada mensagem.
  2. Este servidor confere: a mensagem é DA Adriana (número autorizado) e é o
     comando exato (@aprova)?
  3. Se for, roda o liberar_pedidos.py em MODO=live (headless) e responde no
     WhatsApp com o resumo (quantos aprovados e, por pedido, o primeiro nome do
     representante + o valor do pedido).

Segurança:
  - Só o número em WA_AUTORIZADO dispara. Qualquer outro remetente é ignorado.
  - Só DM (mensagens de grupo são ignoradas).
  - Uma aprovação por vez (trava) e de-duplicação de webhooks repetidos.

Config: mesmo arquivo .env do liberar_pedidos.py (chaves EVOLUTION_* e WA_*).
Uso:   ./.venv/bin/python bot_whatsapp.py
"""
import os
import re
import sys
import json
import threading
import subprocess
import datetime as dt
from collections import deque
from pathlib import Path

import requests
from flask import Flask, request, jsonify

REPO = Path(__file__).resolve().parent


# ----------------------------- config -----------------------------
def load_env():
    env = {}
    envf = REPO / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k, v in os.environ.items():          # shell tem prioridade
        env[k] = v
    return env


ENV = load_env()
EVOLUTION_URL = ENV.get("EVOLUTION_URL", "http://localhost:8080").rstrip("/")
EVOLUTION_APIKEY = ENV.get("EVOLUTION_APIKEY", "")
EVOLUTION_INSTANCE = ENV.get("EVOLUTION_INSTANCE", "")
# lista de números autorizados (separados por vírgula no .env)
WA_AUTORIZADOS = [n.strip() for n in ENV.get("WA_AUTORIZADO", "").split(",") if n.strip()]
WA_COMANDO = ENV.get("WA_COMANDO", "@aprova").strip().lower()
BOT_HOST = ENV.get("BOT_HOST", "0.0.0.0")
BOT_PORT = int(ENV.get("BOT_PORT", "8080"))
BOT_WEBHOOK_PATH = "/" + ENV.get("BOT_WEBHOOK_PATH", "webhook").strip("/")
BOT_PYTHON = ENV.get("BOT_PYTHON", str(REPO / ".venv" / "bin" / "python"))
RUN_TIMEOUT = int(ENV.get("BOT_RUN_TIMEOUT", "600"))  # tempo máx (s) da aprovação
RESULT_JSON = REPO / "logs" / "ultimo_resultado.json"
LOGF = REPO / "logs" / "bot.log"


def log(msg=""):
    linha = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linha, flush=True)
    try:
        LOGF.parent.mkdir(exist_ok=True)
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass


# ----------------------------- utilidades -----------------------------
def _digits(s):
    return re.sub(r"\D", "", s or "")


def _norm_br(num):
    """Normaliza um número BR: garante o DDI 55 (ex.: '91985055247' ->
    '5591985055247'). Números que já começam com 55 ficam como estão."""
    d = _digits(num)
    if d and not d.startswith("55") and len(d) in (10, 11):
        d = "55" + d
    return d


def _variacoes_br(num):
    """Variações de um número BR com/sem o 9º dígito (JIDs antigos às vezes vêm
    sem o 9). Assim '5594984171562' casa com '559484171562' e vice-versa."""
    d = _digits(num)
    vs = {d}
    m = re.match(r"^55(\d{2})(\d+)$", d)     # 55 + DDD(2) + resto
    if m:
        ddd, resto = m.group(1), m.group(2)
        if resto.startswith("9") and len(resto) == 9:   # tem 9 -> versão sem
            vs.add("55" + ddd + resto[1:])
        if len(resto) == 8:                              # sem 9 -> versão com
            vs.add("55" + ddd + "9" + resto)
    return vs


def _mesmo_numero(a, b):
    return bool(_variacoes_br(a) & _variacoes_br(b))


def _extrair(data):
    """Do payload da Evolution (data), tira (numero_remetente, texto, fromMe,
    is_grupo, msg_id)."""
    key = data.get("key", {}) or {}
    remote = key.get("remoteJid", "") or ""
    from_me = bool(key.get("fromMe"))
    msg_id = key.get("id", "")
    is_grupo = remote.endswith("@g.us")
    # em DM o remetente é o remoteJid; em grupo seria o participant
    remetente = key.get("participant") or remote
    numero = _digits(remetente.split("@")[0])
    msg = data.get("message", {}) or {}
    texto = (msg.get("conversation")
             or (msg.get("extendedTextMessage") or {}).get("text")
             or "")
    return numero, texto.strip(), from_me, is_grupo, msg_id


# ----------------------------- Evolution REST -----------------------------
def enviar_whatsapp(numero, texto):
    """Envia uma mensagem de texto pela Evolution API (v2)."""
    if not (EVOLUTION_INSTANCE and EVOLUTION_APIKEY):
        log("   ! Evolution não configurada (EVOLUTION_INSTANCE/APIKEY) — não enviei resposta.")
        return False
    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"
    try:
        r = requests.post(
            url,
            headers={"apikey": EVOLUTION_APIKEY, "Content-Type": "application/json"},
            json={"number": _digits(numero), "text": texto},
            timeout=30,
        )
        if r.status_code >= 300:
            log(f"   ! Evolution RECUSOU o envio p/ {_digits(numero)} "
                f"(HTTP {r.status_code}): {r.text[:300]}")
            return False
        try:
            j = r.json()
            mid = (j.get("key") or {}).get("id") or j.get("id") or "?"
            status = j.get("status") or "?"
            log(f"   → WhatsApp ACEITO p/ {_digits(numero)} (status={status}, id={mid})")
        except Exception:
            log(f"   → WhatsApp aceito p/ {_digits(numero)} (HTTP {r.status_code})")
        return True
    except Exception as e:
        log(f"   ! erro ao enviar WhatsApp p/ {_digits(numero)}: {e}")
        return False


# ----------------------------- aprovação -----------------------------
def _rodar_aprovacao():
    """Roda o liberar_pedidos.py em live/headless e devolve o resumo (dict)."""
    env = dict(os.environ)
    env["MODO"] = "live"
    env["HEADED"] = "0"          # sem janela na máquina do bot
    try:
        subprocess.run(
            [BOT_PYTHON, str(REPO / "liberar_pedidos.py")],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "erro": f"tempo esgotado ({RUN_TIMEOUT}s)", "pedidos": []}
    except Exception as e:
        return {"ok": False, "erro": f"falha ao executar: {e}", "pedidos": []}
    try:
        return json.loads(RESULT_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "erro": f"não consegui ler o resultado ({e})", "pedidos": []}


def _formatar_resumo(resumo):
    if resumo.get("erro"):
        return (f"⚠️ Não consegui concluir a aprovação: {resumo['erro']}\n"
                "Confira o sistema / os logs no servidor.")
    total = resumo.get("total_encontrados")
    peds = resumo.get("pedidos", []) or []
    conf = resumo.get("confirmados", 0)
    if not peds and (total == 0):
        return "✅ Pedidos aprovados (0)\nNão havia pedidos pendentes."
    # placar em destaque na 1ª linha
    linhas = [f"✅ Pedidos aprovados ({conf})"]
    for p in peds:
        icon = "✅" if p.get("confirmado") else "⛔"
        nome = (p.get("primeiro_nome") or "").strip() or "?"
        valor = (p.get("valor_fmt") or "").strip()
        # fmt() devolve "—" quando não conseguiu ler o valor
        linhas.append(f"{icon} {nome} - R$ {valor}" if valor and valor != "—"
                      else f"{icon} {nome}")
    if conf < len(peds):
        nao = len(peds) - conf
        linhas.append(f"⚠️ {nao} não confirmado(s) — confira no sistema.")
    return "\n".join(linhas)


run_lock = threading.Lock()


def _processar_e_responder(numero):
    """Roda a aprovação e responde o resumo. Chamado numa thread separada para
    não segurar a resposta HTTP do webhook (a aprovação demora ~1-2 min)."""
    try:
        resumo = _rodar_aprovacao()
        enviar_whatsapp(numero, _formatar_resumo(resumo))
        log(f"   resumo enviado: ok={resumo.get('ok')} confirmados={resumo.get('confirmados')}")
    except Exception as e:
        log(f"   ! erro no processamento: {e}")
        enviar_whatsapp(numero, f"⚠️ Erro inesperado ao processar: {e}")
    finally:
        run_lock.release()


# ----------------------------- servidor -----------------------------
app = Flask(__name__)
_vistos = deque(maxlen=200)          # ids de mensagens já processadas (de-dup)


@app.get("/")
def health():
    return "bot-aprovacao ok", 200


@app.get("/qr")
def qr():
    """Página que mostra o QR da instância e se atualiza sozinha até conectar.
    Evita o Manager e o problema do QR expirar."""
    estado, img = "?", ""
    try:
        st = requests.get(
            f"{EVOLUTION_URL}/instance/connectionState/{EVOLUTION_INSTANCE}",
            headers={"apikey": EVOLUTION_APIKEY}, timeout=15).json()
        estado = (st.get("instance") or {}).get("state") or st.get("state") or "?"
    except Exception as e:
        estado = f"erro ({e})"
    if estado != "open":
        try:
            c = requests.get(
                f"{EVOLUTION_URL}/instance/connect/{EVOLUTION_INSTANCE}",
                headers={"apikey": EVOLUTION_APIKEY}, timeout=15).json()
            img = c.get("base64") or ""
        except Exception:
            img = ""
    if estado == "open":
        refresh, body = "", ("<h2>✅ Conectado!</h2>"
                             "<p>Pode fechar esta aba. Mande <b>@aprova</b> para testar.</p>")
    elif img:
        refresh = '<meta http-equiv="refresh" content="20">'
        body = ("<h2>Escaneie com o WhatsApp do bot (5594981690110)</h2>"
                "<p>WhatsApp → Aparelhos conectados → Conectar um aparelho</p>"
                f'<img src="{img}" style="width:320px;height:320px">'
                f"<p>estado: {estado} — a página atualiza o QR sozinha.</p>")
    else:
        refresh = '<meta http-equiv="refresh" content="4">'
        body = f"<h2>Aguardando o QR...</h2><p>estado: {estado}</p>"
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            f"{refresh}<title>QR do bot</title></head>"
            "<body style='font-family:sans-serif;text-align:center;padding:24px'>"
            f"{body}</body></html>")


@app.route(BOT_WEBHOOK_PATH, methods=["POST"])
@app.route(BOT_WEBHOOK_PATH + "/<path:_sub>", methods=["POST"])
def webhook(_sub=None):
    payload = request.get_json(silent=True) or {}
    evento = (payload.get("event") or "").lower().replace("_", ".")
    data = payload.get("data") or {}
    # só nos importam mensagens novas recebidas
    if evento and evento != "messages.upsert":
        return jsonify(ok=True, ignored=evento), 200
    if isinstance(data, list):               # algumas versões mandam lista
        data = data[0] if data else {}

    numero, texto, from_me, is_grupo, msg_id = _extrair(data)
    if from_me or not texto:
        return jsonify(ok=True), 200
    if msg_id and msg_id in _vistos:         # webhook repetido
        return jsonify(ok=True, dup=True), 200
    if msg_id:
        _vistos.append(msg_id)

    comando_ok = texto.lower().startswith(WA_COMANDO)
    autorizado = any(_mesmo_numero(numero, _norm_br(n)) for n in WA_AUTORIZADOS)

    if not comando_ok:
        return jsonify(ok=True), 200         # mensagem qualquer: ignora em silêncio
    if is_grupo:
        log(f"comando em GRUPO ignorado (só DM). de={numero}")
        return jsonify(ok=True), 200
    if not autorizado:
        log(f"comando '{texto}' de número NÃO autorizado ({numero}) — ignorado.")
        return jsonify(ok=True, unauthorized=True), 200

    # é a Adriana e é o comando -> dispara
    log(f"comando autorizado recebido de {numero}: '{texto}'")
    if not run_lock.acquire(blocking=False):
        enviar_whatsapp(numero, "⏳ Já estou processando uma aprovação. Aguarde terminar.")
        return jsonify(ok=True, busy=True), 200

    enviar_whatsapp(numero, "🔄 Recebido. Processando as aprovações, um instante...")
    threading.Thread(target=_processar_e_responder, args=(numero,), daemon=True).start()
    return jsonify(ok=True, triggered=True), 200


def _checagem_inicial():
    faltando = [k for k, v in {
        "EVOLUTION_URL": EVOLUTION_URL, "EVOLUTION_APIKEY": EVOLUTION_APIKEY,
        "EVOLUTION_INSTANCE": EVOLUTION_INSTANCE, "WA_AUTORIZADO": WA_AUTORIZADOS,
    }.items() if not v]
    log("=" * 60)
    log("BOT DE APROVAÇÃO (WhatsApp via Evolution)")
    log(f"  Evolution: {EVOLUTION_URL}  instância={EVOLUTION_INSTANCE or '(vazio)'}")
    log(f"  Autorizados: {', '.join(WA_AUTORIZADOS) or '(vazio)'}  comando='{WA_COMANDO}'")
    log(f"  Webhook: http://{BOT_HOST}:{BOT_PORT}{BOT_WEBHOOK_PATH}")
    if faltando:
        log(f"  ! ATENÇÃO: configure no .env: {', '.join(faltando)}")
    log("=" * 60)


if __name__ == "__main__":
    _checagem_inicial()
    app.run(host=BOT_HOST, port=BOT_PORT, threaded=True)
