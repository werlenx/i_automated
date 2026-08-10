# Bot de aprovação via WhatsApp (Evolution API)

Quando a Adriana manda **`@aprova`** no WhatsApp (DM) para o número dedicado do
bot, todos os pedidos pendentes da equipe 182 são aprovados automaticamente
(cada um com o "Debitar da conta corrente do RCA" ligado/desligado conforme o
saldo), e o bot responde o resumo.

```
Adriana (WhatsApp)  --"@aprova"-->  Evolution API  --webhook-->  bot_whatsapp.py
                                                                      |
                                                          roda liberar_pedidos.py (live/headless)
                                                                      |
Adriana  <--- resumo (quantos aprovados, saldos) <--- Evolution <-----+
```

## Peças
- **liberar_pedidos.py** — a automação (já existente). Em `MODO=live` aprova e
  grava o resultado em `logs/ultimo_resultado.json`.
- **bot_whatsapp.py** — servidor web que recebe o webhook da Evolution, valida o
  remetente/comando e dispara a automação.
- **Evolution API** — gateway do WhatsApp (você já tem uma; crie uma **instância
  separada** só pra este bot, com um **número/chip dedicado**).

## ⚠️ Antes de tudo
- **Use um número dedicado** (chip só pra isso). A Evolution usa o protocolo
  não-oficial (Baileys) → o número **pode ser banido**. Nunca use o pessoal.
- **Máquina intermitente**: se ela estiver desligada quando a Adriana mandar
  `@aprova`, a mensagem se perde e nada é aprovado. Pra algo crítico, prefira um
  servidor sempre-ligado.
- **Rede**: a Evolution precisa **alcançar** o bot pela URL do webhook. Veja
  "Cenários de rede" abaixo.

## 1. Configurar o `.env`
Preencha a seção do bot no `.env`:
```
EVOLUTION_URL=http://IP-DA-EVOLUTION:8080     # onde sua Evolution responde
EVOLUTION_APIKEY=xxxxxxxx                       # a API key (global ou da instância)
EVOLUTION_INSTANCE=aprovacao-adriana            # instância NOVA, só deste bot
WA_AUTORIZADO=5594984171562                     # só a Adriana dispara
WA_COMANDO=@aprova
BOT_HOST=0.0.0.0
BOT_PORT=8080                                   # troque se conflitar com a Evolution
BOT_WEBHOOK_PATH=webhook
```

## 2. Criar a instância na Evolution e apontar o webhook
Mais fácil pelo **Evolution Manager** (a UI web da Evolution):
1. Crie uma instância nova com o nome de `EVOLUTION_INSTANCE`.
2. Leia o **QR code** com o celular do **número dedicado**.
3. Em *Webhook/Settings* da instância, configure:
   - **URL**: `http://IP-DA-MAQUINA-DO-BOT:8080/webhook`
   - **Eventos**: marque **MESSAGES_UPSERT**
   - Deixe "Webhook by events" **desligado**.

Ou por `curl` (ajuste ao seu Evolution; v2):
```bash
# criar instância
curl -X POST "$EVOLUTION_URL/instance/create" \
  -H "apikey: $EVOLUTION_APIKEY" -H "Content-Type: application/json" \
  -d '{"instanceName":"aprovacao-adriana","integration":"WHATSAPP-BAILEYS","qrcode":true}'

# apontar o webhook para o bot
curl -X POST "$EVOLUTION_URL/webhook/set/aprovacao-adriana" \
  -H "apikey: $EVOLUTION_APIKEY" -H "Content-Type: application/json" \
  -d '{"url":"http://IP-DA-MAQUINA-DO-BOT:8080/webhook","webhook_by_events":false,"events":["MESSAGES_UPSERT"]}'
```

## 3. Rodar o bot
Na máquina do bot (com o repo + `.venv` já prontos):
```bash
cd /home/ti-3/repo/AB_libera_pedidodo
./.venv/bin/python bot_whatsapp.py
```
Teste se está no ar: `curl http://localhost:8080/` → `bot-aprovacao ok`.

Para deixar rodando em segundo plano (simples): `nohup ./.venv/bin/python bot_whatsapp.py &`
(ou monte um serviço `systemd` — recomendado se a máquina reinicia.)

## 4. Testar
Do WhatsApp da Adriana, mande **`@aprova`** para o número do bot.
Esperado: o bot responde "🔄 Recebido..." e, ao terminar, o resumo com os
pedidos aprovados. Números diferentes do da Adriana são ignorados.

## Cenários de rede (a Evolution precisa alcançar o bot)
- **Tudo na mesma máquina local** (Evolution + bot juntos): use
  `EVOLUTION_URL=http://localhost:8080` e webhook `http://localhost:PORTA/webhook`.
  Cuidado com conflito de porta — rode o bot em outra porta (ex.: `BOT_PORT=8090`).
- **Evolution num servidor/VPS e bot em máquina local atrás de NAT**: o servidor
  **não alcança** sua máquina local diretamente. Opções:
  - rodar a **instância** deste bot numa Evolution **na mesma máquina local**, ou
  - expor o bot com um túnel (ex.: `cloudflared`/`ngrok`) e usar a URL pública no
    webhook, ou
  - rodar tudo (bot + Evolution) no servidor sempre-ligado.

## Segurança embutida
- Só o número em `WA_AUTORIZADO` dispara (compara tolerando o 9º dígito dos
  JIDs antigos do WhatsApp BR). Qualquer outro remetente é ignorado.
- Mensagens de **grupo** são ignoradas (só DM).
- Uma aprovação por vez (trava) e de-duplicação de webhooks repetidos.
- Todo comando/tentativa fica registrado em `logs/bot.log`.
