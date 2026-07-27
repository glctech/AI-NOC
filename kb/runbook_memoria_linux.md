# Runbook — Memória alta em servidores Linux

## Primeiro: é pressão real?
`free -h` — o que importa é **available**, não free. Linux usa RAM livre como cache; "memória cheia" com available alto é comportamento normal e o alerta pode ser falso positivo de threshold mal calibrado.

## Se available está baixo
1. Maiores consumidores: `ps aux --sort=-rss | head -15`.
2. OOM killer agiu? `dmesg -T | grep -i "out of memory"` — se sim, identificar a vítima e o culpado (nem sempre são o mesmo processo).
3. Swap: `vmstat 1 5` — si/so constantes indicam thrashing (degradação séria).
4. Crescimento contínuo de RSS de um processo ao longo de horas = suspeita de memory leak → coletar evidência (gráfico do Zabbix) e escalar para N2/dev.

## Ações seguras N1
Reiniciar serviço não crítico identificado como consumidor anômalo (com aprovação), limpar caches de aplicação documentados. NÃO usar `echo 3 > drop_caches` como "solução" — mascara o problema.
