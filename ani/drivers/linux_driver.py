import json
import subprocess
from pathlib import Path
from typing import Any

from .base import BaseDriver


# Prefissi ammessi per comandi di configurazione e validazione generici
ALLOWED_PREFIXES = [
    "ip addr add",
    "ip addr del",
    "ip addr show",
    "ip -6 addr show",
    "ip link set",
    "ip link show",
    "ip route add",
    "ip route del",
    "ip route show",
    "ip -6 route show",
    "ip rule add",
    "ip rule del",
    "ip rule show",
    "ip -6 rule show",
    "sysctl -w net.ipv4.ip_forward=",
    "sysctl -w net.ipv6.conf.all.forwarding=",
    "sysctl net.ipv4.ip_forward",
    "sysctl net.ipv6.conf.all.forwarding",
    "sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding",
    "cat /proc/sys/net/ipv4/ip_forward",
    "cat /proc/sys/net/ipv6/conf/all/forwarding",
    "echo 1 > /proc/sys/net/ipv4/ip_forward",
    "echo 0 > /proc/sys/net/ipv4/ip_forward",
    "echo 1 > /proc/sys/net/ipv6/conf/all/forwarding",
    "echo 0 > /proc/sys/net/ipv6/conf/all/forwarding",
    "ping ",
    "ping6 ",
    "traceroute ",
    "tracepath ",
    "ss ",
    "netstat ",
    "arp ",
    "ip neigh",
    "ip -br addr",
    "ip -br link",
    "hostname",
    "uname ",
    "vtysh -c 'show running-config'",
    "vtysh -c \"show running-config\"",
    "vtysh -c 'show version'",
    "vtysh -c \"show version\"",
    "nft list ruleset",
    "iptables-save",
]

# Comandi o sottostringhe pericolose da bloccare
BLOCKED_SUBSTRINGS = [
    "rm ",
    "reboot",
    "shutdown",
    "poweroff",
    "mkfs",
    "iptables -F",
    "nft flush",
    "> /etc/",
    ">> /etc/",
    "chmod ",
    "chown ",
    "apt ",
    "yum ",
    "dnf ",
    "apk ",
    "curl ",
    "wget ",
    "nc -e",
    "bash -c",
]

# Token di chaining shell che vogliamo proibire
FORBIDDEN_TOKENS = ["&&", "||", ";"]

# Comandi read-only ammessi per get_running_config e validation
READ_ONLY_PREFIXES = [
    "ip addr show",
    "ip -6 addr show",
    "ip link show",
    "ip route show",
    "ip -6 route show",
    "ip rule show",
    "ip -6 rule show",
    "sysctl net.ipv4.ip_forward",
    "sysctl net.ipv6.conf.all.forwarding",
    "sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding",
    "cat /proc/sys/net/ipv4/ip_forward",
    "cat /proc/sys/net/ipv6/conf/all/forwarding",
    "hostname",
    "uname ",
    "vtysh -c 'show running-config'",
    "vtysh -c \"show running-config\"",
    "vtysh -c 'show version'",
    "vtysh -c \"show version\"",
    "nft list ruleset",
    "iptables-save",
    "ss ",
    "netstat ",
    "arp ",
    "ip neigh",
    "ip -br addr",
    "ip -br link",
]


