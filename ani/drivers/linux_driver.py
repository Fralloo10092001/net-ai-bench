import json
import re
import shlex
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
    "ip -6 addr add",
    "ip -6 addr del",
    "ip link add",
    "ip link set",
    "ip link show",
    "ip route add",
    "ip route del",
    "ip route show",
    "ip -6 route add",
    "ip -6 route del",
    "ip -6 route show",
    "ip rule add",
    "ip rule del",
    "ip rule show",
    "ip -6 rule show",
    "nft add table ",
    "nft add chain ",
    "nft add rule ",

    # Forwarding
    "sysctl -w net.ipv4.ip_forward=",
    "sysctl -w net.ipv6.conf.all.forwarding=",
    "sysctl -w net.ipv4.conf.all.rp_filter=",
    "sysctl -w net.ipv4.conf.default.rp_filter=",
    "sysctl -w net.ipv4.conf.eth1.rp_filter=",
    "sysctl -w net.ipv4.conf.eth2.rp_filter=",

    # Read sysctl forwarding (some tasks inspect)
    "sysctl net.ipv4.ip_forward",
    "sysctl net.ipv6.conf.all.forwarding",
    "sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding",

    # Read sysctl rp_filter (optional but useful)
    "sysctl net.ipv4.conf.all.rp_filter",
    "sysctl net.ipv4.conf.default.rp_filter",
    "sysctl net.ipv4.conf.eth1.rp_filter",

    # /proc sysctl equivalents
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

    # FRR show
    "vtysh -c 'show running-config'",
    "vtysh -c \"show running-config\"",
    "vtysh -c 'show version'",
    "vtysh -c \"show version\"",
    "vtysh -c 'configure terminal'",
    "vtysh -c \"configure terminal\"",
    "vtysh -c 'conf t'",
    "vtysh -c \"conf t\"",

    # Firewall read
    "nft list ruleset",
    "iptables-save",

    # Firewall apply (Task 4) - load ruleset from file
    "nft -f ",

    # NAT minimal (Task 5) - allow only additive rules / checks
    "iptables -t nat -A ",
    "iptables -A FORWARD ",
    "iptables -C ",
    "iptables -t nat -A PREROUTING ",
    "iptables -t nat -A POSTROUTING ",
    "iptables -t nat -C PREROUTING ",
    "iptables -t nat -C POSTROUTING ",
    "iptables -A FORWARD ",
    "iptables -C FORWARD ",
    # Start long-running services in background (used for internal HTTP server)
    "nohup ",

    # DNAT/SNAT rules for Floating IP task
    "iptables -t nat -A PREROUTING ",
    "iptables -t nat -A POSTROUTING ",
    "iptables -t nat -C PREROUTING ",
    "iptables -t nat -C POSTROUTING ",
    "iptables -A FORWARD ",
    "iptables -C FORWARD ",
    "iptables -t nat -D PREROUTING ",
    "iptables -t nat -D POSTROUTING ",
    "iptables -D FORWARD ",
]

# Comandi o sottostringhe pericolose da bloccare
BLOCKED_SUBSTRINGS = [
    "rm ",
    "reboot",
    "shutdown",
    "poweroff",
    "mkfs",

    # Prevent flushing/resetting firewalls
    "iptables -F",
    "iptables -P",          # default policies
    "iptables -X",          # delete chains
    "iptables -Z",          # zero counters (not dangerous but hides evidence)
    "iptables-restore",     # too powerful

    "nft flush",
    "nft reset",
    "nft delete table",
    "nft delete chain",
    "nft -f /etc/",         # avoid writing system-level firewall configs (optional hardening)

    # Prevent modifying /etc
    "> /etc/",
    ">> /etc/",
    "chmod ",
    "chown ",

    # Package managers / downloads / shells
    "apt ",
    "yum ",
    "dnf ",
    "apk ",
    "wget ",
    "nc -e",
    "bash -c",
    "flush ruleset",
    "nft delete ruleset",

    # Keep bind-mounted FRR startup configs immutable across benchmark runs.
    "write memory",
]

# Token di chaining shell che vogliamo proibire
FORBIDDEN_TOKENS = ["&&", "||", ";"]

