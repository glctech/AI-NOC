# Runbook — Host down / perda de conectividade

## Isolar o problema em camadas
1. É só este host ou o segmento todo? Verificar no Zabbix outros hosts do mesmo switch/gateway/site. Vários hosts juntos = problema de rede/energia, não do host.
2. Ping de DUAS origens diferentes (Zabbix server + outra máquina/VPN). `mtr -rwc 30 <IP>` mostra o salto onde morre.
3. Console fora de banda (iLO/iDRAC/IPMI/console da VM): host respondendo no console mas sem rede = problema de interface/cabo/switch; console morto = host realmente caído.
4. ARP no gateway: entrada presente e incompleta indica L2 ok / L3 com problema.

## Antes de escalar
Confirmar que não há janela de manutenção ou mudança recente (firewall, VLAN, patch). Registrar horário exato da queda (último dado no Zabbix) — essencial para correlacionar com mudanças.

## Encerramento
Conectividade estável por 10 min, causa registrada, responsável pela tratativa identificado no incidente.
