# Runbook — CPU alta no ambiente de laboratório

## Contexto
Hosts do grupo AINOC Lab (lab-web-01, lab-db-01, lab-net-01) recebem carga
sintética via zabbix_sender. Picos bimodais de CPU (97,5% ↔ 12%) neste
ambiente são gerados pelo simulador `ainoc simulate cpu`.

## Procedimento N1
1. Verificar se o pico coincide com execução do simulador (perguntar à equipe
   ou checar histórico de comandos no servidor Zabbix).
2. Em host real: identificar processo com `top -b -n 1` e `ps aux --sort=-%cpu`.
3. Verificar crons: `crontab -l` e `/etc/cron.d/`.

## Encerramento
CPU abaixo de 90% por 15 minutos. Se a origem foi o simulador, encerrar como
teste controlado, sem escalonamento.
