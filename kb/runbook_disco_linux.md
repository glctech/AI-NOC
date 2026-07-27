# Runbook — Disco cheio em servidores Linux

## Diagnóstico rápido
1. `df -h` e `df -i` (espaço E inodes — inode cheio dá "No space left" com df mostrando espaço livre).
2. Onde cresceu: `du -xhd1 / 2>/dev/null | sort -rh | head`, descendo diretório a diretório (`/var`, `/var/log`, `/tmp`, `/home`).
3. Arquivos grandes recentes: `find / -xdev -size +500M -mtime -3 2>/dev/null`.
4. Espaço "fantasma": `lsof +L1 | head` — arquivo deletado ainda aberto por processo. Solução: reiniciar o processo dono (NUNCA kill -9 direto em banco de dados).

## Causas recorrentes
- Logs sem rotação (`/var/log`): configurar logrotate; nunca truncar log de aplicação viva sem `copytruncate`.
- Dumps de core, backups locais esquecidos, cache de pacotes (`apt clean` / `dnf clean all`).
- Journal do systemd: `journalctl --disk-usage` e `journalctl --vacuum-size=500M`.

## Encerramento
Uso < 85%, causa identificada e mitigada (rotação/limpeza/expansão), registrado no incidente quem tratou e o que foi feito.
