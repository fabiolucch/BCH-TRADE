"""
okx_demo_test.py — Script de teste para OKX Demo Trading (Paper Trading).

O que este script faz, em ordem:
  1. Autentica via HMAC-SHA256 com header x-simulated-trading: 1
  2. Consulta o saldo da conta demo
  3. Se não houver USDC nem USDT utilizáveis, credita 5000 USDT via endpoint demo
  4. Envia uma ordem de compra a mercado de teste no par BCH-USDT (tdMode=cash)

Não depende do ccxt — usa apenas `requests` para controle total dos headers.

Uso:
    pip install requests python-dotenv
    python okx_demo_test.py
"""

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────────────────────

load_dotenv()

API_KEY    = os.getenv("API_KEY", "")
SECRET_KEY = os.getenv("API_SECRET", "")
PASSPHRASE = os.getenv("API_PASSPHRASE", "")

BASE_URL   = "https://www.okx.com"      # mesmo domínio para demo e produção

# Header obrigatório para todas as requisições no ambiente demo
DEMO_HEADER = {"x-simulated-trading": "1"}

# Par e parâmetros da ordem de teste
TEST_SYMBOL  = "BCH-USDT"               # instId no formato OKX — mesmo par do bot
ORDER_SIDE   = "buy"
ORDER_TYPE   = "market"
TD_MODE      = "cash"                   # spot/cash no modo demo
ORDER_SIZE   = "0.01"                   # mínimo de BCH aceito pela OKX (≈ $5)

# Moedas aceitas pelo endpoint de ajuste de saldo demo
DEMO_SUPPORTED_CCY = {"BTC", "ETH", "USDT", "OKB"}
CREDIT_CCY  = "USDT"
CREDIT_AMT  = "5000"


# ═════════════════════════════════════════════════════════════
# AUTENTICAÇÃO — Assinatura HMAC-SHA256 + Base64
# ═════════════════════════════════════════════════════════════

def _timestamp() -> str:
    """Retorna timestamp ISO 8601 com milissegundos, conforme exige a OKX."""
    now = datetime.now(timezone.utc)
    ms  = f"{now.microsecond // 1000:03d}"
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + ms + "Z"


def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    """
    Gera a assinatura da OKX.

    Pre-hash: timestamp + METHOD + /path/completo + body_json
    Algoritmo: HMAC-SHA256 → Base64
    """
    message = timestamp + method.upper() + path + body
    mac     = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def _auth_headers(method: str, path: str, body: str = "") -> dict:
    """
    Monta o conjunto completo de headers de autenticação + header de demo.

    Headers obrigatórios OKX:
      OK-ACCESS-KEY         → API Key
      OK-ACCESS-SIGN        → Assinatura HMAC-SHA256 base64
      OK-ACCESS-TIMESTAMP   → Timestamp ISO 8601
      OK-ACCESS-PASSPHRASE  → Passphrase definida ao criar a API Key
      x-simulated-trading   → "1" para ambiente demo
    """
    ts = _timestamp()
    return {
        "Content-Type"        : "application/json",
        "OK-ACCESS-KEY"       : API_KEY,
        "OK-ACCESS-SIGN"      : _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP" : ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        **DEMO_HEADER,          # x-simulated-trading: 1
    }


def _validate_credentials() -> bool:
    """Valida que as credenciais foram preenchidas no .env."""
    missing = [k for k, v in [("API_KEY", API_KEY), ("API_SECRET", SECRET_KEY), ("API_PASSPHRASE", PASSPHRASE)] if not v]
    if missing:
        print(f"[ERRO] Variáveis não encontradas no .env: {', '.join(missing)}")
        print("       Verifique se o arquivo .env existe e está preenchido corretamente.")
        return False
    return True


# ═════════════════════════════════════════════════════════════
# CONSULTA DE SALDO
# ═════════════════════════════════════════════════════════════

