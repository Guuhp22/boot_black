# Monitor de preços - Mercado Livre e Amazon

Bot gratuito que roda na nuvem (GitHub Actions) e avisa no Telegram quando
o preço de um item cai.

## Como configurar

1. Crie um repositório novo no GitHub e suba estes arquivos (mantendo a
   pasta `.github/workflows/`).
2. Crie um bot no Telegram:
   - Fale com **@BotFather**, use `/newbot` e guarde o token gerado.
   - Mande uma mensagem qualquer para o seu bot recém-criado.
   - Pegue seu `chat_id` acessando no navegador:
     `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates`
     (o número em `"chat":{"id": ...}` é o seu chat_id)
3. No repositório do GitHub, vá em **Settings > Secrets and variables >
   Actions** e crie dois secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. Edite o `config.json` com os itens que quer monitorar:
   - Para Mercado Livre: use o ID do anúncio (aparece na URL do produto,
     algo como `MLB123456789`).
   - Para Amazon: use a URL completa do produto (a página `/dp/...`).
5. Confirme que o Actions está habilitado no repositório (aba "Actions").
6. Pronto — o workflow roda sozinho a cada 6 horas. Para testar na hora,
   vá em **Actions > Monitor de Preços > Run workflow**.

## Observações

- O scraping da Amazon pode parar de funcionar se eles mudarem o HTML da
  página ou bloquearem o IP do GitHub Actions. Se isso acontecer, o erro
  aparece no log da execução (aba Actions).
- O histórico de preços fica salvo em `price_history.json`, que o próprio
  workflow atualiza e commita automaticamente — não precisa mexer nele.
- Para monitorar mais itens, basta adicionar mais entradas no array
  `items` do `config.json`.
- O intervalo de 6 horas pode ser ajustado editando a linha `cron` no
  arquivo `.github/workflows/price-monitor.yml`.
