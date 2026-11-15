# Steam Deals Discord Bot

Bot do Discord que monitora promoções da Steam e avisa em **um único canal**:

- **Ofertas especiais** da loja (jogos base com desconto, preços em BRL);
- jogos da **wishlist** dos usuários que vincularam a conta Steam (wishlist pública);
- jogos adicionados **manualmente** com `/wishlist_add`.

Cada promoção gera **uma única notificação por jogo e por estado de preço**, mesmo que vários usuários ou
várias origens (ofertas especiais, wishlist, lista manual) contenham o jogo. Os usuários interessados são
mencionados na mesma mensagem.

## Requisitos

- Python 3.12 ou superior (desenvolvido e testado em Python 3.14; `discord.py` 2.7 importa e roda normalmente).
- Um bot criado no Discord Developer Portal.
- Acesso HTTPS à Steam (nenhuma chave de API da Steam é necessária).

## Instalação no Windows

```powershell
cd steam-promo
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
# para rodar os testes:
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env
```

### Criar o bot no Developer Portal

1. Acesse <https://discord.com/developers/applications> e clique em **New Application**.
2. Na aba **Bot**, crie o bot e copie o token (**Reset Token**). Cole em `DISCORD_BOT_TOKEN` no `.env`.
   Nunca compartilhe nem versione esse token.
3. **Não é necessário** ativar nenhuma *Privileged Gateway Intent* (sem *Message Content Intent*).
4. Em **OAuth2 > URL Generator**, marque os escopos `bot` e `applications.commands`.
5. Permissões mínimas do bot no canal de notificações: **View Channel**, **Send Messages** e **Embed Links**.
6. Abra a URL gerada e adicione o bot ao servidor.
7. Ative o modo desenvolvedor no Discord, copie o ID do canal de notificações (`NOTIFY_CHANNEL_ID`) e,
   opcionalmente, o ID do servidor (`GUILD_ID`).

## Execução

```powershell
.venv\Scripts\python bot.py
```

No primeiro ciclo o bot registra todas as ofertas especiais existentes como *baseline* **sem notificar**
(veja "Baseline" abaixo). Os comandos slash são sincronizados na inicialização; com `GUILD_ID` definido a
sincronização é imediata naquele servidor, sem ele os comandos globais podem levar até cerca de uma hora para
aparecer.

## Configuração (`.env`)

| Variável | Padrão | Descrição |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | (obrigatória) | Token do bot. Nunca é registrado nos logs (mascarado). |
| `NOTIFY_CHANNEL_ID` | (obrigatória) | Canal onde as promoções são publicadas. |
| `GUILD_ID` | vazio | Servidor para sincronização rápida dos comandos. |
| `CHECK_INTERVAL_MINUTES` | `30` | Intervalo entre ciclos de verificação. |
| `MAX_CONCURRENCY` | `5` | Requisições simultâneas à Steam. |
| `MAX_NOTIFICATIONS_PER_CYCLE` | `20` | Máximo de notificações enviadas por ciclo (o excedente fica na fila). |
| `INCLUDE_NON_GAMES` | `false` | `false` = só jogos base (`category1=998`); `true` inclui DLC, software etc. |
| `DATABASE_PATH` | `data/steamdeals.db` | Arquivo SQLite. |
| `STEAM_COUNTRY` | `br` | País da loja (preços). |
| `STEAM_LANGUAGE` | `brazilian` | Idioma da API da loja (a busca usa `portuguese`, mapeado automaticamente). |
| `LOG_LEVEL` | `INFO` | Nível de log. |

Erros de configuração (variável ausente, número inválido etc.) são listados de uma só vez na inicialização.

## Comandos

Todos os comandos funcionam apenas em servidores, e as respostas são **efêmeras** (só quem executou vê).
Somente as notificações de promoção são públicas. Cada usuário só altera os próprios dados.

