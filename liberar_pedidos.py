#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automação de liberação (autorização) de pedidos - maxGestão / Soluções Máxima.

Fluxo:
  1. Login no portal app.solucoesmaxima.com.br
  2. Clica no card maxGESTÃO (abre nova aba com SSO)
  3. Vai para a tela de Autorização de Pedido
  4. Filtra pelo representante (código do .env, ex: 182 = Adriana) e pesquisa
  5. Para cada pedido "aguardando autorização":
       - abre o pedido (ícone "i")
       - lê "Saldo após aprovação do pedido"
       - clica em "Aceitar Pedido"
       - preenche "Observações de Autorização"
       - liga/desliga "Debitar da conta corrente do RCA" conforme o saldo:
             saldo >= 0  -> LIGA a opção
             saldo <  0  -> DESLIGA a opção
       - MODO=dry-run: apenas mostra o que faria (NÃO confirma)
         MODO=live:    clica em "Confirmar"

Configuração: arquivo .env (ao lado deste script).
Uso:  ./.venv/bin/python liberar_pedidos.py
"""
import os
import re
import sys
import json
import time
import datetime as dt
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

REPO = Path(__file__).resolve().parent
OUT = REPO / "capturas"
OUT.mkdir(exist_ok=True)
LOGF = REPO / "logs"
LOGF.mkdir(exist_ok=True)
_logfile = LOGF / f"run_{dt.datetime.now():%Y%m%d_%H%M%S}.log"


# ----------------------------- infra -----------------------------
def log(msg=""):
    linha = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    print(linha, flush=True)
    with open(_logfile, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def load_env():
    env = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    # variáveis de ambiente do shell têm prioridade (override em runtime)
    for k in list(env.keys()) + ["FORCE_TOGGLE", "DIAG", "DIAG_TOGGLE"]:
        if k in os.environ:
            env[k] = os.environ[k]
    return env


ENV = load_env()
MODO = ENV.get("MODO", "dry-run").lower()
DRY = MODO != "live"
OBSERVACAO = ENV.get("OBSERVACAO", "Autorizado por Adriana")
CODIGO = ENV.get("CODIGO_FILTRO", "182")
SIG = ENV["GESTAO_LOGO_SIG"]
AUTORIZACAO_URL = ENV["AUTORIZACAO_URL"]
SSO_TIMEOUT = int(ENV.get("SSO_TIMEOUT", "120"))
DRY_PAUSE = float(ENV.get("DRY_PAUSE", "5"))
FINAL_HOLD = float(ENV.get("FINAL_HOLD", "30"))
MAX_PEDIDOS = int(ENV.get("MAX_PEDIDOS", "0"))


def parse_valor(txt):
    """Converte '1.234,56' / '-1.234,56' / 'R$ 0,00' em float (padrão BR)."""
    if txt is None:
        return None
    t = str(txt).strip()
    if t == "":
        return None
    negativo = ("-" in t) or ("(" in t and ")" in t)
    t = re.sub(r"[^0-9,]", "", t)      # mantém só dígitos e vírgula
    t = t.replace(",", ".")
    if t in ("", "."):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if negativo else v


def fmt(v):
    return "—" if v is None else f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ----------------------------- navegação -----------------------------
def login_portal(page):
    log("1) Login no portal...")
    page.goto(ENV["MAXIMA_URL"], wait_until="commit", timeout=90000)
    # espera o campo aparecer (mais robusto que o evento de load)
    page.wait_for_selector("input[name=username]", state="visible", timeout=90000)
    page.fill("input[name=username]", ENV["MAXIMA_USER"], timeout=30000)
    page.fill("input[name=password]", ENV["MAXIMA_PASS"], timeout=30000)
    page.click("button[type=submit]")
    page.wait_for_selector("text=Soluções para", timeout=60000)
    log("   portal carregado.")


def abrir_maxgestao(page, contexto, tabs):
    log("2) Abrindo maxGESTÃO (clique no card + SSO)...")
    card = page.locator(f'img[src*="{SIG}"]')
    if card.count() == 0:
        raise RuntimeError("Card do maxGESTÃO não encontrado no portal (logo/assinatura mudou?).")
    card.first.click(timeout=10000)

    # espera a aba do maxgestao aparecer (SSO costuma demorar)
    log(f"   aguardando SSO (até {SSO_TIMEOUT}s)...")
    alvo = None
    fim = time.time() + SSO_TIMEOUT
    proximo_reclique = time.time() + 15
    while time.time() < fim:
        for t in list(tabs):
            try:
                if "maxgestao.solucoesmaxima" in (t.url or ""):
                    alvo = t
                    break
            except Exception:
                pass
        if alvo:
            break
        # reclica no card a cada 15s enquanto a aba não aparece (o SSO às vezes
        # ignora o 1º clique)
        if time.time() > proximo_reclique:
            proximo_reclique = time.time() + 15
            try:
                card.first.click(timeout=5000)
                log("     (recliquei no card maxGESTÃO aguardando o SSO)")
            except Exception:
                pass
        time.sleep(1)
    if not alvo:
        raise RuntimeError("A aba do maxGestão não abriu dentro do tempo (SSO_TIMEOUT).")
    log(f"   aba maxGestão aberta: {alvo.url[:60]}...")
    url_token = alvo.url                      # tem o ?PWE=<token> (reutilizável)
    try:
        alvo.wait_for_load_state("load", timeout=25000)
    except Exception:
        pass
    if not _esperar_app_pronto(alvo, url_token):
        raise RuntimeError("maxGestão não terminou de montar após o SSO (travou na tela do token).")
    return alvo


def _esperar_app_pronto(mg, url_token=None, tentativas=3, espera=25):
    """Garante que o maxGestão terminou de BOOTAR após o SSO: ele precisa consumir o
    token (?PWE=) e redirecionar pra '#/pages/...', montando o menu. Às vezes trava na
    URL do token e o menu nunca aparece — aí RE-NAVEGAMOS a URL do token (reutilizável,
    verificado) pra re-disparar o boot. Reage ao render (menu montado), não a tempo fixo."""
    for tent in range(1, tentativas + 1):
        try:
            mg.wait_for_selector("a.m-menu__link", timeout=espera * 1000)
            log("   app maxGestão pronto (menu montado).")
            return True
        except Exception:
            pass
        url = mg.url or ""
        log(f"   app não montou (tentativa {tent}/{tentativas}, url={url[:55]}...) — re-disparando boot")
        alvo_url = url_token if (url_token and "PWE=" in url_token) else url
        try:
            mg.goto(alvo_url, wait_until="commit", timeout=30000)
        except Exception as e:
            log(f"     (re-navegação do token falhou: {e})")
        time.sleep(3)
    return False


def ir_para_autorizacao(page):
    log("3) Indo para Autorização de Pedido (pelo menu)...")
    # OBS.: tentamos navegar DIRETO trocando o hash (#/pages/cadastros/autorizacao-pedido),
    # mas o maxGestão NÃO suporta: a rota tem guard/resolver que depende do contexto que
    # o próprio menu seta — pular isso DESMONTA o shell (o menu some). Então navegamos
    # pelo menu mesmo. Ele só era frágil quando o app não tinha bootado; agora o
    # abrir_maxgestao garante o boot antes, então o menu é confiável.
    _ir_por_menu(page)
    expandir_filtros(page)
    page.wait_for_selector("#filtro-avancado-botao-pesquisa", state="visible", timeout=30000)
    page.wait_for_selector("mat-select[aria-label='Equipe']", timeout=30000)
    time.sleep(1)
    log("   filtros prontos.")


def _ir_por_menu(page):
    """Fallback: navega pelo menu lateral (accordion 'Autorizações' -> item)."""
    page.wait_for_selector("a.m-menu__link", timeout=90000)
    time.sleep(3)
    # 1) grupo "Autorizações" (plural). Só ele contém a palavra 'Autorizações'.
    grupo = page.locator("a.m-menu__toggle:has-text('Autorizações')").first
    # 2) item exato "Autorização de pedido" (texto exato, p/ não pegar o
    #    "Autorização de Pedido/Orçamento", que é outro item).
    item = page.locator(
        "a.m-menu__link:has(span.m-menu__link-text:text-is('Autorização de pedido'))"
    ).first
    for tentativa in range(3):
        try:
            if item.is_visible():
                break
        except Exception:
            pass
        grupo.click(timeout=15000)
        try:
            item.wait_for(state="visible", timeout=8000)
            break
        except PWTimeout:
            log(f"   submenu 'Autorizações' não abriu (tentativa {tentativa + 1})...")
    item.click(timeout=15000)
    page.wait_for_selector("text=Filtros avançados", timeout=60000)
    log("   página de autorização carregada (via menu).")


def expandir_filtros(page):
    """O painel 'Filtros avançados' vem recolhido; clica no olho/cabeçalho p/ abrir."""
    time.sleep(1.5)
    botao = page.locator("#filtro-avancado-botao-pesquisa")
    try:
        if botao.count() and botao.first.is_visible():
            return
    except Exception:
        pass
    log("   expandindo 'Filtros avançados'...")
    candidatos = [
        "i.la-eye", "i.la-eye-slash", "i[class*='eye']", "[class*='la-eye']",
        "mat-icon:has-text('visibility')",
    ]
    for sel in candidatos:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=4000)
                time.sleep(1.5)
                if page.locator("#filtro-avancado-botao-pesquisa").first.is_visible():
                    log(f"   filtros expandidos (via {sel}).")
                    return
        except Exception:
            pass
    # último recurso: clicar no próprio título
    try:
        page.get_by_text("Filtros avançados", exact=False).first.click(timeout=4000)
        time.sleep(1.5)
    except Exception:
        pass


def _dump_opcoes(page, rotulo, limite=40):
    ops = page.locator("mat-option:visible")
    n = ops.count()
    log(f"   {rotulo}: {n} opção(ões) visíveis")
    for i in range(min(n, limite)):
        try:
            log(f"     - {ops.nth(i).inner_text().strip()!r}")
        except Exception:
            pass


def diagnostico_equipe(page):
    """Abre Equipe e Representante, lista opções e testa digitar o código."""
    log("== DIAGNÓSTICO DE FILTROS ==")
    for label in ("Equipe", "Representante"):
        sel = f"mat-select[aria-label='{label}']"
        loc = page.locator(sel).first
        try:
            disabled = loc.get_attribute("aria-disabled")
        except Exception:
            disabled = "?"
        log(f"-- campo {label}: aria-disabled={disabled}")
        try:
            loc.click(timeout=6000)
            time.sleep(1.5)
            page.screenshot(path=str(OUT / f"diag_{label.lower()}.png"))
            _dump_opcoes(page, f"{label} (sem filtro)")
            busca = page.locator('input[placeholder="Pesquise..."]:visible')
            if busca.count():
                busca.first.fill(CODIGO, timeout=5000)
                time.sleep(1.5)
                _dump_opcoes(page, f"{label} (após digitar {CODIGO})", 20)
                page.screenshot(path=str(OUT / f"diag_{label.lower()}_{CODIGO}.png"))
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception as e:
            log(f"   (não consegui abrir {label}: {e})")
    log("== FIM DIAGNÓSTICO ==")


def filtrar(page):
    log(f"4) Filtrando pela equipe {CODIGO} (Adriana)...")
    if ENV.get("DIAG", "0") == "1":
        diagnostico_equipe(page)
        raise SystemExit(0)
    # garante o status 'aguardando autorização' marcado
    try:
        radio = page.get_by_text("Solicitações aguardando autorização", exact=False).first
        radio.click(timeout=4000)
    except Exception:
        pass

    # abre o select EQUIPE (é aqui que fica "182 - ADRIANA GONÇALVES DOS SANTOS")
    page.locator("mat-select[aria-label='Equipe']").click(timeout=10000)
    time.sleep(1)
    # digita o código na busca do dropdown
    busca = page.locator('input[placeholder="Pesquise..."]:visible').first
    busca.fill(CODIGO, timeout=8000)
    time.sleep(1.5)
    # escolhe a opção que começa com o código (ex.: "182 - ADRIANA ...")
    opcoes = page.locator("mat-option:visible")
    n = opcoes.count()
    escolhido = None
    for i in range(n):
        txt = (opcoes.nth(i).inner_text() or "").strip()
        if txt.startswith(CODIGO) or re.match(rf"^0*{CODIGO}\b", txt):
            opcoes.nth(i).click()
            escolhido = txt
            break
    if escolhido is None and n > 0:
        escolhido = (opcoes.first.inner_text() or "").strip()
        opcoes.first.click()
    log(f"   equipe selecionada: {escolhido!r}")
    if escolhido is None:
        raise RuntimeError(f"Nenhuma equipe encontrada para o código {CODIGO}.")
    page.keyboard.press("Escape")  # fecha o dropdown (multi-select)
    time.sleep(0.5)
    page.locator("#filtro-avancado-botao-pesquisa").click(timeout=10000)
    _esperar_loading(page)                    # espera o overlay global sumir (fim da busca)
    # espera os resultados RENDERIZAREM (ou confirmar vazio) — reage ao DOM,
    # não a um tempo fixo.
    botoes = 0
    for _ in range(12):
        try:
            botoes = page.locator("button:has(em.editar)").count()
        except Exception:
            botoes = 0
        if botoes > 0:
            break
        time.sleep(1)
    try:
        footer = page.get_by_text(re.compile(r"Total de registros"), exact=False).first.inner_text().strip()
    except Exception:
        footer = "?"
    try:
        page.screenshot(path=str(OUT / "apos_filtro.png"))
    except Exception:
        pass
    log(f"   pesquisa executada. rodapé={footer!r} | botões 'editar' encontrados={botoes}")


# ----------------------------- processamento -----------------------------
def contar_pedidos(page):
    return page.locator("button:has(em.editar)").count()


def _fechar_swal(page, timeout=6000):
    """Fecha o popup SweetAlert2 que o maxGestão mostra DEPOIS de confirmar uma
    autorização (ele fica na frente e bloqueia cliques, ex.: o 'Pesquisar').
    Retorna (tipo, texto): tipo = 'success' / 'error' / 'unknown' pelo ícone e
    texto = a mensagem do popup (ex.: o motivo da recusa), ou (None, None) se
    não apareceu."""
    tipo = texto = None
    try:
        try:
            page.locator(".swal2-container").first.wait_for(state="visible", timeout=timeout)
        except Exception:
            return None, None                # nenhum popup apareceu
        try:
            if page.locator(".swal2-icon.swal2-success").first.is_visible():
                tipo = "success"
            elif page.locator(".swal2-icon.swal2-error").first.is_visible():
                tipo = "error"
            else:
                tipo = "unknown"
        except Exception:
            tipo = "unknown"
        # lê a mensagem ANTES de fechar. O corpo tem classe diferente conforme a
        # versão do SweetAlert2 (html-container nas novas, content nas antigas);
        # o título fica de fallback quando não há corpo.
        try:
            texto = page.evaluate("""() => {
              const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
              const pega = sel => { const e = document.querySelector(sel);
                                    return e ? norm(e.innerText) : ''; };
              return pega('.swal2-html-container') || pega('.swal2-content')
                  || pega('.swal2-title') || null;
            }""")
        except Exception:
            texto = None
        try:
            btn = page.locator("button.swal2-confirm")
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=4000)
            else:
                page.keyboard.press("Enter")
        except Exception:
            pass
        try:                                 # espera o overlay sumir
            page.wait_for_selector(".swal2-container", state="hidden", timeout=5000)
        except Exception:
            pass
    except Exception:
        pass
    return tipo, texto


def _footer_total(page):
    """Lê o N de 'Total de registros: N' do rodapé (a contagem OFICIAL da busca)."""
    try:
        t = page.get_by_text(re.compile(r"Total de registros"), exact=False).first.inner_text()
        m = re.search(r"Total de registros:\s*([\d.]+)", t)
        if m:
            return int(m.group(1).replace(".", ""))
    except Exception:
        pass
    return None


def _esperar_loading(page, aparecer=4000, sumir=30000):
    """Espera o overlay GLOBAL do maxGestão terminar. É um <div class="loading-principal">
    fullscreen (z-index 1999) que cobre a tela durante cada requisição. Enquanto ele
    está visível o grid fica vazio e o rodapé mostra 'Total de registros: 0' — ler a
    contagem aí engana (foi o bug do "só aprova 1"). O overlay fica no DOM (só é
    escondido via CSS), então esperamos ele aparecer (confirma que o reload começou)
    e depois SUMIR (reload terminou). Reage ao render, não a tempo fixo."""
    try:
        page.wait_for_selector(".loading-principal", state="visible", timeout=aparecer)
    except Exception:
        pass          # reload rápido demais / já em andamento — segue pro 'hidden'
    try:
        page.wait_for_selector(".loading-principal", state="hidden", timeout=sumir)
    except Exception:
        pass


def _garantir_filtros(page):
    """Re-garante os filtros (status 'aguardando' + equipe CODIGO) SEM desmarcar
    o que já está. Necessário porque abrir/fechar o modal de um pedido às vezes
    zera os filtros, e aí a re-pesquisa volta 0 resultados."""
    # status: clicar num radio já marcado o mantém marcado (radio não alterna)
    try:
        page.get_by_text("Solicitações aguardando autorização", exact=False).first.click(timeout=4000)
    except Exception:
        pass
    # equipe: só re-seleciona se ainda NÃO estiver mostrando o código (evita
    # desmarcar um multi-select já selecionado)
    try:
        val = page.locator("mat-select[aria-label='Equipe']").first.inner_text(timeout=3000)
    except Exception:
        val = ""
    if CODIGO not in (val or ""):
        log(f"     [filtros] equipe não estava aplicada (valor={val!r}) — re-selecionando {CODIGO}")
        try:
            page.locator("mat-select[aria-label='Equipe']").click(timeout=8000)
            busca = page.locator('input[placeholder="Pesquise..."]:visible').first
            busca.fill(CODIGO, timeout=6000)
            time.sleep(1.0)
            ops = page.locator("mat-option:visible")
            for i in range(ops.count()):
                txt = (ops.nth(i).inner_text() or "").strip()
                if txt.startswith(CODIGO) or re.match(rf"^0*{CODIGO}\b", txt):
                    ops.nth(i).click()
                    break
            page.keyboard.press("Escape")
            time.sleep(0.4)
        except Exception as e:
            log(f"     [filtros] falha ao re-selecionar equipe: {e}")


def repesquisar(page, antes=None, timeout=30):
    """Re-dispara o 'Pesquisar' e ESPERA a busca terminar de recarregar.
    Antes, RE-GARANTE os filtros (eles se perdem ao abrir/fechar o modal). O grid
    fica vazio por vários segundos no reload, então contar só as linhas engana:
    usamos o rodapé 'Total de registros: N' como âncora — a busca terminou quando
    o rodapé BATE com a quantidade de botões visíveis, estável. Loga o progresso."""
    _fechar_swal(page, timeout=1500)         # limpa popup de sucesso que reste
    _garantir_filtros(page)                  # <-- reaplica equipe/status
    try:
        page.locator("#filtro-avancado-botao-pesquisa").click(timeout=10000)
    except Exception as e:
        log(f"   (não consegui reclicar em 'Pesquisar': {e})")
        return contar_pedidos(page)
    _esperar_loading(page)                    # <-- ESPERA o overlay sumir (fim do reload).
    # Só agora a contagem é confiável: durante o loading o rodapé fica em 0 e enganava.
    fim = time.time() + timeout
    ok_seguidos = 0
    ult_log = 0.0
    resultado = None
    while time.time() < fim:
        botoes = contar_pedidos(page)
        footer = _footer_total(page)
        consistente = (footer is not None and footer == botoes)
        if time.time() - ult_log > 1.0:      # log leve de progresso
            log(f"     [repesquisar] rodapé={footer} botões={botoes} ok={consistente}")
            ult_log = time.time()
        if consistente:
            ok_seguidos += 1
            if ok_seguidos >= 2:             # 2 leituras consistentes seguidas
                resultado = botoes
                break
        else:
            ok_seguidos = 0
        time.sleep(0.5)
    if resultado is None:
        fb = _footer_total(page)
        resultado = fb if fb is not None else contar_pedidos(page)
        log(f"     [repesquisar] timeout — rodapé={fb} botões={contar_pedidos(page)}")
    _diag_filtros(page, resultado)           # <-- foto + estado do filtro
    return resultado


def _diag_filtros(page, resultado):
    """Registra o estado do filtro após a re-pesquisa e tira um screenshot,
    pra entender por que às vezes volta 0 mesmo com pedido pendente."""
    try:
        eq = page.locator("mat-select[aria-label='Equipe']").first.inner_text(timeout=2000).strip()
    except Exception:
        eq = "?"
    try:
        agu = page.evaluate("""() => {
          const els = [...document.querySelectorAll('mat-radio-button, .mat-radio-button, label')];
          const el = els.find(e => (e.textContent||'').includes('aguardando autoriza'));
          if (!el) return 'texto-nao-achado';
          const cls = (el.className||'') + ' ' + (el.querySelector('input')?.checked ? 'input-checked' : '');
          return cls.includes('checked') ? 'MARCADO' : 'desmarcado';
        }""")
    except Exception as e:
        agu = f"erro({e})"
    log(f"     [diag] equipe={eq!r} | radio-aguardando={agu} | resultado={resultado}")
    try:
        page.screenshot(path=str(OUT / "repesquisar_estado.png"))
        log("     [diag] screenshot salvo: repesquisar_estado.png")
    except Exception:
        pass


def ler_saldo(page, timeout=18):
    """Lê 'Saldo após aprovação do pedido'.
    ATENÇÃO: o valor NÃO fica no input — é um texto solto ao lado dele, dentro
    do <div> pai (ex.: <input ...readonly>R$ 1.185,05</div>). Então lemos o
    texto do elemento pai. O modal carrega o valor com atraso -> poll."""
    js = """
    () => {
      const inp = document.querySelector('input[placeholder="Saldo após aprovação do pedido"]');
      if (!inp) return null;
      // texto do container (o valor fica fora do input, como nó de texto irmão)
      const t = (inp.parentElement ? inp.parentElement.textContent : '') || inp.value || '';
      return t.trim();
    }
    """
    fim = time.time() + timeout
    val = ""
    while time.time() < fim:
        try:
            val = page.evaluate(js)
        except Exception:
            val = ""
        val = (val or "").strip()
        if val != "":
            return val
        time.sleep(0.6)
    return val


def _ler_campo_modal(page, rotulo):
    """Lê um campo do modal 'Detalhes do Pedido' (ex.: 'Representante',
    'Valor do pedido'). Mesmo padrão do saldo: o <input> é só o rótulo
    (placeholder) e o VALOR é um nó de texto solto no elemento pai.
    A busca é limitada ao modal — a tela de filtros tem campos com nomes
    iguais ('Representante') e pegaria o valor errado."""
    js = """
    (ph) => {
      const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
      const limpa = t => { t = norm(t); return t.startsWith(ph) ? norm(t.slice(ph.length)) : t; };
      const anchor = document.querySelector('input[placeholder="Saldo após aprovação do pedido"]');
      const root = (anchor && anchor.closest(
        'mat-dialog-container, .mat-dialog-container, [role=dialog], .modal-content, .modal')) || document;
      // 1) input cujo placeholder é o rótulo; valor = texto do container pai
      const inp = [...root.querySelectorAll('input')]
            .find(i => norm(i.getAttribute('placeholder')) === ph);
      if (inp) {
        const t = limpa(inp.parentElement ? inp.parentElement.textContent : '') || norm(inp.value);
        if (t) return t;
      }
      // 2) fallback: rótulo em <label>/<span> e valor no mesmo bloco
      const lab = [...root.querySelectorAll('label, span, small, div')]
            .find(e => norm(e.textContent) === ph);
      if (lab && lab.parentElement) {
        const t = limpa(lab.parentElement.textContent);
        if (t) return t;
      }
      return null;
    }
    """
    try:
        return (page.evaluate(js, rotulo) or "").strip() or None
    except Exception:
        return None


def _ler_numero_pedido(page):
    """Nº do pedido, do título do modal ('Detalhes do Pedido Nº 1678217961').
    É a identidade usada pra reconhecer um pedido já pulado. Busca restrita ao
    modal aberto, pra não pegar título velho de outro pedido."""
    js = """
    () => {
      const anchor = document.querySelector('input[placeholder="Saldo após aprovação do pedido"]');
      const root = (anchor && anchor.closest(
        'mat-dialog-container, .mat-dialog-container, [role=dialog], .modal-content, .modal')) || document.body;
      const m = (root.innerText || '').match(/Detalhes do Pedido\\s*N\\S*\\s*(\\d+)/i);
      return m ? m[1] : null;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return None


def _modal_fechado(page, timeout=8000):
    """True quando os modais do pedido (detalhe e confirmação) sumiram da tela."""
    try:
        page.wait_for_selector('input[placeholder="Saldo após aprovação do pedido"]',
                               state="hidden", timeout=timeout)
        page.wait_for_selector('input[placeholder="Observações de Autorização"]',
                               state="hidden", timeout=timeout)
        return True
    except Exception:
        return False


def primeiro_nome(representante):
    """'60917 - ELIZANGELA MARIA DA SILVA' -> 'ELIZANGELA'.

    Parte dos cadastros vem com o prefixo 'VEND.' antes do nome
    ('00000 -VEND. SABRINA ...'); ele é descartado. O ponto (ou o espaço) é
    exigido pra não mutilar nome que só COMEÇA com essas letras (ex.: 'VENDA').
    """
    if not representante:
        return None
    t = re.sub(r"^\s*\d+\s*[-–]\s*", "", str(representante).strip())
    t = re.sub(r"^\s*VEND\s*(?:\.\s*|\s+)", "", t, flags=re.I)
    partes = t.split()
    return partes[0] if partes else None


def fechar_modais(page):
    """Fecha modal de confirmação e de detalhe, se abertos."""
    _fechar_swal(page, timeout=1200)         # popup na frente bloqueia os botões
    try:
        c = page.locator("m-autorizacao-pedido-confirmar button.btn-primary:has-text('Cancelar')")
        if c.count() and c.first.is_visible():
            c.first.click(timeout=4000)
            time.sleep(0.5)
    except Exception:
        pass
    try:
        d = page.locator("button[aria-label='Close dialog']:has-text('Cancelar')")
        if d.count() and d.first.is_visible():
            d.first.click(timeout=4000)
            time.sleep(0.5)
    except Exception:
        pass


def processar_pedido(page, indice, n_total, ignorar=None):
    """Abre o pedido de índice 'indice', lê saldo, preenche e (se live) confirma.
    Se o pedido estiver em 'ignorar' (chaves já puladas nesta execução), só fecha
    o modal e devolve {'ja_pulado': True} — sem clicar em Aceitar."""
    botoes = page.locator("button:has(em.editar)")
    if indice >= botoes.count():
        return None
    botoes.nth(indice).click(timeout=10000)

    # modal de detalhe
    page.wait_for_selector('input[placeholder="Saldo após aprovação do pedido"]',
                           state="visible", timeout=15000)
    saldo_txt = ler_saldo(page)           # espera o valor carregar
    saldo = parse_valor(saldo_txt)
    log(f"   • Pedido {indice + 1}/{n_total}: saldo após aprovação = {saldo_txt!r} -> {fmt(saldo)}")
    # dados que vão para a mensagem do WhatsApp (o modal já está aberto aqui)
    representante = _ler_campo_modal(page, "Representante")
    valor_txt = _ler_campo_modal(page, "Valor do pedido")
    valor = parse_valor(valor_txt)
    numero = _ler_numero_pedido(page)
    chave = numero or f"{representante}|{valor_txt}"   # fallback se o título mudar
    log(f"     pedido Nº {numero or '?'} | representante={representante!r} -> "
        f"{primeiro_nome(representante)!r} | valor do pedido={valor_txt!r} -> {fmt(valor)}")
    if ignorar and chave in ignorar:
        log("     já foi pulado nesta execução — fechando sem mexer.")
        fechar_modais(page)
        return {"ja_pulado": True, "chave": chave}
    try:
        page.screenshot(path=str(OUT / f"det_{indice + 1:02d}.png"))
    except Exception:
        pass

    # abre modal de confirmação
    page.locator("button.btn-success:has-text('Aceitar Pedido')").first.click(timeout=10000)
    page.wait_for_selector('input[placeholder="Observações de Autorização"]',
                           state="visible", timeout=15000)

    # preenche observação
    page.locator('input[placeholder="Observações de Autorização"]').fill(OBSERVACAO, timeout=8000)

    # decide e ajusta a opção "Debitar da conta corrente do RCA"
    desejado = (saldo is not None) and (saldo >= 0)
    # knob só de TESTE p/ validar o mecanismo do toggle (não usar em produção):
    _force = ENV.get("FORCE_TOGGLE", "").lower()
    if _force == "on":
        desejado = True
    elif _force == "off":
        desejado = False
    toggle = page.locator("m-autorizacao-pedido-confirmar mat-slide-toggle").first

    def _toggle_on():
        # estado REAL do Angular: mat-slide-toggle ganha a classe 'mat-checked' quando ligado
        try:
            return page.evaluate(
                "() => { const t = document.querySelector("
                "'m-autorizacao-pedido-confirmar mat-slide-toggle');"
                " return t ? t.classList.contains('mat-checked') : null; }")
        except Exception:
            return None

    if saldo is None:
        log("     ! não consegui ler o saldo — NÃO vou confirmar este pedido por segurança.")
    antes = _toggle_on()
    depois = antes
    inp = toggle.locator("input")
    # aciona até o estado real bater com o desejado (até 3 tentativas).
    # Preferimos teclado (Espaço no input focado): alterna limpo, sem o
    # resíduo de "arraste" que o clique deixa na bolinha.
    for _ in range(3):
        if depois == desejado:
            break
        try:
            inp.focus(timeout=3000)
            page.keyboard.press("Space")
        except Exception:
            try:
                alvo = toggle.locator("label")
                (alvo.first if alvo.count() else toggle).click(timeout=6000)
            except Exception:
                pass
        time.sleep(0.7)
        depois = _toggle_on()
    # remove o resíduo de transform inline na bolinha para o VISUAL refletir o
    # estado real (só cosmético; o valor enviado é o checked/mat-checked).
    try:
        page.evaluate(
            "() => { const t = document.querySelector("
            "'m-autorizacao-pedido-confirmar mat-slide-toggle');"
            " const tc = t && t.querySelector('.mat-slide-toggle-thumb-container');"
            " if (tc) tc.style.transform = ''; }")
    except Exception:
        pass
    estado = "LIGADA" if depois else "DESLIGADA"
    log(f"     observação='{OBSERVACAO}' | 'Debitar da conta corrente do RCA': "
        f"desejado={'ON' if desejado else 'OFF'} antes={antes} depois={depois} -> {estado}")
    if depois != desejado:
        log(f"     ! ATENÇÃO: toggle não ficou no estado desejado — pedido NÃO deve ser confirmado.")
    if ENV.get("DIAG_TOGGLE", "0") == "1":
        try:
            diag = page.evaluate("""() => {
              const ts = [...document.querySelectorAll('m-autorizacao-pedido-confirmar mat-slide-toggle')];
              return ts.map(t => ({
                cls: t.className,
                matChecked: t.classList.contains('mat-checked'),
                aria: t.getAttribute('aria-checked'),
                inputChecked: (t.querySelector('input')||{}).checked,
                thumbTransform: (t.querySelector('.mat-slide-toggle-thumb-container')||{}).style
                                ? getComputedStyle(t.querySelector('.mat-slide-toggle-thumb-container')).transform : '?',
                visible: !!(t.offsetWidth||t.offsetHeight),
              }));
            }""")
            log(f"     [DIAG_TOGGLE] qtd={len(diag)} -> {diag}")
        except Exception as e:
            log(f"     [DIAG_TOGGLE err] {e}")

    # screenshot do modal preenchido
    shot = OUT / f"pedido_{indice + 1:02d}.png"
    try:
        page.screenshot(path=str(shot))
        log(f"     print salvo: {shot.name}")
    except Exception:
        pass

    toggle_ok = (depois == desejado)
    resultado = {"indice": indice, "saldo": saldo, "opcao": desejado,
                 "representante": representante, "valor": valor,
                 "toggle_ok": toggle_ok, "confirmado": False,
                 "numero": numero, "chave": chave,
                 "motivo": None,      # por que NÃO foi confirmado (vai pro WhatsApp)
                 # não confirmado com estado CONHECIDO (recusa do sistema ou nem
                 # chegou a confirmar) -> o loop pode pular e seguir pro próximo
                 "pode_pular": False}

    if DRY:
        log(f"     [DRY-RUN] NÃO clicando em Confirmar. (pausa {DRY_PAUSE:.0f}s p/ conferência)")
        time.sleep(DRY_PAUSE)
        fechar_modais(page)
    else:
        if saldo is None or not toggle_ok:
            motivo = "saldo ilegível" if saldo is None else "toggle não confirmado no estado certo"
            log(f"     [LIVE] pulando confirmação por segurança ({motivo}).")
            resultado["motivo"] = motivo
            resultado["pode_pular"] = True       # nada foi confirmado
            fechar_modais(page)
        else:
            page.locator("m-autorizacao-pedido-confirmar button.btn-success:has-text('Confirmar')").first.click(timeout=10000)
            log("     [LIVE] cliquei em Confirmar — aguardando resposta do sistema...")
            # O maxGestão responde com um popup SweetAlert (sucesso/erro). Esse é
            # o sinal de aceite mais confiável — e precisa ser fechado, senão o
            # overlay bloqueia o 'Pesquisar' do próximo pedido.
            swal, swal_msg = _fechar_swal(page, timeout=15000)
            if swal == "error":
                log(f"     ! [LIVE] o sistema retornou ERRO ao confirmar — NÃO confirmado: {swal_msg!r}")
                resultado["motivo"] = swal_msg or "o sistema retornou erro ao confirmar"
                resultado["pode_pular"] = True   # recusa explícita do sistema
                fechar_modais(page)
            else:
                # sucesso (ou sem popup): confirma também pelo fechamento do modal
                try:
                    page.wait_for_selector('input[placeholder="Saldo após aprovação do pedido"]',
                                           state="hidden", timeout=15000)
                    resultado["confirmado"] = True
                    log(f"     [LIVE] CONFIRMADO (popup={swal or 'nenhum'}, modal fechou).")
                except Exception:
                    if swal == "success":
                        resultado["confirmado"] = True
                        log("     [LIVE] CONFIRMADO (popup de sucesso do sistema).")
                    else:
                        log(f"     ! [LIVE] sem confirmação clara — NÃO confirmado (popup={swal_msg!r}).")
                        resultado["motivo"] = swal_msg or "sem confirmação clara do sistema"
                    fechar_modais(page)
    return resultado


def _loop_live(mg, total):
    """LIVE: aprova do topo da lista e RE-PESQUISA após cada aprovação (a decisão
    de seguir vem de sinais de render — modal fechou, pedido saiu da lista —, não
    de 'sleep' fixo).

    Pedido não confirmado com estado conhecido (recusado pelo sistema, ex.: desconto
    acima do permitido, ou pulado antes de confirmar) CONTINUA na lista. Antes o
    loop parava nele e, se ele ficasse no topo, travava a fila. Agora ele é anotado
    pelo Nº e o loop segue: como sempre processamos do topo, os pulados ocupam as
    primeiras posições e o próximo candidato é o índice len(pulados). Se a ordem do
    grid mudar, o Nº evita repetir um pulado (no pior caso um pedido fica pro
    próximo @aprova — nunca é aprovado errado). Casos AMBÍGUOS (exceção, sem
    confirmação clara, modal que não fecha) continuam PARANDO."""
    resultados = []
    pulados = set()
    confirmados = 0
    restantes = total
    i = 0
    guard = 0
    while i < restantes and (MAX_PEDIDOS == 0 or confirmados < MAX_PEDIDOS):
        guard += 1
        if guard > 2 * total + 10:
            log("   ! limite de segurança de iterações atingido — parando.")
            break
        try:
            r = processar_pedido(mg, i, restantes, ignorar=pulados)
        except Exception as e:
            log(f"     ! erro ao processar pedido: {e}")
            fechar_modais(mg)
            break
        if not r:
            break
        if r.get("ja_pulado"):                # ordem mudou: passa pro seguinte
            if not _modal_fechado(mg):
                log("   ! modal não fechou — parando por segurança.")
                break
            i += 1
            continue
        resultados.append(r)
        if r["confirmado"]:
            confirmados += 1
            antes = restantes
            restantes = repesquisar(mg, antes=antes)
            if restantes >= antes:
                log(f"   ! pedido aprovado, mas a lista ainda mostra {restantes} após "
                    "re-pesquisar — parando por segurança.")
                break
            log(f"   lista atualizada: {restantes} pedido(s) restante(s).")
            i = len(pulados)
            continue
        if not r.get("pode_pular"):
            log("   ! pedido não confirmado em estado ambíguo — parando para não arriscar.")
            break
        if not _modal_fechado(mg):
            log("   ! pedido não confirmado e o modal não fechou — parando por segurança.")
            break
        pulados.add(r["chave"])
        log(f"   ↷ pedido Nº {r.get('numero') or '?'} NÃO aprovado — pulando e seguindo com o próximo.")
        restantes = repesquisar(mg, antes=restantes)
        i = len(pulados)
    if pulados:
        log(f"   {len(pulados)} pedido(s) pulado(s) ficaram pendentes: {sorted(pulados)}")
    return resultados


RESULT_JSON = LOGF / "ultimo_resultado.json"


def _escrever_resultado(resumo):
    """Grava o resumo da execução em JSON, pro bot (ou qualquer outro) ler."""
    try:
        RESULT_JSON.write_text(json.dumps(resumo, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        log(f"   (não consegui gravar {RESULT_JSON.name}: {e})")


def main():
    resumo = {
        "ok": False, "modo": MODO, "equipe": CODIGO,
        "total_encontrados": None, "processados": 0, "confirmados": 0,
        "pedidos": [], "erro": None,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "log": str(_logfile),
    }
    log("=" * 60)
    log(f"AUTOMAÇÃO LIBERAÇÃO DE PEDIDOS — MODO={MODO.upper()}  (equipe {CODIGO} / Adriana)")
    if DRY:
        log("DRY-RUN: nada será confirmado. Só leitura + preenchimento.")
    else:
        log("*** MODO LIVE: os pedidos SERÃO confirmados de verdade. ***")
    log("=" * 60)

    with sync_playwright() as pw:
        headed = ENV.get("HEADED", "1") == "1"
        browser = pw.chromium.launch(headless=not headed, slow_mo=(150 if headed else 0))
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="pt-BR",
            permissions=["geolocation", "notifications"],
            geolocation={"latitude": float(ENV.get("GEO_LAT", "-23.55052")),
                         "longitude": float(ENV.get("GEO_LNG", "-46.633308"))},
        )
        ctx.on("dialog", lambda d: d.accept())
        tabs = []
        ctx.on("page", lambda p: tabs.append(p))
        page = ctx.new_page()
        tabs.append(page)

        resultados = []
        try:
            # Setup (login -> SSO maxGestão -> menu -> filtro) é a parte instável:
            # o token SSO às vezes é rejeitado e a aba cai na tela de re-login.
            # Por isso tentamos algumas vezes antes de desistir.
            setup_tentativas = int(ENV.get("SETUP_RETRIES", "3"))
            mg = None
            for tentativa in range(1, setup_tentativas + 1):
                try:
                    if tentativa > 1:
                        log(f"   ↻ tentativa {tentativa}/{setup_tentativas} de login/SSO...")
                        for t in list(tabs):        # fecha abas extras da tentativa anterior
                            if t is not page:
                                try:
                                    t.close()
                                except Exception:
                                    pass
                        tabs[:] = [page]
                        time.sleep(6)
                    login_portal(page)
                    mg = abrir_maxgestao(page, ctx, tabs)
                    ir_para_autorizacao(mg)
                    filtrar(mg)
                    break                           # setup concluído
                except Exception as e:
                    log(f"   ! falha no login/SSO (tentativa {tentativa}/{setup_tentativas}): {e}")
                    if tentativa >= setup_tentativas:
                        raise

            total = contar_pedidos(mg)
            resumo["total_encontrados"] = total
            log(f"5) {total} pedido(s) encontrado(s) para a equipe {CODIGO} (Adriana).")
            if total == 0:
                log("   Nada a processar. Encerrando.")
            else:
                limite = total if MAX_PEDIDOS == 0 else min(total, MAX_PEDIDOS)
                if DRY:
                    for i in range(limite):
                        try:
                            r = processar_pedido(mg, i, total)
                            if r:
                                resultados.append(r)
                        except Exception as e:
                            log(f"     ! erro no pedido {i + 1}: {e}")
                            fechar_modais(mg)
                else:
                    resultados = _loop_live(mg, total)

            # resumo
            log("-" * 60)
            log("RESUMO:")
            for r in resultados:
                acao = "CONFIRMADO" if r["confirmado"] else ("[dry] não confirmado" if DRY else "não confirmado")
                op = "opção LIGADA" if r["opcao"] else "opção DESLIGADA"
                log(f"  pedido Nº {r.get('numero') or '?'}: {r.get('representante') or '?'} | "
                    f"valor {fmt(r.get('valor'))} | saldo {fmt(r['saldo'])} -> {op} -> {acao}")
            resumo["pedidos"] = [
                {"indice": r["indice"], "saldo": r["saldo"], "saldo_fmt": fmt(r["saldo"]),
                 "representante": r.get("representante"),
                 "primeiro_nome": primeiro_nome(r.get("representante")),
                 "valor": r.get("valor"), "valor_fmt": fmt(r.get("valor")),
                 "opcao_ligada": r["opcao"], "confirmado": r["confirmado"],
                 "numero": r.get("numero"), "motivo": r.get("motivo")}
                for r in resultados
            ]
            resumo["processados"] = len(resultados)
            resumo["confirmados"] = sum(1 for r in resultados if r["confirmado"])
            resumo["ok"] = True
            log(f"Total processado: {len(resultados)}")

            if headed:
                log(f"Mantendo o navegador aberto por {FINAL_HOLD:.0f}s para conferência...")
                time.sleep(FINAL_HOLD)
        except Exception as e:
            resumo["erro"] = str(e)
            log(f"ERRO GERAL: {e}")
            try:
                (mg if 'mg' in dir() else page).screenshot(path=str(OUT / "erro.png"))
            except Exception:
                pass
            if headed:
                time.sleep(min(FINAL_HOLD, 20))
            # NÃO re-levanta: registramos o erro no resumo p/ o bot poder reportar.
        finally:
            ctx.close()
            browser.close()
            _escrever_resultado(resumo)
            log(f"Fim. Log: {_logfile}")
    return resumo


if __name__ == "__main__":
    _r = main()
    sys.exit(0 if _r.get("ok") else 1)
