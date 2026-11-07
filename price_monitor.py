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

CONFIG_FILE = Path("config.json")
HISTORY_FILE = Path("price_history.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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


def preco_mercado_livre(url: str):
    """Faz scraping da página do produto no Mercado Livre para extrair o preço.

    A API oficial (api.mercadolibre.com/items/...) passou a exigir um
    access token mesmo para itens públicos, então extraímos o preço
    direto do HTML da página, igual é feito para a Amazon.
    """
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Tenta primeiro via dado estruturado (JSON-LD), que é mais confiável
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

    # Fallback: preço visível na página (parte inteira do valor)
    fracao = soup.select_one(".andes-money-amount__fraction")
    if fracao:
        texto = fracao.text.strip().replace(".", "")
        try:
            return float(texto)
        except ValueError:
            pass

    print(
        f"[ML][debug] status={resp.status_code} tamanho_html={len(resp.text)} "
        f"trecho={resp.text[:300]!r}"
    )
    return None


def preco_amazon(url: str):
    """Faz scraping leve da página do produto na Amazon para extrair o preço."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    seletores = [
        "span.a-price span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#corePrice_feature_div span.a-offscreen",
    ]

    for seletor in seletores:
        el = soup.select_one(seletor)
        if el and el.text.strip():
            texto = el.text.strip()
            numero = re.sub(r"[^\d,\.]", "", texto)
            numero = numero.replace(".", "").replace(",", ".")
            try:
                return float(numero)
            except ValueError:
                continue

    print(
        f"[Amazon][debug] status={resp.status_code} tamanho_html={len(resp.text)} "
        f"trecho={resp.text[:300]!r}"
    )
    return None


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