| Comando | O que faz |
| --- | --- |
| `/steam_status` | Última verificação por fonte, fila pendente, próximo ciclo e quantidade de jogos monitorados. Detalhes internos de erro aparecem apenas para quem tem a permissão **Gerenciar Servidor**. |
| `/steam_vincular steam_id` | Vincula sua conta (SteamID64 de 17 dígitos ou URL `steamcommunity.com/profiles/<id>`) e importa a wishlist (respeitando o intervalo mínimo de importação; se estiver em intervalo, o ciclo seguinte importa). URLs personalizadas (`/id/nome`) não são suportadas. |
| `/steam_desvincular` | Remove o vínculo e os jogos vindos da wishlist; os adicionados manualmente permanecem. |
| `/wishlist` | Lista seus jogos monitorados com o último preço conhecido (até 25 itens + contagem). |
| `/wishlist_add appid` | Adiciona um jogo manualmente (autocompletar por nome ou AppID numérico; validado na Steam). Limite de 100 jogos manuais por usuário; AppID até 2^31-1. |
| `/wishlist_remover appid` | Remove um jogo da lista manual (autocompletar sobre os seus jogos manuais). |
| `/wishlist_importar` | Reimporta agora a wishlist vinculada (intervalo mínimo de 120 s por usuário). |
| `/steam_ofertas [quantidade]` | Maiores descontos registrados no banco (1 a 15, padrão 10). |

## Arquitetura

```
bot.py                      ponto de entrada
steamdeals/
  config.py                 Settings a partir do .env
  logging_setup.py          logging + máscara do token
  app.py                    montagem dos componentes (Services)
  models.py                 DealSnapshot, assinatura (hash) de promoção
  steam/                    http.py (retry/backoff/429), specials.py, store.py, wishlist.py, suggest.py
  db/                       database.py (aiosqlite, WAL, FKs), migrations.py, repositories.py
  monitor/service.py        ciclo: fontes -> detecção -> fila
  notify/                   dispatcher.py (fila persistente), embeds.py
  discord_bot/              client.py (loop, sync, envio), commands.py (slash commands)
scripts/smoke_steam.py      validação manual contra a Steam real (sem Discord)
tests/                      pytest (sem rede)
```

### Ciclo de monitoramento

1. **Specials**: pagina `store.steampowered.com/search/results` (`specials=1`, 100 por página, pausa de 3 s
   entre páginas; um ciclo completo leva cerca de 4 minutos). Falha da fonte é registrada em `check_state` e o ciclo continua com as demais.
2. **Wishlists**: um `GetWishlist` por SteamID distinto (concorrência limitada); sincroniza as origens
   `wishlist` (remove o que saiu, preserva as `manual`).
3. **Preços**: o conjunto deduplicado de AppIDs monitorados (união de todos os usuários) é consultado em lotes de
   100 no `IStoreBrowseService/GetItems` (cada AppID uma vez por ciclo; fallback para `appdetails` se um lote falhar).
4. **Detecção**: a *assinatura* é `hash(preço final, preço original, % desconto, término)` e só existe para itens
   realmente em desconto. Assinatura nova para um AppID enfileira uma notificação (`UNIQUE(appid, assinatura)`).
   Preços inconsistentes (final maior que o original, desconto 0, sem preço) não geram notificação.
5. **Fila**: uma notificação por jogo/estado. Origens e usuários a mencionar são mesclados enquanto ela estiver
   pendente. Quando uma assinatura mais nova é enfileirada, a pendente antiga vira `superseded` (continua no banco,
   registrada, nunca apagada em silêncio).
6. **Envio**: até `MAX_NOTIFICATIONS_PER_CYCLE` por ciclo, das mais antigas para as mais novas; o excedente continua
   `pending`. Um `asyncio.Lock` impede ciclos simultâneos (o segundo disparo é pulado e logado).

### Baseline

Na primeira execução em que a fonte `specials` é lida com sucesso (flag `specials_baseline_done` em `settings`), todas
as ofertas existentes são gravadas em `deal_state` com `baseline=1` e **sem notificação**, para não inundar o canal.
Jogos que tenham origem wishlist/manual **não** são suprimidos e notificam normalmente. Depois do baseline, jogos novos
nas ofertas ou com assinatura alterada notificam. Se a leitura de `specials` falhar, o baseline **não** é marcado como
concluído e é refeito no ciclo seguinte. Se um jogo do baseline passar a ser monitorado por alguém, ele é anunciado
uma vez (nunca tinha sido anunciado).

