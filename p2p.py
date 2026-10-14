#!/usr/bin/env python3
"""TcpQuality-P2P: lightweight, single-destination TCP diagnostics.

GPL-3.0-only. https://github.com/ibsgss/TcpQuality inspired the project.
The standalone implementation does not fetch a target list or upload reports.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import statistics
import subprocess
import sys
import time

VERSION = "0.1.0"


def arguments(argv=None):
    p = argparse.ArgumentParser(
        prog="tcpquality-p2p", description="只测试指定两台设备之间的 TCP 网络质量。"
    )
    p.add_argument("target", nargs="?", help="目标 IPv4、IPv6 或域名")
    p.add_argument("--target", dest="named_target", help="等价于位置参数 target")
    p.add_argument("--port", type=int, default=22, help="目标 TCP 端口，默认 22 (SSH)")
    p.add_argument("-c", "--count", type=int, default=30, help="TCP 探测次数，默认 30")
    p.add_argument("--interval", type=float, default=0.3, help="发起探测的间隔秒数，默认 0.3")
    p.add_argument("--timeout", type=float, default=1.5, help="每次连接超时秒数，默认 1.5")
    p.add_argument("--mode", choices=("auto", "syn", "connect"), default="auto", help="auto: Linux root+nping 优先使用 TCP SYN")
    fam = p.add_mutually_exclusive_group()
    fam.add_argument("-4", "--ipv4", action="store_true")
    fam.add_argument("-6", "--ipv6", action="store_true")
    p.add_argument("--route", action="store_true", help="执行 TCP 路由追踪")
    p.add_argument("--speed", action="store_true", help="执行 iperf3 单线程正向及反向带宽测试")
    p.add_argument("--full", action="store_true", help="TCP 探测、路由和双向带宽（缺少 iperf3 时跳过）")
    p.add_argument("--iperf-port", type=int, default=5201, help="iperf3 服务端口，默认 5201")
    p.add_argument("--seconds", type=int, default=8, help="每个方向的带宽测试秒数，默认 8")
    p.add_argument("--max-rate", default=None, help="iperf3 的 TCP 速率上限，例如 30M，默认不限速")
    p.add_argument("--json", dest="json_path", metavar="FILE", help="将详细报告保存为 JSON")
    p.add_argument("--csv", dest="csv_path", metavar="FILE", help="将逐次探测结果保存为 CSV")
    p.add_argument("--serve", action="store_true", help="启动一个仅响应 TCP 连接的目标监听器")
    p.add_argument("--bind", default="127.0.0.1", help="监听器绑定地址，默认 127.0.0.1；需要外部访问请指定 Tailscale IP")
    p.add_argument("--listen-port", type=int, default=49222, help="监听器端口，默认 49222")
    p.add_argument("--version", action="version", version=VERSION)
    args = p.parse_args(argv)
    args.target = args.named_target or args.target
    for field in ("port", "iperf_port", "listen_port"):
        if not 1 <= getattr(args, field) <= 65535:
            p.error(f"{field}: 端口必须在 1-65535 之间")
    if not 1 <= args.count <= 10000 or args.interval < 0 or not 0.05 <= args.timeout <= 120:
        p.error("count 需在 1-10000，interval >= 0，timeout 需在 0.05-120 秒")
    if not 1 <= args.seconds <= 120:
        p.error("--seconds 需在 1-120 秒")
    if args.max_rate and not re.fullmatch(r"\d+(?:\.\d+)?[KMG]?", args.max_rate, re.I):
        p.error("--max-rate 格式示例：30M / 1G")
    if not args.serve and not args.target:
        p.error("请指定目标 IP，例如：p2p.py 1.2.3.4")
    if args.full:
        args.route = True
        args.speed = True
    return args


def resolve(target: str, port: int, only_v4=False, only_v6=False):
    # DNS resolution is intentionally handled by the system resolver.
    family = socket.AF_INET if only_v4 else socket.AF_INET6 if only_v6 else socket.AF_UNSPEC
    results = socket.getaddrinfo(target, port, family, socket.SOCK_STREAM)
    for f, socktype, proto, canon, address in results:
        if f in (socket.AF_INET, socket.AF_INET6):
            return f, address[0]
    raise OSError("目标地址未解析出 IPv4 或 IPv6")


def connect_probe(family, ip, port, timeout):
    began = time.perf_counter()
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            return "ok", (time.perf_counter() - began) * 1000, ""
        except ConnectionRefusedError:
            return "refused", (time.perf_counter() - began) * 1000, "目标端口拒绝连接（RST）"
        except (TimeoutError, socket.timeout):
            return "timeout", None, "连接超时（也可能是防火墙丢弃）"
        except OSError as e:
            return "error", None, str(e)


def syn_probe(ip, port, timeout, ipv6=False):
    cmd = ["nping", "-6" if ipv6 else "-4", "--tcp", "-p", str(port),
           "--flags", "syn", "-c", "1", ip]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "error", None, str(e)
    raw = p.stdout + "\n" + p.stderr
    # Nping reports SENT/RCVD and a timestamp (in seconds) on each matching TCP response.
    # Treat TCP RST as a valid path response but not as evidence of an open service.
    sent = re.search(r"^SENT \((\d+(?:\.\d+)?)s\).*? TCP (\S+)\s*>\s*(\S+)", raw, re.M)
    responses = re.findall(r"^RCVD \((\d+(?:\.\d+)?)s\).*? TCP (.*)$", raw, re.M)
    if not sent:
        return "error", None, "nping 发送失败：" + raw.strip()[-200:]
    # Return traffic must target the *source* port of the outgoing SYN.
    source_endpoint = sent.group(2)
    match = re.search(r":(\d+)\b", source_endpoint)
    source_port = match.group(1) if match else None
    for stamp, detail in responses:
        if source_port and re.search(r">\s*\S+:" + re.escape(source_port) + r"\b", detail):
            rtt = (float(stamp) - float(sent.group(1))) * 1000
            return "ok", max(0, rtt), "TCP SYN 已收到响应（含 SYN/ACK 或 RST）"
    if p.returncode not in (0, 1) and "Rcvd: 0" not in raw:
        return "error", None, "nping 异常：" + raw.strip()[-200:]
    return "timeout", None, "未收到匹配的 TCP 回复"


def statistics_for(samples):
    oks = [x["rtt_ms"] for x in samples if x["status"] == "ok" and x["rtt_ms"] is not None]
    n = len(samples)
    ordered = sorted(oks)
    def round3(value):
        return round(value, 3) if value is not None else None
    def percentile(q):
        if not ordered:
            return None
        index = (len(ordered) - 1) * q
        a, b = int(index), min(int(index) + 1, len(ordered) - 1)
        return round3(ordered[a] + (ordered[b] - ordered[a]) * (index - a))
    return {
        "sent": n, "received": len(oks),
        "refused": sum(x["status"] == "refused" for x in samples),
        "timed_out": sum(x["status"] == "timeout" for x in samples),
        "errors": sum(x["status"] == "error" for x in samples),
        "response_missing_pct": round((n - len(oks)) * 100 / n, 2) if n else None,
        "min_ms": round3(min(oks)) if oks else None,
        "avg_ms": round3(statistics.mean(oks)) if oks else None,
        "p50_ms": percentile(.5), "p95_ms": percentile(.95),
        "max_ms": round3(max(oks)) if oks else None,
        "jitter_ms": round3(statistics.mean(abs(b - a) for a, b in zip(oks, oks[1:]))) if len(oks) > 1 else None,
    }


def run_probes(args, family, ip, method):
    print(f"\n[1] TCP 测试 | {args.target} -> {ip}:{args.port} | 方法: {method}", flush=True)
    records = []
    for i in range(1, args.count + 1):
        started = time.perf_counter()
        if method == "syn":
            status, ms, details = syn_probe(ip, args.port, args.timeout, family == socket.AF_INET6)
        else:
            status, ms, details = connect_probe(family, ip, args.port, args.timeout)
        records.append({"index": i, "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "status": status, "rtt_ms": round(ms, 3) if ms is not None else None,
                        "details": details})
        if i <= 5 or i == args.count or i % 10 == 0 or status != "ok":
            latency = f"{ms:.2f} ms" if ms is not None else "-"
            print(f"  #{i:>3} {status:<8} {latency}", flush=True)
        delay = args.interval - (time.perf_counter() - started)
        if i < args.count and delay > 0:
            time.sleep(delay)
    stats = statistics_for(records)
    print(f"  响应 {stats['received']}/{stats['sent']} | 无有效响应 {stats['response_missing_pct']}%"
          f" | 端口拒绝 {stats['refused']} | 超时 {stats['timed_out']}")
    print(f"  RTT min/avg/p50/p95/max: {stats['min_ms']}/{stats['avg_ms']}/{stats['p50_ms']}/"
          f"{stats['p95_ms']}/{stats['max_ms']} ms | 抖动: {stats['jitter_ms']} ms")
    if method == "connect":
        print("  注：这是 TCP 完整连接成功率，不是 IP 层丢包率；端口拒绝与超时分别统计。")
    else:
        print("  注：SYN 回复率可能受到防火墙与限速影响，并非纯粹的物理链路丢包率。")
    return records, stats


def route_trace(ip, port, ipv6=False):
    if sys.platform.startswith("win"):
        command = ["tracert", "-d", "-h", "16", "-w", "500", ip]
        kind = "ICMP (Windows tracert)"
    elif shutil.which("traceroute"):
        command = ["traceroute", "-n", "-6" if ipv6 else "-4", "-T", "-p", str(port),
                   "-m", "16", "-w", "1", "-q", "1", ip]
        kind = "TCP traceroute"
    else:
        return {"status": "skipped", "reason": "未安装 traceroute"}
    print("\n[2] 路由追踪 | " + kind, flush=True)
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=40, check=False,
                              encoding="utf-8", errors="replace")
        output = (proc.stdout + proc.stderr).strip()
        print(output or "(无输出)")
        return {"status": "ok" if proc.returncode == 0 else "error", "kind": kind,
                "exit_code": proc.returncode, "command": command, "output": output}
    except (OSError, subprocess.TimeoutExpired) as e:
        print("  路由追踪未完成:", e)
        return {"status": "error", "reason": str(e)}


def speed_test(ip, port, seconds, max_rate, ipv6=False):
    if not shutil.which("iperf3"):
        print("\n[3] iperf3 未安装，跳过双向带宽测试。")
        return {"status": "skipped", "reason": "没有安装 iperf3"}
    print("\n[3] 双向单线程 TCP 吞吐量 | 目标需运行 iperf3 -s", flush=True)
    if not max_rate:
        print("  注意：默认不限速，可能消耗较多 VPS 出站流量。可使用 --max-rate 30M。", flush=True)
    results = {}
    for name, reverse in (("本机 -> 目标", False), ("目标 -> 本机", True)):
        cmd = ["iperf3", "-6" if ipv6 else "-4", "-c", ip, "-p", str(port),
               "-t", str(seconds), "-P", "1", "-J", "--connect-timeout", "3000"]
        if reverse:
            cmd += ["-R"]
        if max_rate:
            cmd += ["-b", max_rate]
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, timeout=seconds + 18,
                                  check=False, encoding="utf-8", errors="replace")
            payload = json.loads(proc.stdout)
            if proc.returncode != 0 or payload.get("error"):
                raise RuntimeError(payload.get("error") or proc.stderr.strip() or "iperf3 返回非零退出码")
            summary = payload.get("end", {}).get("sum_sent", {})
            receiver = payload.get("end", {}).get("sum_received", {})
            bps = receiver.get("bits_per_second") or summary.get("bits_per_second")
            retransmits = summary.get("retransmits")
            results[name] = {"status": "ok", "mbps": round(bps / 1000000, 3) if bps is not None else None,
                             "retransmits_reported": retransmits, "server_output": reverse,
                             "rate_limit": max_rate, "seconds": seconds}
            print(f"  {name}: {results[name]['mbps']} Mbps | 重传 {retransmits if retransmits is not None else '未提供'}")
        except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as e:
            print(f"  {name}: 跳过/失败（{e}）")
            results[name] = {"status": "error", "reason": str(e)}
    return {"status": "ok" if any(x["status"] == "ok" for x in results.values()) else "error",
            "directions": results, "rate_limit": max_rate}


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(1)
        try:
            self.request.recv(1)
        except (socket.timeout, OSError):
            pass


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server(bind, port):
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    class TypedServer(ThreadedTCPServer):
        address_family = family
    with TypedServer((bind, port), Handler) as server:
        print(f"监听 TCP {bind}:{port}，仅用于点对点连通性探测。Ctrl+C 停止。", flush=True)
        print("若要测试带宽，还需要在目标设备另行运行 iperf3 -s。", flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


def write_files(args, report):
    if args.json_path:
        file = Path(args.json_path).expanduser()
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("JSON:", file)
    if args.csv_path:
        file = Path(args.csv_path).expanduser()
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("w", encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=("index", "timestamp", "status", "rtt_ms", "details"))
            wr.writeheader()
            wr.writerows(report["samples"])
        print("CSV:", file)


def main(argv=None):
    args = arguments(argv)
    if args.serve:
        run_server(args.bind, args.listen_port)
        return 0
    try:
        family, ip = resolve(args.target, args.port, args.ipv4, args.ipv6)
    except OSError as e:
        print("错误：无法解析目标：", e, file=sys.stderr)
        return 2
    method = args.mode
    if method == "auto":
        method = "syn" if os.name == "posix" and getattr(os, "geteuid", lambda: 1)() == 0 and shutil.which("nping") else "connect"
    if method == "syn" and (not shutil.which("nping") or (hasattr(os, "geteuid") and os.geteuid() != 0)):
        print("错误：--mode syn 需要 nping 和 root 权限。", file=sys.stderr)
        return 2
    print(f"TcpQuality-P2P v{VERSION} | 本机 -> {args.target} ({ip})")
    samples, stats = run_probes(args, family, ip, method)
    report = {"tool": "TcpQuality-P2P", "version": VERSION,
              "time_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "source": socket.gethostname(), "target": args.target, "resolved_ip": ip,
              "family": "IPv6" if family == socket.AF_INET6 else "IPv4",
              "tcp_port": args.port, "probe_mode": method, "stats": stats, "samples": samples}
    if args.route:
        report["route"] = route_trace(ip, args.port, family == socket.AF_INET6)
    if args.speed:
        report["speed"] = speed_test(ip, args.iperf_port, args.seconds, args.max_rate, family == socket.AF_INET6)
    write_files(args, report)
    return 0 if stats["received"] else 1


if __name__ == "__main__":
    sys.exit(main())