# Driver per nodi Linux-like eseguiti dentro container Containerlab
class LinuxDriver(BaseDriver):
    # Inizializza il driver salvando il nome del lab e il file di log dei comandi
    def __init__(self, lab_name: str, log_file: Path):
        self.lab_name = lab_name
        self.log_file = log_file

    # Costruisce il nome reale del container Containerlab per un dato nodo
    def _clab(self, node: str) -> str:
        return f"clab-{self.lab_name}-{node}"

    # Verifica se il container del nodo esiste ed è visibile via docker ps
    def _container_exists(self, container_name: str) -> bool:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return container_name in result.stdout.splitlines()

    # Esegue un comando shell nel container e restituisce stdout/stderr combinati
    def _exec(self, node: str, command: str, check: bool = True, timeout: int = 10) -> str:
        container_name = self._clab(node)

        if not self._container_exists(container_name):
            raise RuntimeError(f"Container not found: {container_name}")

        result = subprocess.run(
            ["docker", "exec", container_name, "sh", "-c", command],
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stderr and not stdout:
            return stderr
        if stdout and stderr:
            return f"{stdout}\n{stderr}"
        return stdout

    # Esegue un comando e prova a deserializzare stdout come JSON
    def _exec_json(self, node: str, command: str, timeout: int = 10, default: Any = None) -> Any:
        out = self._exec(node, command, check=False, timeout=timeout).strip()

        if not out:
            return default

        try:
            return json.loads(out)
        except Exception:
            return default

    # Controlla se un comando generico rientra nella allowlist di sicurezza
    def _is_allowed_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        if any(tok in cmd for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            if "-c" not in cmd:
                return False

        return any(cmd.startswith(prefix) for prefix in ALLOWED_PREFIXES)

    # Controlla se un comando è read-only e quindi sicuro per inspection/validation
    def _is_readonly_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        if any(tok in cmd for tok in FORBIDDEN_TOKENS):
            return False

        if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            return "-c" in cmd

        return any(cmd.startswith(prefix) for prefix in READ_ONLY_PREFIXES)

    # Esegue un comando solo se è esplicitamente classificato come read-only
    def _exec_readonly(self, node: str, command: str, timeout: int = 10) -> str:
        if not self._is_readonly_command(command):
            raise RuntimeError(f"Read-only command not allowed: {command}")
        return self._exec(node, command, check=False, timeout=timeout)

    # Converte un blocco sysctl in un dizionario chiave->valore
    def _parse_sysctl_output(self, text: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                result[key.strip()] = value.strip()
        return result

    # Restituisce solo gli indirizzi IPv4 globali utili da ripristinare
    def _extract_ipv4_addresses(self, addr_json: list[dict[str, Any]]) -> list[dict[str, str]]:
        results = []
        for iface in addr_json or []:
            ifname = iface.get("ifname")
            if not ifname or ifname == "lo":
                continue
            for info in iface.get("addr_info", []):
                if info.get("family") != "inet":
                    continue
                local = info.get("local")
                prefixlen = info.get("prefixlen")
                scope = info.get("scope")
                if not local or prefixlen is None:
                    continue
                if scope == "host":
                    continue
                results.append({
                    "ifname": ifname,
                    "cidr": f"{local}/{prefixlen}",
                })
        return results

    # Restituisce solo gli indirizzi IPv6 globali utili da ripristinare
    def _extract_ipv6_addresses(self, addr_json: list[dict[str, Any]]) -> list[dict[str, str]]:
        results = []
        for iface in addr_json or []:
            ifname = iface.get("ifname")
            if not ifname or ifname == "lo":
                continue
            for info in iface.get("addr_info", []):
                if info.get("family") != "inet6":
                    continue
                local = info.get("local")
                prefixlen = info.get("prefixlen")
                scope = info.get("scope")
                if not local or prefixlen is None:
                    continue
                # Non tocchiamo le link-local
                if scope in {"host", "link"}:
                    continue
                results.append({
                    "ifname": ifname,
                    "cidr": f"{local}/{prefixlen}",
                })
        return results

    # Filtra le route IPv4 non-kernel che vogliamo ripristinare esplicitamente
    def _extract_ipv4_routes(self, route_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for route in route_json or []:
            proto = route.get("protocol")
            if proto == "kernel":
                continue
            results.append(route)
        return results

    # Filtra le route IPv6 non-kernel che vogliamo ripristinare esplicitamente
    def _extract_ipv6_routes(self, route_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for route in route_json or []:
            proto = route.get("protocol")
            if proto == "kernel":
                continue
            results.append(route)
        return results

    # Filtra le rule IPv4 non di default che vogliamo ripristinare esplicitamente
    def _extract_ipv4_rules(self, rule_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for rule in rule_json or []:
            priority = rule.get("priority")
            if priority in {0, 32766, 32767}:
                continue
            results.append(rule)
        return results

    # Filtra le rule IPv6 non di default che vogliamo ripristinare esplicitamente
    def _extract_ipv6_rules(self, rule_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for rule in rule_json or []:
            priority = rule.get("priority")
            if priority in {0, 32766, 32767}:
                continue
            results.append(rule)
        return results

    # Converte una route IPv4 JSON in un comando ip route add
    def _ipv4_route_to_command(self, route: dict[str, Any]) -> str | None:
        dst = route.get("dst", "default")
        parts = ["ip", "route", "add", dst]

        gateway = route.get("gateway")
        dev = route.get("dev")
        metric = route.get("metric")

        if gateway:
            parts += ["via", gateway]
        if dev:
            parts += ["dev", dev]
        if metric is not None:
            parts += ["metric", str(metric)]

        return " ".join(parts)

    # Converte una route IPv6 JSON in un comando ip -6 route add
    def _ipv6_route_to_command(self, route: dict[str, Any]) -> str | None:
        dst = route.get("dst", "default")
        parts = ["ip", "-6", "route", "add", dst]

        gateway = route.get("gateway")
        dev = route.get("dev")
        metric = route.get("metric")

        if gateway:
            parts += ["via", gateway]
        if dev:
            parts += ["dev", dev]
        if metric is not None:
            parts += ["metric", str(metric)]

        return " ".join(parts)

    # Converte una rule IPv4 JSON in un comando ip rule add semplificato
    def _ipv4_rule_to_command(self, rule: dict[str, Any]) -> str | None:
        parts = ["ip", "rule", "add"]

        src = rule.get("src")
        dst = rule.get("dst")
        table = rule.get("table")
        priority = rule.get("priority")

        if src:
            parts += ["from", src]
        if dst:
            parts += ["to", dst]
        if table:
            parts += ["lookup", str(table)]
        if priority is not None:
            parts += ["priority", str(priority)]

        if parts == ["ip", "rule", "add"]:
            return None
        return " ".join(parts)

    # Converte una rule IPv6 JSON in un comando ip -6 rule add semplificato
    def _ipv6_rule_to_command(self, rule: dict[str, Any]) -> str | None:
        parts = ["ip", "-6", "rule", "add"]

        src = rule.get("src")
        dst = rule.get("dst")
        table = rule.get("table")
        priority = rule.get("priority")

        if src:
            parts += ["from", src]
        if dst:
            parts += ["to", dst]
        if table:
            parts += ["lookup", str(table)]
        if priority is not None:
            parts += ["priority", str(priority)]

        if parts == ["ip", "-6", "rule", "add"]:
            return None
        return " ".join(parts)

    # Ricava uno snapshot esteso e read-only dello stato del nodo Linux
    def get_running_config(self, device: str) -> dict:
        sections = [
            ("ip addr", "ip addr show"),
            ("ip -6 addr", "ip -6 addr show"),
            ("routes", "ip route show"),
            ("ip -6 routes", "ip -6 route show"),
            ("ip rules", "ip rule show"),
            ("ip -6 rules", "ip -6 rule show"),
            ("forwarding", "sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding"),
        ]

        outputs = []
        for title, command in sections:
            try:
                out = self._exec_readonly(device, command)
            except Exception as e:
                out = f"ERROR: {e}"
            outputs.append(f"=== {title} ===\n{out}")

        try:
            frr_out = self._exec_readonly(device, "vtysh -c 'show running-config'")
            outputs.append(f"=== frr running-config ===\n{frr_out}")
        except Exception:
            pass

        try:
            nft_out = self._exec_readonly(device, "nft list ruleset")
            outputs.append(f"=== nft ruleset ===\n{nft_out}")
        except Exception:
            pass

        try:
            iptables_out = self._exec_readonly(device, "iptables-save")
            outputs.append(f"=== iptables-save ===\n{iptables_out}")
        except Exception:
            pass

        return {"running_config": "\n".join(outputs)}

    # Cattura uno snapshot strutturato dello stato di rete utile per il rollback
# Cattura uno snapshot strutturato dello stato di rete utile per il rollback
    def capture_rollback_state(self, device: str) -> dict[str, Any]:
        ipv4_addr_json = self._exec_json(device, "ip -j addr show", default=[])
        ipv6_addr_json = self._exec_json(device, "ip -j -6 addr show", default=[])
        ipv4_route_json = self._exec_json(device, "ip -j route show", default=[])
        ipv6_route_json = self._exec_json(device, "ip -j -6 route show", default=[])
        ipv4_rule_json = self._exec_json(device, "ip -j rule show", default=[])
        ipv6_rule_json = self._exec_json(device, "ip -j -6 rule show", default=[])

        sysctl_text = self._exec_readonly(
            device,
            "sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding"
        )

        return {
            "addresses_v4": self._extract_ipv4_addresses(ipv4_addr_json),
            "addresses_v6": self._extract_ipv6_addresses(ipv6_addr_json),
            "routes_v4": self._extract_ipv4_routes(ipv4_route_json),
            "routes_v6": self._extract_ipv6_routes(ipv6_route_json),
            "rules_v4": self._extract_ipv4_rules(ipv4_rule_json),
            "rules_v6": self._extract_ipv6_rules(ipv6_rule_json),
            "sysctl": self._parse_sysctl_output(sysctl_text),
        }

    # Ripristina uno snapshot Linux con strategia best-effort su address, route, rule e forwarding
    def restore_rollback_state(self, device: str, snapshot: dict[str, Any]) -> dict:
        steps: list[dict[str, str]] = []

        try:
            # 1. Elimina rule custom attuali
            current_v4_rules = self._extract_ipv4_rules(self._exec_json(device, "ip -j rule show"))
            current_v6_rules = self._extract_ipv6_rules(self._exec_json(device, "ip -j -6 rule show"))

            for rule in current_v4_rules:
                prio = rule.get("priority")
                if prio is not None:
                    cmd = f"ip rule del priority {prio}"
                    self._exec(device, cmd, check=False)
                    steps.append({"step": cmd, "status": "ok"})

            for rule in current_v6_rules:
                prio = rule.get("priority")
                if prio is not None:
                    cmd = f"ip -6 rule del priority {prio}"
                    self._exec(device, cmd, check=False)
                    steps.append({"step": cmd, "status": "ok"})

            # 2. Flush delle route main table
            for cmd in [
                "ip route flush table main",
                "ip -6 route flush table main",
            ]:
                self._exec(device, cmd, check=False)
                steps.append({"step": cmd, "status": "ok"})

            # 3. Flush degli indirizzi globali non-loopback
            addr_state = self._exec_json(device, "ip -j addr show")
            interfaces = [iface.get("ifname") for iface in addr_state or [] if iface.get("ifname") and iface.get("ifname") != "lo"]

            for ifname in interfaces:
                cmd = f"ip addr flush dev {ifname} scope global"
                self._exec(device, cmd, check=False)
                steps.append({"step": cmd, "status": "ok"})

            for ifname in interfaces:
                cmd = f"ip -6 addr flush dev {ifname} scope global"
                self._exec(device, cmd, check=False)
                steps.append({"step": cmd, "status": "ok"})

            # 4. Ripristina indirizzi IPv4
            for item in snapshot.get("addresses_v4", []):
                cmd = f"ip addr add {item['cidr']} dev {item['ifname']}"
                self._exec(device, cmd, check=False)
                steps.append({"step": cmd, "status": "ok"})

            # 5. Ripristina indirizzi IPv6 globali
            for item in snapshot.get("addresses_v6", []):
                cmd = f"ip -6 addr add {item['cidr']} dev {item['ifname']}"
                self._exec(device, cmd, check=False)
                steps.append({"step": cmd, "status": "ok"})

            # 6. Ripristina route IPv4
            for route in snapshot.get("routes_v4", []):
                cmd = self._ipv4_route_to_command(route)
                if cmd:
                    self._exec(device, cmd, check=False)
                    steps.append({"step": cmd, "status": "ok"})

            # 7. Ripristina route IPv6
            for route in snapshot.get("routes_v6", []):
                cmd = self._ipv6_route_to_command(route)
                if cmd:
                    self._exec(device, cmd, check=False)
                    steps.append({"step": cmd, "status": "ok"})

            # 8. Ripristina rule IPv4
            for rule in snapshot.get("rules_v4", []):
                cmd = self._ipv4_rule_to_command(rule)
                if cmd:
                    self._exec(device, cmd, check=False)
                    steps.append({"step": cmd, "status": "ok"})

            # 9. Ripristina rule IPv6
            for rule in snapshot.get("rules_v6", []):
                cmd = self._ipv6_rule_to_command(rule)
                if cmd:
                    self._exec(device, cmd, check=False)
                    steps.append({"step": cmd, "status": "ok"})

            # 10. Ripristina sysctl forwarding
            sysctl_state = snapshot.get("sysctl", {})
            if "net.ipv4.ip_forward" in sysctl_state:
                val = sysctl_state["net.ipv4.ip_forward"]
                cmd = f"sysctl -w net.ipv4.ip_forward={val}"
                self._exec(device, cmd, check=False)
                steps.append({"step": cmd, "status": "ok"})

            if "net.ipv6.conf.all.forwarding" in sysctl_state:
                val = sysctl_state["net.ipv6.conf.all.forwarding"]
                cmd = f"sysctl -w net.ipv6.conf.all.forwarding={val}"
                self._exec(device, cmd, check=False)
                steps.append({"step": cmd, "status": "ok"})

            return {
                "status": "success",
                "steps": steps,
            }
        except Exception as e:
            return {
                "status": "error",
                "steps": steps,
                "error": str(e),
            }

    # Applica una lista di comandi di configurazione e li registra nel log dei comandi eseguiti
    def update_config(self, device: str, commands: list[str]) -> dict:
        results = []

        for cmd in commands:
            cmd = cmd.strip()

            if not self._is_allowed_command(cmd):
                results.append({
                    "command": cmd,
                    "status": "blocked",
                    "output": "command not allowed",
                })
                continue

            with open(self.log_file, "a") as f:
                f.write(json.dumps({"device": device, "command": cmd}) + "\n")

            try:
                out = self._exec(device, cmd)
                results.append({
                    "command": cmd,
                    "status": "success",
                    "output": out,
                })
            except subprocess.TimeoutExpired:
                results.append({
                    "command": cmd,
                    "status": "error",
                    "output": "command timed out",
                })
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "").strip()
                stdout = (e.stdout or "").strip()
                output = stderr or stdout or str(e)
                results.append({
                    "command": cmd,
                    "status": "error",
                    "output": output,
                })
            except Exception as e:
                results.append({
                    "command": cmd,
                    "status": "error",
                    "output": str(e),
                })

        return {"results": results}

    # Esegue un comando di validazione, purché read-only, senza far esplodere il driver se fallisce
    def execute_validation(self, device: str, command: str) -> dict:
        try:
            out = self._exec_readonly(device, command, timeout=15)
            return {"output": out}
        except subprocess.TimeoutExpired:
            return {"output": "validation command timed out"}
        except Exception as e:
            return {"output": str(e)}