Observação: a busca de ofertas não informa a data de término. Para jogos apenas nas ofertas especiais a assinatura usa
o término desconhecido; para evitar duplicatas, o término já conhecido é reaproveitado quando o preço é idêntico, e
uma promoção idêntica que volte depois de ter terminado é anunciada de novo (contador interno `sale_epoch`).

**Comportamentos de detecção a conhecer**

- **Mudança de `discount_end` reanuncia.** O término faz parte da assinatura: se a Steam prorrogar ou alterar a data de
  uma promoção (mesmo com o mesmo preço) de um jogo monitorado, isso conta como novo estado e gera nova notificação.
- **Reversão A -> B -> A**: se o preço volta a um estado já notificado, a linha existente (`UNIQUE(appid, assinatura)`)
  é reativada como `pending` e reanunciada, e a pendente intermediária (B) vira `superseded`.
- **Fonte `specials` inválida não apaga estado.** Lista vazia, ou com menos de 90% do `total_count`, ou que desabe para
  menos de 50% do que já era conhecido (com 20+ itens), é tratada como falha da fonte: nada é limpo e o baseline não é
  marcado. Um jogo que some da listagem só deixa de estar em oferta depois de **2 ciclos consecutivos** de ausência
  (`deal_state.missing_cycles`); sumir por um ciclo (deslocamento de página) e voltar não reanuncia. O mesmo vale para
  um jogo que deixou de ser monitorado (também zerado ao remover a origem em `game_sources`).
- **`/wishlist_add` e jogo já anunciado**: se o jogo já está em promoção com o mesmo estado que foi anunciado antes, o
  usuário que o adiciona depois **não** gera nova notificação nem é mencionado; ele é mencionado nas promoções seguintes
  (ou na pendente ainda não enviada, pois usuários e origens são mesclados por jogo).
- Trocar o SteamID64 com `/steam_vincular` descarta as origens `wishlist` da conta antiga (as manuais ficam). Um
  resultado de wishlist obtido para um vínculo que mudou ou foi removido durante a requisição é descartado.

### Entrega "at-least-once" e estados da fila

Uma notificação só é marcada `sent` **depois** que o envio ao Discord retorna sucesso (e o `message_id` é gravado).
Falhas (`HTTPException`, 429, `Forbidden`, canal inexistente) guardam `last_error` e a mantêm `pending` para o próximo
ciclo, sem derrubar o ciclo; erros de canal (403/404/429, canal indisponível) interrompem o lote em vez de insistir e
**não** consomem tentativas. Outros erros incrementam `attempts`; ao chegar a **5 tentativas** a linha vira `failed`
(mensagem "venenosa": permanece registrada, sai da seleção, e `/steam_status` mostra a contagem). Notificações
pendentes sobrevivem a reinicializações.

Estados na tabela `notifications`: `pending`, `sent`, `superseded` (substituída por um estado mais novo do mesmo jogo),
`expired` (o jogo deixou de estar em promoção antes do envio; revalidado contra `deal_state` antes de enviar) e
`failed`. Nenhuma é apagada em silêncio, mas **nem toda notificação enfileirada chega ao canal**: as `superseded`,
`expired` e `failed` ficam só registradas no banco. Se a mesma promoção voltar (por exemplo após uma leitura transitória
sem preço), a linha `expired` é reativada.

**Risco:** se o processo cair (ou a gravação falhar) exatamente entre o `send()` bem-sucedido e o
`UPDATE ... sent`, a mesma notificação será reenviada após a reinicialização. Pode haver duplicata; uma notificação
nunca some sem registro, mas as garantias acima (superseded/expired/failed) significam que não há entrega garantida de
toda promoção observada.

## Limitações reais das APIs da Steam

- Os endpoints usados **não são documentados oficialmente** e podem mudar sem aviso (busca, `GetItems`, `GetWishlist`,
  `search/suggest`, `appdetails`).