# Comandi read-only ammessi per get_running_config e validation
READ_ONLY_PREFIXES = [
    "iptables -t nat -S",
    "iptables -S",
    "nft list ruleset",
    "nft list tables",
    "nft list table ",
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

    # Read rp_filter
    "sysctl net.ipv4.conf.all.rp_filter",
    "sysctl net.ipv4.conf.default.rp_filter",
    "sysctl net.ipv4.conf.eth1.rp_filter",
    "sysctl net.ipv4.conf.eth2.rp_filter",

    "cat /proc/sys/net/ipv4/ip_forward",
    "cat /proc/sys/net/ipv6/conf/all/forwarding",

    "hostname",
    "uname ",

    "vtysh -c 'show running-config'",
    "vtysh -c \"show running-config\"",
    "vtysh -c 'show version'",
    "vtysh -c \"show version\"",
    "vtysh -c 'show ",
    "vtysh -c \"show ",

    "nft list ruleset",
    "iptables-save",

    "ss ",
    "netstat ",
    "arp ",
    "curl -sS --max-time ",
    "curl --max-time ",
    "haproxy -c -f ",
    "ip neigh",
    "ip -br addr",
    "ip -br link",
    "nping ",

    # Optional: readable iptables views (useful for audits)
    "iptables -S",
    "iptables -t nat -S",
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
            ["docker", "exec", "-u", "0", container_name, "sh", "-c", command],
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

    # Esegue un comando passando contenuto su stdin, usato solo dai restore interni.
    def _exec_stdin(
        self,
        node: str,
        command: str,
        data: str,
        check: bool = False,
        timeout: int = 20,
    ) -> str:
        container_name = self._clab(node)

        if not self._container_exists(container_name):
            raise RuntimeError(f"Container not found: {container_name}")

        result = subprocess.run(
            ["docker", "exec", "-i", "-u", "0", container_name, "sh", "-c", command],
            input=data,
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

    def _allows_semicolon_for_nft_chain(self, cmd: str) -> bool:
        if ";" not in cmd:
            return True
        return cmd.startswith("nft add chain ")

    def _is_allowed_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        # Still block shell chaining operators
        if "&&" in cmd or "||" in cmd:
            return False

        # Prevent taking down data-plane interfaces (common destructive behavior)
        # (Note: we allow eth0 to be manipulated only if it's already in allowlist, but
        # we explicitly block eth1/eth2 down.)
        if cmd.startswith("ip link set"):
            lowered = cmd.lower()
            if (" eth1 " in lowered or lowered.endswith(" eth1") or " dev eth1" in lowered) and " down" in lowered:
                return False
            if (" eth2 " in lowered or lowered.endswith(" eth2") or " dev eth2" in lowered) and " down" in lowered:
                return False

        # Prevent deleting IPv4 addressing on data-plane interfaces
        # This keeps the lab reachable and avoids "destructive_fail" caused by LLMs wiping IPs.
        if cmd.startswith("ip addr del"):
            lowered = cmd.lower()
            if " dev eth1" in lowered or " dev eth2" in lowered:
                return False

        # (Optional) also prevent deleting IPv6 global addresses on eth1/eth2
        # Note: your driver already allows "ip -6 addr show" but not "ip -6 addr del" by default.
        if cmd.startswith("ip -6 addr del"):
            lowered = cmd.lower()
            if " dev eth1" in lowered or " dev eth2" in lowered:
                return False

        # Block ';' except for nft chain definitions that require it
        if ";" in cmd and not self._allows_semicolon_for_nft_chain(cmd):
            return False

        if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            if "-c" not in cmd:
                return False

        return any(cmd.startswith(prefix) for prefix in ALLOWED_PREFIXES)

    def _is_readonly_command(self, cmd: str) -> bool:
        cmd = cmd.strip()

        if not cmd:
            return False

        if "&&" in cmd or "||" in cmd:
            return False

        if ";" in cmd and not self._allows_semicolon_for_nft_chain(cmd):
            return False

        if any(bad in cmd for bad in BLOCKED_SUBSTRINGS):
            return False

        if cmd.startswith("ping ") or cmd.startswith("ping6 "):
            return "-c" in cmd

        if cmd.startswith("curl "):
            return "--max-time" in cmd and re.search(r"\shttps?://", cmd) is not None

        if cmd.startswith("nping "):
            return " -c " in f" {cmd} "

        return any(cmd.startswith(prefix) for prefix in READ_ONLY_PREFIXES)

    # Esegue un comando read-only opzionale e scarta output che indica assenza tool.
    def _capture_optional_readonly(self, node: str, command: str, timeout: int = 10) -> dict[str, Any]:
        try:
            out = self._exec_readonly(node, command, timeout=timeout).strip()
        except Exception as e:
            return {
                "available": False,
                "data": "",
                "error": str(e),
            }

        lowered = out.lower()
        unavailable_markers = [
            "not found",
            "no such file or directory",
            "can't initialize",
            "permission denied",
        ]

        if any(marker in lowered for marker in unavailable_markers):
            return {
                "available": False,
                "data": "",
                "error": out,
            }

        return {
            "available": True,
            "data": out,
            "error": "",
        }

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

    # Estrae lo stato link/VRF necessario per ripristinare master e link virtuali.
    def _extract_links(self, link_json: list[dict[str, Any]]) -> list[dict[str, Any]]:
        links = []

        for link in link_json or []:
            ifname = link.get("ifname")
            if not ifname:
                continue

            linkinfo = link.get("linkinfo") or {}
            info_data = linkinfo.get("info_data") or {}

            links.append({
                "ifname": ifname,
                "master": link.get("master"),
                "kind": linkinfo.get("info_kind"),
                "table": info_data.get("table"),
                "flags": link.get("flags") or [],
            })

        return links

    # Parser fallback per immagini con BusyBox ip senza output JSON.
    def _parse_ip_addr_show(self, text: str, family: str) -> list[dict[str, str]]:
        results = []
        current_ifname = ""

        for line in text.splitlines():
            interface_match = re.match(r"^\d+:\s+([^:]+):", line)
            if interface_match:
                current_ifname = interface_match.group(1).split("@", 1)[0]
                continue

            if not current_ifname or current_ifname == "lo":
                continue

            if family == "inet":
                match = re.search(r"\binet\s+(\S+)", line)
                if not match:
                    continue

                if "scope host" in line:
                    continue

                results.append({
                    "ifname": current_ifname,
                    "cidr": match.group(1),
                })

            elif family == "inet6":
                match = re.search(r"\binet6\s+(\S+)", line)
                if not match:
                    continue

                if "scope host" in line or "scope link" in line:
                    continue

                results.append({
                    "ifname": current_ifname,
                    "cidr": match.group(1),
                })

        return results

    # Parser fallback per route testuali Linux/BusyBox.
    def _parse_ip_route_show(self, text: str) -> list[dict[str, Any]]:
        routes = []

        for line in text.splitlines():
            normalized = line.strip()
            if not normalized:
                continue

            tokens = normalized.split()
            route: dict[str, Any] = {
                "dst": tokens[0],
            }

            idx = 1
            while idx < len(tokens):
                token = tokens[idx]

                if token == "via" and idx + 1 < len(tokens):
                    route["gateway"] = tokens[idx + 1]
                    idx += 2
                    continue

                if token == "dev" and idx + 1 < len(tokens):
                    route["dev"] = tokens[idx + 1]
                    idx += 2
                    continue

                if token == "metric" and idx + 1 < len(tokens):
                    route["metric"] = tokens[idx + 1]
                    idx += 2
                    continue

                if token == "proto" and idx + 1 < len(tokens):
                    route["protocol"] = tokens[idx + 1]
                    idx += 2
                    continue

                idx += 1

            routes.append(route)

        return routes

    # Parser fallback per ip rule show testuale.
    def _parse_ip_rule_show(self, text: str) -> list[dict[str, Any]]:
        rules = []

        for line in text.splitlines():
            normalized = line.strip()
            match = re.match(r"^(\d+):\s+(.*)$", normalized)
            if not match:
                continue

            rule: dict[str, Any] = {
                "priority": int(match.group(1)),
            }
            tokens = match.group(2).split()

            idx = 0
            while idx < len(tokens):
                token = tokens[idx]

                if token == "from" and idx + 1 < len(tokens) and tokens[idx + 1] != "all":
                    rule["src"] = tokens[idx + 1]
                    idx += 2
                    continue

                if token == "to" and idx + 1 < len(tokens) and tokens[idx + 1] != "all":
                    rule["dst"] = tokens[idx + 1]
                    idx += 2
                    continue

                if token in {"lookup", "table"} and idx + 1 < len(tokens):
                    rule["table"] = tokens[idx + 1]
                    idx += 2
                    continue

                idx += 1

            rules.append(rule)

        return rules

    # Parser fallback per ip link show testuale.
    def _parse_ip_link_show(self, text: str) -> list[dict[str, Any]]:
        links = []

        for line in text.splitlines():
            match = re.match(r"^\d+:\s+([^:]+):\s+<([^>]*)>(.*)$", line)
            if not match:
                continue

            ifname = match.group(1).split("@", 1)[0]
            suffix = match.group(3)
            master = None

            master_match = re.search(r"\bmaster\s+(\S+)", suffix)
            if master_match:
                master = master_match.group(1)

            kind = None
            table = None
            if ifname.startswith("vrf"):
                kind = "vrf"

            links.append({
                "ifname": ifname,
                "master": master,
                "kind": kind,
                "table": table,
                "flags": [flag for flag in match.group(2).split(",") if flag],
            })

        return links

    def _capture_ipv4_addresses(self, device: str) -> list[dict[str, str]]:
        data = self._exec_json(device, "ip -j addr show", default=None)
        if data is not None:
            return self._extract_ipv4_addresses(data)

        text = self._exec(device, "ip addr show", check=False)
        return self._parse_ip_addr_show(text, "inet")

    def _capture_ipv6_addresses(self, device: str) -> list[dict[str, str]]:
        data = self._exec_json(device, "ip -j -6 addr show", default=None)
        if data is not None:
            return self._extract_ipv6_addresses(data)

        text = self._exec(device, "ip addr show", check=False)
        return self._parse_ip_addr_show(text, "inet6")

    def _capture_ipv4_routes(self, device: str) -> list[dict[str, Any]]:
        data = self._exec_json(device, "ip -j route show", default=None)
        if data is not None:
            return self._extract_ipv4_routes(data)

        text = self._exec(device, "ip route show", check=False)
        return self._extract_ipv4_routes(self._parse_ip_route_show(text))

    def _capture_ipv6_routes(self, device: str) -> list[dict[str, Any]]:
        data = self._exec_json(device, "ip -j -6 route show", default=None)
        if data is not None:
            return self._extract_ipv6_routes(data)

        text = self._exec(device, "ip -6 route show", check=False)
        return self._extract_ipv6_routes(self._parse_ip_route_show(text))

    def _capture_ipv4_rules(self, device: str) -> list[dict[str, Any]]:
        data = self._exec_json(device, "ip -j rule show", default=None)
        if data is not None:
            return self._extract_ipv4_rules(data)

        text = self._exec(device, "ip rule show", check=False)
        return self._extract_ipv4_rules(self._parse_ip_rule_show(text))

    def _capture_ipv6_rules(self, device: str) -> list[dict[str, Any]]:
        data = self._exec_json(device, "ip -j -6 rule show", default=None)
        if data is not None:
            return self._extract_ipv6_rules(data)

        text = self._exec(device, "ip -6 rule show", check=False)
        return self._extract_ipv6_rules(self._parse_ip_rule_show(text))

    def _capture_links(self, device: str) -> list[dict[str, Any]]:
        data = self._exec_json(device, "ip -j link show", default=None)
        if data is not None:
            return self._extract_links(data)

        text = self._exec(device, "ip link show", check=False)
        return self._parse_ip_link_show(text)

    # Normalizza show running-config FRR per frr-reload.py.
    def _normalize_frr_config(self, text: str) -> str:
        lines = []

        for line in text.splitlines():
            stripped = line.strip()

            if not stripped:
                continue
            if stripped == "Building configuration...":
                continue
            if stripped == "Current configuration:":
                continue

            lines.append(line.rstrip())

        if not lines:
            return ""

        return "\n".join(lines).strip() + "\n"

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


    # Costruisce una chiave stabile per confrontare due elementi di stato
    def _stable_state_key(self, item: dict[str, Any]) -> str:
        return json.dumps(item, sort_keys=True, default=str)

    # Costruisce una chiave stabile per confrontare indirizzi IP
    def _address_key(self, item: dict[str, str]) -> str:
        return f"{item.get('ifname')}|{item.get('cidr')}"

    # Costruisce una chiave stabile per confrontare route IP
    def _route_key(self, route: dict[str, Any]) -> str:
        relevant = {
            "dst": route.get("dst", "default"),
            "gateway": route.get("gateway"),
            "dev": route.get("dev"),
            "metric": route.get("metric"),
            "protocol": route.get("protocol"),
            "table": route.get("table"),
            "prefsrc": route.get("prefsrc"),
        }
        return self._stable_state_key(relevant)

    # Costruisce una chiave stabile per confrontare rule IP
    def _rule_key(self, rule: dict[str, Any]) -> str:
        relevant = {
            "priority": rule.get("priority"),
            "src": rule.get("src"),
            "dst": rule.get("dst"),
            "table": rule.get("table"),
            "action": rule.get("action"),
        }
        return self._stable_state_key(relevant)

    # Costruisce una mappa ifname -> link state.
    def _link_by_name(self, links: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            str(link.get("ifname")): link
            for link in links
            if link.get("ifname")
        }

    # Indica se una interfaccia è protetta perché usata tipicamente per management
    def _is_protected_interface(self, ifname: str | None) -> bool:
        return ifname in {"eth0", "mgmt0", "management0"}

    # Indica se un link creato dopo lo snapshot puo' essere rimosso dal rollback.
    def _is_removable_created_link(self, link: dict[str, Any]) -> bool:
        ifname = str(link.get("ifname") or "")

        if self._is_protected_interface(ifname):
            return False

        if ifname.startswith("vrf"):
            return True

        return link.get("kind") in {
            "vrf",
            "dummy",
            "bridge",
            "veth",
        }

    # Indica se una route è protetta e non deve essere rimossa dal rollback automatico
    def _is_protected_route(self, route: dict[str, Any]) -> bool:
        dst = route.get("dst", "default")
        dev = route.get("dev")

        if self._is_protected_interface(dev):
            return True

        if dst == "default":
            return True

        return False

    # Converte una route IPv4 JSON in un comando ip route del
    def _ipv4_route_to_delete_command(self, route: dict[str, Any]) -> str | None:
        dst = route.get("dst", "default")
        parts = ["ip", "route", "del", dst]

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

    # Converte una route IPv6 JSON in un comando ip -6 route del
    def _ipv6_route_to_delete_command(self, route: dict[str, Any]) -> str | None:
        dst = route.get("dst", "default")
        parts = ["ip", "-6", "route", "del", dst]

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

    # Converte una rule IPv4 JSON in un comando ip rule del semplificato
    def _ipv4_rule_to_delete_command(self, rule: dict[str, Any]) -> str | None:
        priority = rule.get("priority")

        if priority is not None:
            return f"ip rule del priority {priority}"

        add_cmd = self._ipv4_rule_to_command(rule)
        if not add_cmd:
            return None

        return add_cmd.replace("ip rule add", "ip rule del", 1)

    # Converte una rule IPv6 JSON in un comando ip -6 rule del semplificato
    def _ipv6_rule_to_delete_command(self, rule: dict[str, Any]) -> str | None:
        priority = rule.get("priority")

        if priority is not None:
            return f"ip -6 rule del priority {priority}"

        add_cmd = self._ipv6_rule_to_command(rule)
        if not add_cmd:
            return None

        return add_cmd.replace("ip -6 rule add", "ip -6 rule del", 1)

    # Registra ed esegue un comando di rollback best-effort
    def _run_rollback_command(self, device: str, cmd: str, steps: list[dict[str, str]]) -> None:
        out = self._exec(device, cmd, check=False)
        steps.append({
            "step": cmd,
            "status": "ok",
            "output": out,
        })

    # Ripristina la running config FRR da uno snapshot, se FRR e frr-reload.py sono disponibili.
    def _restore_frr_config(
        self,
        device: str,
        snapshot: dict[str, Any],
        steps: list[dict[str, str]],
        skipped: list[dict[str, str]],
    ) -> None:
        frr_state = snapshot.get("frr", {})

        if not frr_state.get("available"):
            return

        config = self._normalize_frr_config(str(frr_state.get("data", "")))
        if not config:
            skipped.append({
                "step": "restore frr running-config",
                "reason": "empty FRR snapshot",
            })
            return

        tmp_path = "/tmp/netconfbench-rollback-frr.conf"
        out = self._exec_stdin(
            device,
            f"cat > {shlex.quote(tmp_path)}",
            config,
            check=False,
            timeout=10,
        )
        steps.append({
            "step": f"write {tmp_path}",
            "status": "ok",
            "output": out,
        })

        reload_cmd = (
            f"if command -v frr-reload.py >/dev/null 2>&1; then "
            f"frr-reload.py --reload {shlex.quote(tmp_path)}; "
            f"else /usr/lib/frr/frr-reload.py --reload {shlex.quote(tmp_path)}; fi"
        )
        out = self._exec(device, reload_cmd, check=False, timeout=35)
        steps.append({
            "step": "restore frr running-config",
            "status": "ok",
            "output": out,
        })

    # Ripristina nftables usando un ruleset testuale nello snapshot.
    def _restore_nft_ruleset(
        self,
        device: str,
        snapshot: dict[str, Any],
        steps: list[dict[str, str]],
    ) -> None:
        nft_state = snapshot.get("nft", {})

        if not nft_state.get("available"):
            return

        ruleset = str(nft_state.get("data", "")).strip()
        restore_input = "flush ruleset\n"

        if ruleset:
            restore_input += ruleset + "\n"

        out = self._exec_stdin(
            device,
            "nft -f -",
            restore_input,
            check=False,
            timeout=20,
        )
        steps.append({
            "step": "restore nft ruleset",
            "status": "ok",
            "output": out,
        })

    # Ripristina iptables usando iptables-restore, non esposto agli agenti.
    def _restore_iptables_rules(
        self,
        device: str,
        snapshot: dict[str, Any],
        steps: list[dict[str, str]],
    ) -> None:
        iptables_state = snapshot.get("iptables", {})

        if not iptables_state.get("available"):
            return

        rules = str(iptables_state.get("data", "")).strip()
        if not rules:
            return

        out = self._exec_stdin(
            device,
            "iptables-restore",
            rules + "\n",
            check=False,
            timeout=20,
        )
        steps.append({
            "step": "restore iptables rules",
            "status": "ok",
            "output": out,
        })

    # Ripristina master VRF/link state e rimuove link virtuali creati dopo lo snapshot.
    def _restore_link_state(
        self,
        device: str,
        snapshot: dict[str, Any],
        steps: list[dict[str, str]],
        skipped: list[dict[str, str]],
    ) -> None:
        snapshot_links = self._link_by_name(snapshot.get("links", []))
        current_links = self._link_by_name(self._capture_links(device))

        # Recreate missing VRF links if they existed in the snapshot.
        for ifname, snapshot_link in snapshot_links.items():
            if ifname in current_links:
                continue
            if snapshot_link.get("kind") != "vrf" or snapshot_link.get("table") is None:
                continue

            cmd = f"ip link add {shlex.quote(ifname)} type vrf table {snapshot_link['table']}"
            self._run_rollback_command(device, cmd, steps)

        current_links = self._link_by_name(self._capture_links(device))

        # Restore interface master before deleting created VRFs.
        for ifname, snapshot_link in snapshot_links.items():
            if ifname not in current_links:
                continue
            if self._is_protected_interface(ifname) or ifname == "lo":
                continue

            current_master = current_links[ifname].get("master")
            snapshot_master = snapshot_link.get("master")

            if current_master == snapshot_master:
                continue

            quoted_ifname = shlex.quote(ifname)
            if snapshot_master:
                cmd = f"ip link set {quoted_ifname} master {shlex.quote(str(snapshot_master))}"
            else:
                cmd = f"ip link set {quoted_ifname} nomaster"

            self._run_rollback_command(device, cmd, steps)

        current_links = self._link_by_name(self._capture_links(device))

        # Restore administrative up/down state for links present in both snapshots.
        for ifname, snapshot_link in snapshot_links.items():
            current_link = current_links.get(ifname)
            if not current_link:
                continue
            if self._is_protected_interface(ifname) or ifname == "lo":
                continue

            snapshot_up = "UP" in (snapshot_link.get("flags") or [])
            current_up = "UP" in (current_link.get("flags") or [])

            if snapshot_up == current_up:
                continue

            state = "up" if snapshot_up else "down"
            self._run_rollback_command(device, f"ip link set {shlex.quote(ifname)} {state}", steps)

        # Delete virtual links created after the snapshot, notably VRFs.
        for ifname, current_link in current_links.items():
            if ifname in snapshot_links:
                continue

            if not self._is_removable_created_link(current_link):
                skipped.append({
                    "step": f"ip link del {ifname}",
                    "reason": "created link kind is not rollback-removable",
                })
                continue

            self._run_rollback_command(device, f"ip link del {shlex.quote(ifname)}", steps)

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
    def capture_rollback_state(self, device: str) -> dict[str, Any]:
        sysctl_text = self._exec_readonly(
            device,
            "sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding"
        )

        return {
            "addresses_v4": self._capture_ipv4_addresses(device),
            "addresses_v6": self._capture_ipv6_addresses(device),
            "routes_v4": self._capture_ipv4_routes(device),
            "routes_v6": self._capture_ipv6_routes(device),
            "rules_v4": self._capture_ipv4_rules(device),
            "rules_v6": self._capture_ipv6_rules(device),
            "links": self._capture_links(device),
            "sysctl": self._parse_sysctl_output(sysctl_text),
            "frr": self._capture_optional_readonly(
                device,
                "vtysh -c 'show running-config'",
                timeout=15,
            ),
            "nft": self._capture_optional_readonly(
                device,
                "nft list ruleset",
                timeout=15,
            ),
            "iptables": self._capture_optional_readonly(
                device,
                "iptables-save",
                timeout=15,
            ),
        }

    # Ripristina uno snapshot Linux usando una strategia a delta non distruttiva
    def restore_rollback_state(self, device: str, snapshot: dict[str, Any]) -> dict:
        steps: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []

        try:
            self._restore_nft_ruleset(device, snapshot, steps)
            self._restore_iptables_rules(device, snapshot, steps)
            self._restore_frr_config(device, snapshot, steps, skipped)
            self._restore_link_state(device, snapshot, steps, skipped)

            current_addresses_v4 = self._capture_ipv4_addresses(device)
            current_addresses_v6 = self._capture_ipv6_addresses(device)
            current_routes_v4 = self._capture_ipv4_routes(device)
            current_routes_v6 = self._capture_ipv6_routes(device)
            current_rules_v4 = self._capture_ipv4_rules(device)
            current_rules_v6 = self._capture_ipv6_rules(device)

            snapshot_addresses_v4 = snapshot.get("addresses_v4", [])
            snapshot_addresses_v6 = snapshot.get("addresses_v6", [])
            snapshot_routes_v4 = snapshot.get("routes_v4", [])
            snapshot_routes_v6 = snapshot.get("routes_v6", [])
            snapshot_rules_v4 = snapshot.get("rules_v4", [])
            snapshot_rules_v6 = snapshot.get("rules_v6", [])

            snapshot_addr_v4_keys = {self._address_key(item) for item in snapshot_addresses_v4}
            snapshot_addr_v6_keys = {self._address_key(item) for item in snapshot_addresses_v6}
            current_addr_v4_keys = {self._address_key(item) for item in current_addresses_v4}
            current_addr_v6_keys = {self._address_key(item) for item in current_addresses_v6}

            snapshot_route_v4_by_key = {self._route_key(item): item for item in snapshot_routes_v4}
            snapshot_route_v6_by_key = {self._route_key(item): item for item in snapshot_routes_v6}
            current_route_v4_by_key = {self._route_key(item): item for item in current_routes_v4}
            current_route_v6_by_key = {self._route_key(item): item for item in current_routes_v6}

            snapshot_rule_v4_by_key = {self._rule_key(item): item for item in snapshot_rules_v4}
            snapshot_rule_v6_by_key = {self._rule_key(item): item for item in snapshot_rules_v6}
            current_rule_v4_by_key = {self._rule_key(item): item for item in current_rules_v4}
            current_rule_v6_by_key = {self._rule_key(item): item for item in current_rules_v6}

            # 1. Rimuove indirizzi IPv4 aggiunti dopo lo snapshot, evitando interfacce protette
            for item in current_addresses_v4:
                key = self._address_key(item)
                if key in snapshot_addr_v4_keys:
                    continue

                if self._is_protected_interface(item.get("ifname")):
                    skipped.append({
                        "step": f"ip addr del {item['cidr']} dev {item['ifname']}",
                        "reason": "protected interface",
                    })
                    continue

                cmd = f"ip addr del {item['cidr']} dev {item['ifname']}"
                self._run_rollback_command(device, cmd, steps)

            # 2. Rimuove indirizzi IPv6 aggiunti dopo lo snapshot, evitando interfacce protette
            for item in current_addresses_v6:
                key = self._address_key(item)
                if key in snapshot_addr_v6_keys:
                    continue

                if self._is_protected_interface(item.get("ifname")):
                    skipped.append({
                        "step": f"ip -6 addr del {item['cidr']} dev {item['ifname']}",
                        "reason": "protected interface",
                    })
                    continue

                cmd = f"ip -6 addr del {item['cidr']} dev {item['ifname']}"
                self._run_rollback_command(device, cmd, steps)

            # 3. Ripristina indirizzi IPv4 presenti nello snapshot ma mancanti ora
            for item in snapshot_addresses_v4:
                key = self._address_key(item)
                if key in current_addr_v4_keys:
                    continue

                cmd = f"ip addr add {item['cidr']} dev {item['ifname']}"
                self._run_rollback_command(device, cmd, steps)

            # 4. Ripristina indirizzi IPv6 presenti nello snapshot ma mancanti ora
            for item in snapshot_addresses_v6:
                key = self._address_key(item)
                if key in current_addr_v6_keys:
                    continue

                cmd = f"ip -6 addr add {item['cidr']} dev {item['ifname']}"
                self._run_rollback_command(device, cmd, steps)

            # 5. Rimuove route IPv4 aggiunte dopo lo snapshot, proteggendo management/default
            for key, route in current_route_v4_by_key.items():
                if key in snapshot_route_v4_by_key:
                    continue

                if self._is_protected_route(route):
                    skipped.append({
                        "step": self._ipv4_route_to_delete_command(route) or "ip route del",
                        "reason": "protected route",
                    })
                    continue

                cmd = self._ipv4_route_to_delete_command(route)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 6. Rimuove route IPv6 aggiunte dopo lo snapshot, proteggendo management/default
            for key, route in current_route_v6_by_key.items():
                if key in snapshot_route_v6_by_key:
                    continue

                if self._is_protected_route(route):
                    skipped.append({
                        "step": self._ipv6_route_to_delete_command(route) or "ip -6 route del",
                        "reason": "protected route",
                    })
                    continue

                cmd = self._ipv6_route_to_delete_command(route)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 7. Ripristina route IPv4 mancanti rispetto allo snapshot
            for key, route in snapshot_route_v4_by_key.items():
                if key in current_route_v4_by_key:
                    continue

                cmd = self._ipv4_route_to_command(route)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 8. Ripristina route IPv6 mancanti rispetto allo snapshot
            for key, route in snapshot_route_v6_by_key.items():
                if key in current_route_v6_by_key:
                    continue

                cmd = self._ipv6_route_to_command(route)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 9. Rimuove rule IPv4 aggiunte dopo lo snapshot
            for key, rule in current_rule_v4_by_key.items():
                if key in snapshot_rule_v4_by_key:
                    continue

                cmd = self._ipv4_rule_to_delete_command(rule)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 10. Rimuove rule IPv6 aggiunte dopo lo snapshot
            for key, rule in current_rule_v6_by_key.items():
                if key in snapshot_rule_v6_by_key:
                    continue

                cmd = self._ipv6_rule_to_delete_command(rule)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 11. Ripristina rule IPv4 mancanti rispetto allo snapshot
            for key, rule in snapshot_rule_v4_by_key.items():
                if key in current_rule_v4_by_key:
                    continue

                cmd = self._ipv4_rule_to_command(rule)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 12. Ripristina rule IPv6 mancanti rispetto allo snapshot
            for key, rule in snapshot_rule_v6_by_key.items():
                if key in current_rule_v6_by_key:
                    continue

                cmd = self._ipv6_rule_to_command(rule)
                if cmd:
                    self._run_rollback_command(device, cmd, steps)

            # 13. Ripristina sysctl forwarding allo stato dello snapshot
            sysctl_state = snapshot.get("sysctl", {})

            if "net.ipv4.ip_forward" in sysctl_state:
                val = sysctl_state["net.ipv4.ip_forward"]
                cmd = f"sysctl -w net.ipv4.ip_forward={val}"
                self._run_rollback_command(device, cmd, steps)

            if "net.ipv6.conf.all.forwarding" in sysctl_state:
                val = sysctl_state["net.ipv6.conf.all.forwarding"]
                cmd = f"sysctl -w net.ipv6.conf.all.forwarding={val}"
                self._run_rollback_command(device, cmd, steps)

            return {
                "status": "success",
                "strategy": "delta_restore",
                "steps": steps,
                "skipped": skipped,
            }

        except Exception as e:
            return {
                "status": "error",
                "strategy": "delta_restore",
                "steps": steps,
                "skipped": skipped,
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
