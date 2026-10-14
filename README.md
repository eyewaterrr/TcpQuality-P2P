# TcpQuality-P2P

TcpQuality 思路的独立、精简点对点 TCP 质量测试工具。**只测试你指定的一台目标设备**；无需调用全国节点列表、不上传数据。Linux 使用 Python 3 标准库核心，Windows 使用原生 PowerShell 5.1+，均不需要安装 Python 包。

> 当前版本：v0.1.0。默认只向指定目标进行测试，不上传报告到第三方。

## 一行运行（无需安装或克隆仓库）

把 `SERVER_IP` 替换为真实 VPS/本地电脑 IP。目标的 TCP 端口必须可连接；VPS 通常使用开放的 SSH 端口（不一定是 22）。**任何端口拒绝连接都单独列出，不当作实际链路丢包。**

Linux VPS（发起端）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/eyewaterrr/TcpQuality-P2P/main/p2p.sh) SERVER_IP --port 22
```

Windows PowerShell 5.1+：

```powershell
& ([scriptblock]::Create((irm 'https://raw.githubusercontent.com/eyewaterrr/TcpQuality-P2P/main/p2p.ps1'))) -Target SERVER_IP -Port 22
```

建议先阅读源码，再执行远程下载的脚本；如需固定版本，可将 URL 中的 `main` 改为明确的 commit SHA。

对于从 ZIP 解压的未签名 PowerShell 脚本，如果系统执行策略拦截，可使用上面的 `powershell.exe -ExecutionPolicy Bypass -File ...` 写法；它只对该 PowerShell 进程生效，不永久修改系统执行策略。GitHub Raw 一行启动使用内存中的 ScriptBlock，不依赖本地脚本执行策略。

## ZIP 解压后本地运行

Linux：`bash p2p.sh SERVER_IP --port 22`。Windows：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\p2p.ps1 -Target SERVER_IP -Port 22`。

## 进阶测试

Linux：

```bash
# 100 次 TCP 连接探测
bash p2p.sh SERVER_IP --port 443 -c 100
# 强制原版风格的 SYN 回复探测（须以 root 执行且安装 nping）
sudo bash p2p.sh SERVER_IP --port 443 -c 100 --mode syn
# 完整测试（路由+带宽，需对端运行 iperf3 -s）
bash p2p.sh SERVER_IP --port 22 --full
# 限速 30 Mbps，避免消耗大量 VPS 流量
bash p2p.sh SERVER_IP --port 22 --full --max-rate 30M
# 保存本地结果
bash p2p.sh SERVER_IP --port 22 -c 100 --json report.json --csv samples.csv
```

Windows：

```powershell
.\p2p.ps1 -Target SERVER_IP -Port 22 -Count 100
.\p2p.ps1 -Target SERVER_IP -Port 22 -Full -MaxRate 30M
.\p2p.ps1 -Target SERVER_IP -Port 22 -Json report.json -Csv samples.csv
```

### 带宽测试的一次性准备

需在目标设备启动（或持久运行）`iperf3` 服务端，并确保 TCP 5201 可以从发起设备访问：

```bash
# Ubuntu / Debian VPS 上执行
sudo apt-get update && sudo apt-get install -y iperf3
iperf3 -s
```

然后在发起设备运行 `--full`（Windows：`-Full`）。`iperf3 -R` 在已建立的控制连接上反向传输：即使本地 Windows 在 NAT 后面，只要**本地能连接 VPS 的 5201**，通常也能测量 VPS→本地的下载吞吐量。

注意：`--full` 的 iperf3 默认不限速，每个方向 8 秒，可能产生较大流量；用 `--max-rate 30M`（Windows：`-MaxRate 30M`）设置传输速率上限。设置速率上限后，得到的是该上限下的吞吐量，不是链路最大速率。

### VPS 主动测试 Windows 本地电脑

- **有公网 IPv4 / 可路由 IPv6**：将目标设置为本地的可达地址并开放一个测试端口。
- **处于家庭/学校 NAT 后**：推荐先配置 Tailscale，并以 Windows 的 Tailscale IP 作为目标。注意这测的是 Tailscale 路径，可能是直连或 DERP 中继，**不等同于普通公网 IP 直连**。
- Windows 如果没有可供连接的 TCP 服务，可以用 `p2p.ps1` 自带监听器，在 Windows 上运行（最好绑定 Tailscale IP，而不是公网全网监听）：

```powershell
.\p2p.ps1 -Serve -Bind 100.x.y.z -ListenPort 49222
```

在 VPS 上执行：

```bash
bash p2p.sh 100.x.y.z --port 49222 -c 100
```

此监听器不提供测速或认证功能，只用于接收并关闭 TCP 连接；不要将它未经防火墙限制开放到整个互联网。Windows 上可用 `Ctrl+C` 结束。服务器反向带宽测试仍需让 Windows 运行 `iperf3 -s` 并确保网络可达。

Linux 监听器对应命令：

```bash
python3 p2p.py --serve --bind 100.x.y.z --listen-port 49222
```

## 探测含义与限制

| 项目 | Linux | Windows |
|---|---|---|
| 默认 TCP 检测 | `socket.connect` 三次握手 | `.NET TcpClient` 三次握手 |
| 原版风格 SYN 回复探测 | 有 root + nping 自动使用，或 `--mode syn` | 暂不支持裸 TCP SYN |
| 单一目标地址、端口 | 支持 | 支持 |
| 路由 | `traceroute -T`（可选工具） | `tracert`，**ICMP 而非 TCP** |
| 双向单线程吞吐量 | `iperf3`（可选工具） | `iperf3`（可选工具） |
| JSON/CSV | 支持 | 支持 |

- RTT 是往返时延，**无法仅凭一端测试独立还原去程与回程各自的单向延迟或丢包率**。
- TCP connect 失败率受到应用监听状态、目标防火墙和 SYN 限速影响；不能称为真实 TCP 数据包丢失率。端口拒绝（RST）与超时已分开计数。
- TCP SYN 接收到 RST 代表设备有响应，但不代表目标端口可建立应用连接。`nping` 原始风格探测不能替代实际下载/上传重传分析。
- iperf3 的 TCP `retransmits` 通常是发送端统计；反向模式能否返回完整重传统计取决于两端 iperf3 版本与权限。
- 不自动修改防火墙、路由或安装软件，也不会向第三方上传报告。
- 使用 VPN/Tailscale 地址进行测量时，其结果包含 VPN 封装及可能的中继开销。

## 依赖

- Linux：Bash、curl（远程一行启动时）、Python 3.8+。`nping`、`traceroute`、`iperf3` 按需安装，默认探测可在无 root 下运行。
- Windows：Windows PowerShell 5.1 或 PowerShell 7；可选 `iperf3`。不需要 Python 或管理员权限。

## 项目文件

```text
p2p.py              Linux/跨平台核心
p2p.sh              Linux 一行命令下载启动器
p2p.ps1             Windows 原生 PowerShell 实现
README.md           使用说明
LICENSE             GPL-3.0-only
NOTICE.md           来源与许可证说明
tests/test_p2p.py   离线回归测试
.github/workflows/tests.yml   GitHub Actions Ubuntu/Windows 测试
```

## 许可证与归属

项目受 [ibsgss/TcpQuality](https://github.com/ibsgss/TcpQuality) 启发，采用 GPL-3.0-only。具体见 [NOTICE.md](NOTICE.md)。