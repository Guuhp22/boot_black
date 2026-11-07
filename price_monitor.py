"""
Bot de monitoramento de preços - Mercado Livre e Amazon
Roda via GitHub Actions (ou qualquer agendador) e avisa no Telegram
quando o preço de um item cai.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

CONFIG_FILE = Path("config.json")
HISTORY_FILE = Path("price_history.json")
ML_TOKEN_FILE = Path("ml_token.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

ML_CLIENT_ID = os.environ.get("ML_CLIENT_ID")
ML_CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET")
ML_REFRESH_TOKEN_INICIAL = os.environ.get("ML_REFRESH_TOKEN")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def carregar_json(caminho: Path, padrao):
    if caminho.exists():
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    return padrao


def salvar_json(caminho: Path, dados):
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)


def obter_access_token_mercado_livre():
    """Troca o refresh token por um access token novo via API oficial do ML.

    O ML rotaciona o refresh token a cada uso: o novo valor é salvo em
    ml_token.json, que o workflow commita de volta no repositório (igual
    já faz com price_history.json), pra usar na próxima execução.
    """
    if not ML_CLIENT_ID or not ML_CLIENT_SECRET:
        return None

    token_salvo = carregar_json(ML_TOKEN_FILE, {})
    refresh_token = token_salvo.get("refresh_token") or ML_REFRESH_TOKEN_INICIAL

    if not refresh_token:
        return None

    resp = requests.post(
        "https://api.mercadolibre.com/oauth/token",
        headers={"accept": "application/json"},
        data={
            "grant_type": "refresh_token",
            "client_id": ML_CLIENT_ID,
            "client_secret": ML_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    dados = resp.json()

    novo_refresh_token = dados.get("refresh_token")
    if novo_refresh_token:
        salvar_json(ML_TOKEN_FILE, {"refresh_token": novo_refresh_token})

    return dados.get("access_token")


def preco_mercado_livre(item_id: str, access_token: str):
    """Busca o preço de um anúncio via API oficial do Mercado Livre (autenticada)."""
    url = f"https://api.mercadolibre.com/items/{item_id}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if not resp.ok:
        print(f"[ML][debug] status={resp.status_code} resposta={resp.text}")
    resp.raise_for_status()
    dados = resp.json()
    return dados.get("price"), dados.get("permalink")


def _extrair_preco_do_texto(texto: str):
    numero = re.sub(r"[^\d,\.]", "", texto)
    numero = numero.replace(".", "").replace(",", ".")
    try:
        return float(numero)
    except ValueError:
        return None


def preco_amazon(url: str):
    """Usa um navegador headless (Playwright) para acessar a Amazon e extrair o preço.

    A Amazon bloqueia requisições simples (tipo requests/urllib) vindas de IPs
    de datacenter com uma página de captcha. Um navegador de verdade, mesmo
    headless, tem mais chance de passar — mas não é garantido.
    """
    seletores = [
        "span.a-price span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#corePrice_feature_div span.a-offscreen",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="pt-BR",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        preco = None
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # dá um tempo pra página terminar de montar

            for seletor in seletores:
                el = page.query_selector(seletor)
                if el:
                    texto = el.inner_text().strip()
                    if texto:
                        preco = _extrair_preco_do_texto(texto)
                        if preco is not None:
                            break

            if preco is None:
                html = page.content()
                print(
                    f"[Amazon][debug] tamanho_html={len(html)} "
                    f"trecho={html[:300]!r}"
                )
        finally:
            browser.close()

    return preco


def enviar_telegram(mensagem: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram não configurado (faltam variáveis de ambiente). Mensagem:")
        print(mensagem)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensagem,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, data=payload, timeout=15)
    if not resp.ok:
        print(f"Falha ao enviar mensagem no Telegram: {resp.status_code} {resp.text}")


def main():
    config = carregar_json(CONFIG_FILE, {"items": []})
    historico = carregar_json(HISTORY_FILE, {})

    if not config.get("items"):
        print("Nenhum item configurado em config.json.")
        sys.exit(0)

    precisa_ml = any(item.get("mercado_livre_id") for item in config["items"])
    ml_access_token = None
    if precisa_ml:
        try:
            ml_access_token = obter_access_token_mercado_livre()
        except Exception as e:
            print(f"[ML] Erro ao obter access token: {e}")
        if not ml_access_token:
            print(
                "[ML] Sem access token válido (confira ML_CLIENT_ID, "
                "ML_CLIENT_SECRET e ML_REFRESH_TOKEN nos secrets)."
            )

    for item in config["items"]:
        nome = item.get("nome", "Item sem nome")
        chave = nome  # usa o nome como chave do histórico

        preco_atual = None
        link_exibicao = None

        if item.get("mercado_livre_id"):
            if ml_access_token:
                try:
                    preco_atual, link_exibicao = preco_mercado_livre(
                        item["mercado_livre_id"], ml_access_token
                    )
                except Exception as e:
                    print(f"[ML] Erro ao buscar preço de '{nome}': {e}")

        elif item.get("amazon_url"):
            try:
                preco_atual = preco_amazon(item["amazon_url"])
                link_exibicao = item["amazon_url"]
            except Exception as e:
                print(f"[Amazon] Erro ao buscar preço de '{nome}': {e}")

        if preco_atual is None:
            print(f"Não foi possível obter o preço de '{nome}'.")
            continue

        preco_anterior = historico.get(chave)

        print(f"{nome}: R$ {preco_atual:.2f} (anterior: {preco_anterior})")

        if preco_anterior is not None and preco_atual < preco_anterior:
            diferenca = preco_anterior - preco_atual
            mensagem = (
                f"📉 <b>{nome}</b> baixou de preço!\n"
                f"De R$ {preco_anterior:.2f} para R$ {preco_atual:.2f} "
                f"(-R$ {diferenca:.2f})\n{link_exibicao}"
            )
            enviar_telegram(mensagem)
        elif preco_anterior is None:
            print(f"Primeira checagem de '{nome}', preço salvo sem aviso.")

        historico[chave] = preco_atual

    salvar_json(HISTORY_FILE, historico)


if __name__ == "__main__":
    main()
