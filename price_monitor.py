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
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CONFIG_FILE = Path("config.json")
HISTORY_FILE = Path("price_history.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BRIGHTDATA_API_KEY = os.environ.get("BRIGHTDATA_API_KEY")
BRIGHTDATA_ZONE = os.environ.get("BRIGHTDATA_ZONE")

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


def _extrair_preco_do_texto(texto: str):
    numero = re.sub(r"[^\d,\.]", "", texto)
    numero = numero.replace(".", "").replace(",", ".")
    try:
        return float(numero)
    except ValueError:
        return None


def _extrair_preco_do_html(html: str):
    """Procura o preço em um HTML do Mercado Livre (JSON-LD ou texto visível)."""
    soup = BeautifulSoup(html, "lxml")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            dados = json.loads(script.string)
        except (TypeError, ValueError):
            continue
        candidatos = dados if isinstance(dados, list) else [dados]
        for candidato in candidatos:
            oferta = candidato.get("offers") if isinstance(candidato, dict) else None
            if isinstance(oferta, dict) and oferta.get("price"):
                try:
                    return float(oferta["price"])
                except (TypeError, ValueError):
                    pass

    fracao = soup.select_one(".andes-money-amount__fraction")
    if fracao:
        preco = _extrair_preco_do_texto(fracao.text.strip())
        if preco is not None:
            return preco

    return None


def preco_mercado_livre(url: str):
    """Busca o preço no Mercado Livre via Bright Data Web Unlocker.

    O Mercado Livre bloqueia requisições simples e até navegador headless
    comum vindos de IP de datacenter. O Web Unlocker é um serviço (com
    cota gratuita mensal) que contorna esse bloqueio e devolve o HTML
    real da página.
    """
    if not BRIGHTDATA_API_KEY or not BRIGHTDATA_ZONE:
        print(
            "[ML] BRIGHTDATA_API_KEY ou BRIGHTDATA_ZONE não configurados nos secrets."
        )
        return None

    resp = requests.post(
        "https://api.brightdata.com/request",
        headers={
            "Authorization": f"Bearer {BRIGHTDATA_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"zone": BRIGHTDATA_ZONE, "url": url, "format": "raw"},
        timeout=30,
    )
    resp.raise_for_status()

    preco = _extrair_preco_do_html(resp.text)
    if preco is None:
        print(
            f"[ML][debug] tamanho_html={len(resp.text)} "
            f"trecho={resp.text[:300]!r}"
        )
    return preco


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

    linhas_mensagem = []

    for item in config["items"]:
        nome = item.get("nome", "Item sem nome")
        chave = nome  # usa o nome como chave do histórico

        preco_atual = None
        link_exibicao = None

        if item.get("mercado_livre_url"):
            try:
                preco_atual = preco_mercado_livre(item["mercado_livre_url"])
                link_exibicao = item["mercado_livre_url"]
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
            linhas_mensagem.append(
                f"⚠️ <b>{nome}</b>: não consegui checar o preço agora."
            )
            continue

        preco_anterior = historico.get(chave)
        print(f"{nome}: R$ {preco_atual:.2f} (anterior: {preco_anterior})")

        if preco_anterior is None:
            linha = f"🆕 <b>{nome}</b>: R$ {preco_atual:.2f} (primeira checagem)"
        elif preco_atual < preco_anterior:
            diferenca = preco_anterior - preco_atual
            linha = (
                f"📉 <b>{nome}</b>: R$ {preco_atual:.2f} "
                f"(caiu de R$ {preco_anterior:.2f}, -R$ {diferenca:.2f})"
            )
        elif preco_atual > preco_anterior:
            diferenca = preco_atual - preco_anterior
            linha = (
                f"📈 <b>{nome}</b>: R$ {preco_atual:.2f} "
                f"(subiu de R$ {preco_anterior:.2f}, +R$ {diferenca:.2f})"
            )
        else:
            linha = f"➡️ <b>{nome}</b>: R$ {preco_atual:.2f} (sem mudança)"

        if link_exibicao:
            linha += f"\n{link_exibicao}"

        linhas_mensagem.append(linha)
        historico[chave] = preco_atual

    salvar_json(HISTORY_FILE, historico)

    if linhas_mensagem:
        mensagem = "\n\n".join(linhas_mensagem)
        enviar_telegram(mensagem)


if __name__ == "__main__":
    main()
