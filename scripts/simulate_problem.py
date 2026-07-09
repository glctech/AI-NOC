#!/usr/bin/env python3
"""Simula problems no laboratório enviando valores aos itens trapper.

Implementa o protocolo do zabbix_sender em Python puro (sem dependências),
falando direto com o Zabbix server/proxy na porta 10051.

Exemplos:
  # dispara "CPU alta" no lab-web-01
  python3 simulate_problem.py --scenario cpu --host lab-web-01

  # dispara "host down" (ICMP=0)
  python3 simulate_problem.py --scenario down --host lab-net-01

  # resolve tudo (valores normais)
  python3 simulate_problem.py --scenario recover --host lab-web-01
"""
import argparse
import json
import socket
import struct
import sys

SCENARIOS = {
    "cpu":     [("cpu.util", "97.5")],
    "disk":    [("vfs.fs.pused", "92.0")],
    "down":    [("icmp.ping", "0")],
    "recover": [("cpu.util", "12.0"), ("vfs.fs.pused", "40.0"), ("icmp.ping", "1")],
}

HEADER = b"ZBXD\x01"


def zabbix_send(server: str, port: int, host: str,
                values: list[tuple[str, str]]) -> dict:
    payload = json.dumps({
        "request": "sender data",
        "data": [{"host": host, "key": k, "value": v} for k, v in values],
    }).encode()
    packet = HEADER + struct.pack("<Q", len(payload)) + payload

    with socket.create_connection((server, port), timeout=10) as sock:
        sock.sendall(packet)
        header = _recv_exact(sock, 13)
        if header[:5] != HEADER:
            raise RuntimeError("Resposta com header invalido do Zabbix server")
        (length,) = struct.unpack("<Q", header[5:13])
        body = _recv_exact(sock, length)
    return json.loads(body)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("Conexao encerrada pelo Zabbix server")
        buf += chunk
    return buf


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--server", default="127.0.0.1", help="Zabbix server/proxy")
    p.add_argument("--port", type=int, default=10051)
    p.add_argument("--host", default="lab-web-01", help="host tecnico no Zabbix")
    p.add_argument("--scenario", choices=SCENARIOS, default="cpu")
    args = p.parse_args()

    values = SCENARIOS[args.scenario]
    print(f"[i] Enviando cenario '{args.scenario}' para {args.host} "
          f"via {args.server}:{args.port}")
    for k, v in values:
        print(f"    {k} = {v}")

    result = zabbix_send(args.server, args.port, args.host, values)
    info = result.get("info", "")
    print(f"[i] Resposta: {result.get('response')} | {info}")
    if "failed: 0" not in info:
        sys.exit("[AVISO] Alguns valores foram rejeitados. "
                 "Rodou o seed_zabbix.py? O nome do host confere?")
    if args.scenario != "recover":
        print("[OK] Aguarde alguns segundos: a trigger deve abrir um Problem "
              "e o AI NOC Analyst deve publicar o ACK [NOC AI] no evento.")
    else:
        print("[OK] Valores normais enviados; problems devem resolver.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as exc:
        sys.exit(f"[ERRO] {exc}")