def consultar_saldo() -> dict:
    """
    GET /api/v5/account/balance

    Retorna dicionário {moeda: saldo_disponível} para todas as moedas
    com saldo > 0 na conta demo.
    """
    path = "/api/v5/account/balance"
    url  = BASE_URL + path

    print("\n── Consultando saldo da conta demo ──────────────────────────")
    try:
        resp = requests.get(url, headers=_auth_headers("GET", path), timeout=10)
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        print(f"[ERRO DE REDE] {exc}")
        return {}

    # Erros de autenticação
    if data.get("code") == "50111":
        print("[ERRO DE AUTENTICAÇÃO] API Key inválida ou sem permissão.")
        print(f"   Detalhe: {data.get('msg')}")
        return {}
    if data.get("code") == "50112":
        print("[ERRO DE AUTENTICAÇÃO] Passphrase incorreta.")
        return {}
    if data.get("code") != "0":
        print(f"[ERRO API] code={data.get('code')} msg={data.get('msg')}")
        return {}

    saldos = {}
    details = data.get("data", [{}])[0].get("details", [])
    for item in details:
        moeda = item.get("ccy", "")
        livre = float(item.get("availBal", 0) or 0)
        if livre > 0:
            saldos[moeda] = livre

    if saldos:
        print("   Moedas com saldo disponível:")
        for moeda, valor in saldos.items():
            print(f"     {moeda}: {valor:.6f}")
    else:
        print("   Nenhum saldo disponível na conta demo.")

    return saldos


# ═════════════════════════════════════════════════════════════
# AJUSTE DE SALDO DEMO
# ═════════════════════════════════════════════════════════════

def creditar_saldo_demo(ccy: str = CREDIT_CCY, amt: str = CREDIT_AMT) -> bool:
    """
    POST /api/v5/account/demo-account-adjust-balance

    Credita saldo fictício na conta demo.
    Moedas aceitas pela OKX: BTC, ETH, USDT, OKB (USDC não é suportado).

    Retorna True se bem-sucedido.
    """
    if ccy not in DEMO_SUPPORTED_CCY:
        print(f"[AVISO] Moeda '{ccy}' não suportada pelo endpoint de ajuste demo.")
        print(f"        Use uma destas: {', '.join(sorted(DEMO_SUPPORTED_CCY))}")
        return False

    path    = "/api/v5/account/demo-account-adjust-balance"
    url     = BASE_URL + path
    payload = {
        "type"       : "increase",
        "adjustments": [{"ccy": ccy, "amt": amt}],
    }
    body = json.dumps(payload)

    print(f"\n── Creditando {amt} {ccy} na conta demo ─────────────────────")
    try:
        resp = requests.post(
            url,
            headers=_auth_headers("POST", path, body),
            data=body,
            timeout=10,
        )
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        print(f"[ERRO DE REDE] {exc}")
        return False

    if data.get("code") == "0":
        print(f"   Saldo creditado com sucesso: +{amt} {ccy}")
        return True
    else:
        print(f"[ERRO] Falha ao creditar saldo.")
        print(f"   code={data.get('code')} | msg={data.get('msg')}")
        # Detalha erros específicos
        for item in data.get("data", []):
            if item.get("sCode") != "0":
                print(f"   Detalhe: sCode={item.get('sCode')} sMsg={item.get('sMsg')}")
        return False


# ═════════════════════════════════════════════════════════════
# ENVIO DE ORDEM DE TESTE
# ═════════════════════════════════════════════════════════════

