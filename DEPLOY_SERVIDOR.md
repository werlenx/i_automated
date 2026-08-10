# Subir o bot no servidor (Docker) — sem encostar no que já roda lá

Guia passo a passo. Cada etapa tem **o que rodar** e **o que esperar de saída**.
Se a saída divergir, pare e me mostre — não siga adiante "no chute".

---

## Regras de ouro (o que NUNCA rodar nesse servidor)

Estes comandos derrubam/apagam coisa dos outros. Nenhuma etapa deste guia precisa deles:

| ❌ Não rodar | Por quê |
|---|---|
| `docker system prune -a` / `docker volume prune` | apaga imagens e **volumes** de todo mundo (sessão do WhatsApp, bancos) |
| `docker stop $(docker ps -q)` | para **todos** os containers do servidor |
| `docker compose down -v` | o `-v` destrói os volumes do projeto |
| `docker compose down` fora da pasta deste projeto | derruba a stack de outro projeto |
| editar o `docker-compose.yml` da Evolution | não é necessário: a gente só **se conecta** na rede dela |

O que a gente usa é sempre `docker compose ... ` **de dentro da pasta deste projeto**.
O `name: aprova-bot` no compose isola o projeto: os comandos só alcançam o
container `aprova_bot`.

---

## Como a Evolution funciona (e por que dá pra reusar a que já existe)

Uma Evolution API é um servidor **multi-instância**:

```
Evolution API (1 container, porta 8080)
├── instância "vendas"      → número A  → webhook A   ← já existe, não tocamos
├── instância "suporte"     → número B  → webhook B   ← já existe, não tocamos
└── instância "aprovacao-adriana" → chip do bot → webhook do nosso container  ← a nossa
```

Cada instância tem sessão, número, webhook e eventos **próprios**. Criar uma nova
instância é uma chamada de API — não reinicia o serviço, não desconecta as outras,
não pede QR de ninguém.

**O que você ganha reusando:** nada de Postgres/Redis duplicado, menos RAM, menos
coisa pra manter.

**O que você precisa saber antes de reusar:**
- A **API key global** dá acesso a *todas* as instâncias. Quem tiver a nossa chave
  mexe nas outras. Dá pra reduzir isso usando a apikey própria da instância
  (a Evolution v2 devolve uma no `instance/create`).
- Se alguém reiniciar/atualizar a Evolution, **todas** as instâncias caem juntas,
  a nossa inclusive. Volta sozinho, mas fica fora do ar nesse intervalo.
- Precisa ser **Evolution v2.x**. O código usa endpoints v2
  (`/message/sendText/{instance}`, `/instance/connect/{instance}`). Em v1 os
  caminhos são outros e o bot não fala com ela.

---

## Etapa 1 — Diagnóstico (só leitura, não muda nada)

Rode no servidor e me mande a saída inteira:

```bash
# o que está rodando hoje (foto do "antes")
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

# a Evolution é container? qual versão?
docker ps --format '{{.Names}}\t{{.Image}}' | grep -i evolution

# em qual(is) rede(s) docker ela está
docker inspect $(docker ps --format '{{.Names}}' | grep -i evolution | head -1) \
  -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}'

# a porta 8090 está livre no host?
ss -ltnp | grep -E ':8090|:8080' || echo "8090 livre"

# recursos (o Chromium quer ~1GB livre na hora que roda)
free -h; df -h /var/lib/docker; nproc

# versão do docker + se sobe sozinho no boot
docker --version; docker compose version
systemctl is-enabled docker
```

Confirme também a versão da Evolution pela própria API (troque a URL/chave):

```bash
curl -s http://localhost:8080/ -H "apikey: SUA_APIKEY"
```
> Espera-se um JSON com `"version": "2.x.x"`.

---

## Etapa 2 — Copiar o projeto pro servidor

Da **sua máquina atual** (esta aqui):

```bash
cd /home/ti-3/repo
rsync -av --exclude .venv --exclude __pycache__ --exclude logs --exclude capturas \
      AB_libera_pedidodo/ USUARIO@IP-DO-SERVIDOR:~/aprova-bot/
```

> O `.env` **vai** nessa cópia (ele tem as credenciais do maxGestão). É o
> comportamento desejado — só garanta que o destino é o servidor certo e que a
> permissão fica fechada: `chmod 600 ~/aprova-bot/.env` no servidor.

