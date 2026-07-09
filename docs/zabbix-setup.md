# Configuração do Zabbix 7.4.x para o AI NOC Analyst

## 1. Criar o API Token

1. `Users → API tokens → Create API token`
2. Usuário: crie um usuário de serviço `ainoc` com permissão de **leitura** nos
   host groups monitorados e permissão de **acknowledge** de problems.
3. Copie o token para `AINOC_ZABBIX_TOKEN` no `.env`.

## 2. Criar o Media Type (Webhook)

`Alerts → Media types → Create media type`

- **Name:** AI NOC Analyst
- **Type:** Webhook
- **Parameters:**

| Nome        | Valor                  |
|-------------|------------------------|
| url         | `http://SEU_HOST:8000/webhook/zabbix` |
| secret      | mesmo valor de `AINOC_WEBHOOK_SHARED_SECRET` |
| eventid     | `{EVENT.ID}`           |
| event_name  | `{EVENT.NAME}`         |
| severity    | `{EVENT.SEVERITY}`     |
| hostname    | `{HOST.NAME}`          |

- **Script:**

```javascript
var params = JSON.parse(value);

var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('X-AINOC-Secret: ' + params.secret);

var body = JSON.stringify({
    eventid: params.eventid,
    event_name: params.event_name,
    severity: params.severity,
    hostname: params.hostname
});

var resp = req.post(params.url, body);
if (req.getStatus() !== 202) {
    throw 'AI NOC Analyst retornou HTTP ' + req.getStatus() + ': ' + resp;
}
return 'OK';
```

## 3. Criar usuário de mídia e Action

1. Adicione a mídia "AI NOC Analyst" ao usuário `ainoc`
   (Send to: qualquer valor, ex. `-`).
2. `Alerts → Actions → Trigger actions → Create action`:
   - **Conditions:** ajuste conforme escopo (ex.: severity >= Warning).
   - **Operations:** Send message → to user `ainoc` → via AI NOC Analyst.

## 4. Testar

```bash
curl -X POST http://SEU_HOST:8000/webhook/zabbix \
  -H 'Content-Type: application/json' \
  -H 'X-AINOC-Secret: SEU_SEGREDO' \
  -d '{"eventid": "12345"}'
```

Em seguida verifique o problem no frontend: deve exibir um acknowledge
`[NOC AI] Primeira análise automática concluída.` com resumo, causa provável,
confiança e próximas ações.