def enviar_ordem_teste(
    instId: str   = TEST_SYMBOL,
    side: str     = ORDER_SIDE,
    sz: str       = ORDER_SIZE,
) -> bool:
    """
    POST /api/v5/trade/order

    Envia ordem de compra a mercado no ambiente demo.
    tdMode='cash' → spot sem alavancagem.

    Retorna True se a ordem foi aceita pela exchange.
    """
    path    = "/api/v5/trade/order"
    url     = BASE_URL + path
    payload = {
        "instId" : instId,
        "tdMode" : TD_MODE,         # "cash" para spot
        "side"   : side,
        "ordType": ORDER_TYPE,      # "market"
        "sz"     : sz,              # quantidade em moeda base (BTC)
    }
    body = json.dumps(payload)

    print(f"\n── Enviando ordem de teste ──────────────────────────────────")
    print(f"   Par    : {instId}")
    print(f"   Lado   : {side.upper()}")
    print(f"   Tipo   : {ORDER_TYPE} | tdMode={TD_MODE}")
    print(f"   Qtd    : {sz} BCH")

    try:
        resp = requests.post(
            url,
            headers=_auth_headers("POST", path, body),
            data=body,
            timeout=10,
        )
        data = resp.json()
    except requests.exceptions.RequestException as exc:
        print(f"[ERRO DE REDE] {exc}")
        return False

    if data.get("code") == "0":
        ordem = data.get("data", [{}])[0]
        print(f"   ✓ Ordem enviada com sucesso!")
        print(f"   ordId  : {ordem.get('ordId')}")
        print(f"   clOrdId: {ordem.get('clOrdId')}")
        print(f"   sCode  : {ordem.get('sCode')} | sMsg: {ordem.get('sMsg', 'OK')}")
        return True
    else:
        print(f"[ERRO] Ordem rejeitada.")
        print(f"   code={data.get('code')} | msg={data.get('msg')}")
        for item in data.get("data", []):
            sc = item.get("sCode", "")
            sm = item.get("sMsg", "")
            # Traduz códigos de erro comuns
            descricao = {
                "51008": "Saldo insuficiente para esta ordem.",
                "51000": "Parâmetros da ordem inválidos.",
                "51001": "Instrumento (par) não encontrado ou inativo.",
                "51020": "Valor da ordem abaixo do mínimo exigido pela OKX.",
                "50102": "Timestamp fora do intervalo aceito (verifique o relógio do sistema).",
            }.get(sc, sm)
            print(f"   sCode={sc} → {descricao}")
        return False


# ═════════════════════════════════════════════════════════════
# FLUXO PRINCIPAL
# ═════════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     OKX Demo Trading — Script de Teste e Diagnóstico    ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # 1. Valida credenciais
    if not _validate_credentials():
        return

    # 2. Consulta saldo atual
    saldos = consultar_saldo()

    # 3. Verifica se há USDC ou USDT suficiente para operar
    saldo_usdc = saldos.get("USDC", 0.0)
    saldo_usdt = saldos.get("USDT", 0.0)
    saldo_util = saldo_usdc + saldo_usdt

    print(f"\n   Saldo utilizável: USDC={saldo_usdc:.2f} | USDT={saldo_usdt:.2f} | Total≈{saldo_util:.2f}")
    print(f"   Par de teste usa USDT — saldo de {saldo_usdt:.2f} USDT será usado.")

    # 4. Se saldo insuficiente (< 10 USDC/USDT), credita USDT no demo
    if saldo_util < 10.0:
        print("\n   Saldo insuficiente. Solicitando crédito de saldo demo em USDT...")
        print("   (OKX Demo não aceita crédito direto de USDC — usando USDT como substituto)")
        ok = creditar_saldo_demo(ccy=CREDIT_CCY, amt=CREDIT_AMT)
        if not ok:
            print("\n[ABORTANDO] Não foi possível creditar saldo demo. Verifique as permissões da API Key.")
            return
        # Revalida saldo após crédito
        saldos     = consultar_saldo()
        saldo_usdt = saldos.get("USDT", 0.0)
        if saldo_usdt < 10.0:
            print("\n[ABORTANDO] Saldo ainda insuficiente após tentativa de crédito.")
            return
    else:
        print("\n   Saldo suficiente. Pulando etapa de crédito demo.")

    # 5. Envia ordem de teste
    enviar_ordem_teste()

    print("\n── Diagnóstico concluído ────────────────────────────────────")
    print("   Verifique as respostas acima para confirmar que tudo está funcionando.")
    print("   Quando estiver OK, rode o bot principal com: python main.py")


if __name__ == "__main__":
    main()