No servidor, prepare as pastas de saída com o dono certo (o container roda como
uid 1000, não-root):

```bash
cd ~/aprova-bot
mkdir -p logs capturas
sudo chown -R 1000:1000 logs capturas    # se o seu usuário já é uid 1000, nem precisa
```

---

## Etapa 3 — Ajustar o `.env` no servidor

Edite `~/aprova-bot/.env` e confirme/adicione:

```ini
# --- como o BOT acha a Evolution (container -> container, pelo nome) ---
EVOLUTION_URL=http://NOME_DO_CONTAINER_EVOLUTION:8080
EVOLUTION_APIKEY=<a apikey da Evolution do servidor>
EVOLUTION_INSTANCE=aprovacao-adriana

# --- rede docker JÁ EXISTENTE da Evolution (saída da Etapa 1) ---
EVOLUTION_NETWORK=<ex.: evolution_default>

# --- porta de debug no loopback do servidor; troque se 8090 estiver ocupada ---
BOT_HOST_PORT=8090

# --- inalterados ---
WA_AUTORIZADO=5594984171562,5591985055247
WA_COMANDO=@aprova
BOT_WEBHOOK_PATH=webhook
MODO=live
```

`BOT_HOST`, `BOT_PORT`, `BOT_PYTHON` e `HEADED` **não precisam** estar certos no
`.env`: o `docker-compose.yml` sobrescreve os quatro com os valores do container.

---

## Etapa 4 — Build e subida

```bash
cd ~/aprova-bot
docker compose build            # ~3-6 min na 1ª vez (baixa o Chromium)
docker compose up -d
docker compose ps               # espere STATUS = Up (healthy)
docker compose logs -f          # Ctrl-C sai do log, o container continua
```

Esperado no log:
```
BOT DE APROVAÇÃO (WhatsApp via Evolution)
  Evolution: http://...:8080  instância=aprovacao-adriana
  Webhook: http://0.0.0.0:8090/webhook
```

Teste local e **confirme que o resto do servidor não se mexeu**:

```bash
curl http://127.0.0.1:8090/          # -> bot-aprovacao ok
docker ps --format 'table {{.Names}}\t{{.Status}}'   # compare com a foto da Etapa 1
```

Confirme que o bot enxerga a Evolution pela rede interna:

```bash
docker compose exec aprova-bot python -c \
 "import urllib.request as u;print(u.urlopen('http://NOME_DO_CONTAINER_EVOLUTION:8080/',timeout=5).read()[:200])"
```

---

## Etapa 5 — Criar a instância e apontar o webhook

Rode **de dentro do container do bot** (assim o `apikey`/URL usados são os mesmos
que ele usa):

```bash
cd ~/aprova-bot
source .env      # só pra ter as variáveis no shell

# 1) criar a instância NOVA (não encosta nas existentes)
docker compose exec aprova-bot python - <<'PY'
import os, json, requests
url = os.environ["EVOLUTION_URL"].rstrip("/")
h = {"apikey": os.environ["EVOLUTION_APIKEY"], "Content-Type": "application/json"}
r = requests.post(f"{url}/instance/create", headers=h, json={
    "instanceName": os.environ["EVOLUTION_INSTANCE"],
    "integration": "WHATSAPP-BAILEYS",
    "qrcode": True,
})
print(r.status_code, json.dumps(r.json(), indent=2, ensure_ascii=False)[:1200])
PY

# 2) apontar o webhook para o NOSSO container (nome do serviço na rede docker)
docker compose exec aprova-bot python - <<'PY'
import os, json, requests
url = os.environ["EVOLUTION_URL"].rstrip("/")
inst = os.environ["EVOLUTION_INSTANCE"]
h = {"apikey": os.environ["EVOLUTION_APIKEY"], "Content-Type": "application/json"}
body = {"webhook": {"enabled": True,
                    "url": "http://aprova_bot:8090/webhook",
                    "byEvents": False, "base64": False,
                    "events": ["MESSAGES_UPSERT"]}}
r = requests.post(f"{url}/webhook/set/{inst}", headers=h, json=body)
print(r.status_code, r.text[:600])
PY
```