- A busca de ofertas devolve **HTML dentro de JSON**: o parser é frágil por natureza. Uma página sem linhas com
  `total_count > 0` gera `SteamSchemaError` (a fonte falha de forma visível, sem notificar dados errados).
- **Wishlist privada e wishlist vazia são indistinguíveis** (`{"response":{}}` nos dois casos). O bot explica isso ao
  usuário e, nesse caso, **não apaga** os jogos já importados (use `/steam_desvincular` para limpar).
- O **término da oferta** só vem do `GetItems`; a busca de ofertas não o informa.
- `GetItems`: lote de **100 AppIDs** adotado. Em teste, 500 retornou HTTP 400, 200 funcionou e POST retorna 405.
- **Rate limits não documentados**: durante a validação a busca paginada recebeu HTTP 429 sem cabeçalho
  `Retry-After`. O cliente faz backoff exponencial com jitter, respeita `Retry-After` quando existe e pausa 3 s
  entre páginas (com 1,6 s/página o 429 apareceu após ~26 páginas; com 3 s as 67 páginas passaram sem 429). Se uma
  página continuar limitada, o cliente pausa 60 s e tenta de novo (2 vezes) antes de falhar a fonte, que é refeita
  no ciclo seguinte. `count` acima de 100 é ignorado pela Steam.
- **URLs personalizadas (vanity, `/id/nome`) não são suportadas** (resolver exigiria chave de API).
- `category1=998` reduz a busca a jogos base: na validação real o total caiu de 9906 para 6592 itens e a amostra
  de 100 itens passou a conter apenas `type=0` (sem DLC, que apareciam como `type=4` sem o filtro).
- A ordenação padrão da busca (relevância) é **instável durante a paginação**: em um ciclo completo houve 210 linhas
  duplicadas e cerca de 210 jogos ausentes, o que geraria falsos "novos" no ciclo seguinte (137 notificações
  indevidas na validação). Por isso a busca usa `sort_by=Name_ASC`: 6570 jogos únicos, 0 duplicatas.
- Jogos que entram em promoção como pacote/bundle (`data-ds-bundleid`/`packageid`) são ignorados.

## Testes

```powershell
.venv\Scripts\python -m pytest -q
```

Os testes não usam rede: o HTTP é substituído por fakes injetados e as respostas vêm de amostras reais em
`tests/fixtures` (busca, `GetItems`, wishlist pública/privada, sugestões).

Validação manual contra a Steam real (somente leitura, sem Discord):

```powershell
.venv\Scripts\python scripts\smoke_steam.py [SteamID64]
```

### O que foi verificado

| Item | Testado automaticamente | Validado com chamadas reais (Steam) | NÃO validado (Discord) |
| --- | --- | --- | --- |
| Parse da busca de ofertas, bundles, HTML alterado | Sim (fixture real) | Sim (1 página, `category1=998`) | n/a |
| `GetItems`, itens sem preço, lotes, fallback | Sim | Sim (lote de 100; 500 = HTTP 400; POST = 405) | n/a |
| Wishlist pública/privada, SteamID | Sim | Sim (wishlist pública de teste) | n/a |
| Sugestões/autocomplete (parser, cache) | Sim | Sim (`portal`) | Autocomplete real no cliente Discord |
| Retry, backoff, `Retry-After`, timeout | Sim | 429 observado na busca paginada | n/a |
| Banco: migrações, FKs, UNIQUE, reinício | Sim | n/a | n/a |
| Detecção, dedupe, baseline, fila, superseded | Sim | Ciclo completo com Steam real e envio simulado | n/a |
| Comandos slash e permissões | Sim (objetos `Interaction` falsos) | n/a | **Não validado** em um servidor Discord real |
| Envio de embed/menções, `allowed_mentions` | Sim (canal falso) | n/a | **Não validado** no Discord real |
| Sincronização de comandos, loop, login do bot | Construção do bot testada | n/a | **Não validado** (requer token e servidor) |

Os testes com Discord usam objetos falsos: o comportamento real da API do Discord (permissões, limites de rate,
renderização dos embeds e menções) ainda precisa ser conferido manualmente em um servidor de teste.
