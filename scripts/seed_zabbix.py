#!/usr/bin/env python3
"""Seed do laboratório Zabbix para o AI NOC Analyst.

Cria (idempotente — pode rodar várias vezes):
  - host group "AINOC Lab"
  - 3 hosts sintéticos com itens trapper (cpu.util, vfs.fs.pused, icmp.ping)
  - triggers de CPU alta, disco cheio e host down
  - um API token para o serviço (impresso no final)

Uso:
  python3 seed_zabbix.py --url http://localhost/api_jsonrpc.php \
      --user Admin --password zabbix

Requer apenas a biblioteca padrão do Python.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

HOSTS = ["lab-web-01", "lab-db-01", "lab-net-01"]
GROUP = "AINOC Lab"

ITEMS = [
    # (nome, key, value_type)  -> type=2 (Zabbix trapper)
    ("CPU utilization", "cpu.util", 0),
    ("Disk used %", "vfs.fs.pused", 0),
    ("ICMP ping", "icmp.ping", 3),
]

TRIGGERS = [
    ("CPU alta em {host}", "last(/{host}/cpu.util)>90", 4),
    ("Disco quase cheio em {host}", "last(/{host}/vfs.fs.pused)>85", 3),
    ("Host {host} indisponivel (ICMP)", "last(/{host}/icmp.ping)=0", 5),
]


class Api:
    def __init__(self, url):
        self.url = url
        self.auth = None
        self._id = 0

    def call(self, method, params):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        headers = {"Content-Type": "application/json-rpc"}
        if self.auth and method not in ("user.login", "apiinfo.version"):
            headers["Authorization"] = "Bearer " + self.auth
        req = urllib.request.Request(
            self.url, json.dumps(payload).encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            raise SystemExit(f"[ERRO] {method}: {data['error']}")
        return data["result"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://localhost/api_jsonrpc.php")
    p.add_argument("--user", default="Admin")
    p.add_argument("--password", default="zabbix")
    p.add_argument("--token-only", action="store_true",
                   help="apenas cria/verifica o API token do servico, "
                        "SEM criar hosts/triggers de laboratorio "
                        "(recomendado em producao)")
    p.add_argument("--rotate-token", action="store_true",
                   help="gera novo valor para o token existente "
                        "(INVALIDA o valor anterior usado pelo servico)")
    args = p.parse_args()

    api = Api(args.url)
    version = api.call("apiinfo.version", [])
    print(f"[i] Zabbix API {version} em {args.url}")

    api.auth = api.call("user.login", {"username": args.user, "password": args.password})
    print("[i] Autenticado.")

    if args.token_only:
        print("[i] Modo --token-only: pulando criacao de hosts de laboratorio.")
        _ensure_token(api, args)
        return

    groups = api.call("hostgroup.get", {"filter": {"name": [GROUP]}})
    groupid = groups[0]["groupid"] if groups else api.call(
        "hostgroup.create", {"name": GROUP})["groupids"][0]
    print(f"[+] Host group '{GROUP}' (id {groupid})")

    for hostname in HOSTS:
        existing = api.call("host.get", {"filter": {"host": [hostname]}})
        if existing:
            hostid = existing[0]["hostid"]
            print(f"[=] Host {hostname} ja existe (id {hostid})")
        else:
            hostid = api.call("host.create", {
                "host": hostname,
                "groups": [{"groupid": groupid}],
                "tags": [{"tag": "env", "value": "lab"},
                         {"tag": "managed_by", "value": "ainoc"}],
            })["hostids"][0]
            print(f"[+] Host {hostname} criado (id {hostid})")

        current = {i["key_"] for i in api.call(
            "item.get", {"hostids": [hostid], "output": ["key_"]})}
        for name, key, value_type in ITEMS:
            if key in current:
                continue
            api.call("item.create", {
                "hostid": hostid, "name": name, "key_": key,
                "type": 2, "value_type": value_type,
            })
            print(f"    [+] Item {key}")

        current_trg = {t["description"] for t in api.call(
            "trigger.get", {"hostids": [hostid], "output": ["description"]})}
        for desc_tpl, expr_tpl, priority in TRIGGERS:
            desc = desc_tpl.format(host=hostname)
            if desc in current_trg:
                continue
            api.call("trigger.create", {
                "description": desc,
                "expression": expr_tpl.format(host=hostname),
                "priority": priority,
                "tags": [{"tag": "scope", "value": "lab"}],
            })
            print(f"    [+] Trigger '{desc}'")

    _ensure_token(api, args)


def _ensure_token(api, args):
    userid = api.call("user.get", {"filter": {"username": [args.user]}})[0]["userid"]
    tokens = api.call("token.get", {"filter": {"name": ["ainoc-service"]}})
    token_value = None
    if tokens and not args.rotate_token:
        print("[=] Token 'ainoc-service' ja existe -- valor atual PRESERVADO.")
        print("    (o serviço continua funcionando com o token do .env;")
        print("     para gerar um novo valor use: ainoc seed --rotate-token)")
    else:
        if tokens:
            print("[!] --rotate-token: gerando NOVO valor "
                  "(o anterior sera INVALIDADO; atualize o .env!)")
            tokenid = tokens[0]["tokenid"]
        else:
            tokenid = api.call("token.create", {
                "name": "ainoc-service", "userid": userid})["tokenids"][0]
        token_value = api.call("token.generate", [tokenid])[0]["token"]

    print("\n" + "=" * 56)
    if args.token_only:
        print("  Token verificado (modo producao).")
    else:
        print("  Laboratorio pronto!")
        print(f"  Hosts: {', '.join(HOSTS)}")
    if token_value:
        print("  Copie para o .env do servico:")
        print(f"  AINOC_ZABBIX_TOKEN={token_value}")
    print("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        sys.exit(f"[ERRO] Nao foi possivel conectar a API do Zabbix: {exc}")