> Se o `webhook/set` responder 400, sua Evolution usa o formato antigo (campos na
> raiz: `{"url": ..., "webhook_by_events": false, "events": [...]}`). Me mande o
> erro que eu ajusto — ou faça pelo **Evolution Manager** (`http://IP:8080/manager`),
> aba *Webhook* da instância nova, marcando só `MESSAGES_UPSERT`.

**Confira que as outras instâncias continuam intactas:**
```bash
docker compose exec aprova-bot python -c \
 "import os,requests;print([i.get('name') or i.get('instanceName') for i in requests.get(os.environ['EVOLUTION_URL']+'/instance/fetchInstances',headers={'apikey':os.environ['EVOLUTION_APIKEY']}).json()])"
```

---

## Etapa 6 — Conectar o WhatsApp do bot (QR)

O bot tem uma página que renova o QR sozinho. Do seu computador, faça um túnel SSH
(não precisa abrir porta nenhuma no servidor):

```bash
ssh -L 8090:127.0.0.1:8090 USUARIO@IP-DO-SERVIDOR
```

E abra `http://localhost:8090/qr` no navegador. Leia o QR com o **chip dedicado do
bot** (WhatsApp → Aparelhos conectados → Conectar um aparelho). A página vira
"✅ Conectado!" sozinha.

> ⚠️ A sessão do WhatsApp mora na Evolution do servidor — é uma sessão **nova**,
> independente da que está conectada na sua máquina atual. Por isso o QR.
> Um mesmo número em duas Evolutions ao mesmo tempo briga e derruba a sessão:
> faça a Etapa 8 antes de mandar o primeiro `@aprova`.

---

## Etapa 7 — Testar ponta a ponta

Do WhatsApp da Adriana (ou do número de teste `5591985055247`), mande `@aprova`
para o número do bot.

Acompanhe no servidor:
```bash
docker compose logs -f
```

Esperado: `comando autorizado recebido de ...` → o Chromium roda headless
(~1-2 min) → `resumo enviado: ok=True confirmados=N` → a resposta chega no WhatsApp.

Se der erro no meio, os screenshots ficam em `~/aprova-bot/capturas/` (`erro.png`
é o mais útil) e o log da automação em `~/aprova-bot/logs/run_*.log`.

---

## Etapa 8 — Desligar o bot antigo (nesta máquina)

Com o do servidor funcionando, **não deixe os dois no ar**: os dois receberiam o
webhook e aprovariam duas vezes.

```bash
sudo systemctl disable --now aprova-bot          # para e tira do boot
docker compose -f docker-compose.evolution.yml down   # (sem -v! preserva os volumes)
```

---

## Operação do dia a dia

| Ação | Comando (dentro de `~/aprova-bot`) |
|---|---|
| Ver logs ao vivo | `docker compose logs -f` |
| Últimas 200 linhas | `docker compose logs --tail 200` |
| Reiniciar o bot | `docker compose restart` |
| Parar / subir | `docker compose stop` / `docker compose start` |
| Publicar mudança de código | `docker compose up -d --build` |
| Estado | `docker compose ps` |
| Último resultado | `cat logs/ultimo_resultado.json` |

O `restart: unless-stopped` + `systemctl is-enabled docker` fazem o bot voltar
sozinho depois de reboot ou queda. Se o servidor for mesmo intermitente, saiba que
**mensagem recebida com o servidor desligado se perde** — o WhatsApp entrega, mas
não há ninguém pra processar; a Adriana precisaria mandar `@aprova` de novo.

---

## Plano B — se a Evolution NÃO estiver em Docker (ou não puder compartilhar rede)

1. **Evolution roda direto no host (systemd/pm2):**
   no `docker-compose.yml`, troque o bloco `networks` por
   ```yaml
       extra_hosts:
         - "host.docker.internal:host-gateway"
   ```
   (dentro do serviço, e remova a seção `networks:` do arquivo);
   `EVOLUTION_URL=http://host.docker.internal:8080` e webhook
   `http://172.17.0.1:8090/webhook` — mas aí o bot precisa publicar em
   `0.0.0.0:8090` em vez de `127.0.0.1`, então feche a 8090 no firewall
   (`sudo ufw deny 8090`).

2. **Você prefere uma Evolution só sua:** use o `docker-compose.evolution.yml`
   deste repo, mudando a porta publicada de `8080` para uma livre (ex.: `8081`)
   pra não conflitar com a que já existe. Custo: +2 containers (Postgres/Redis) e
   ~400MB de RAM.
