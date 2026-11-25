# ADS桥接工具

面向 **Windows 上的 Keysight Advanced Design System 2026 Update 0.1（64-bit Simulations）**，通过 Python、PowerShell 和 MCP 自动完成电路设计、ADS 求解与指标验收。支持在 ADS 图形界面中操作当前工作区，也支持无头批处理。

当前版本 **2.3.5** · [使用与接口](#使用与接口) · [版本记录](CHANGELOG.md) · [MIT 许可证](LICENSE)

## 项目优势

| 优势 | 实际收益 |
| --- | --- |
| 同一套工具接入不同 AI | MCP 使用本机标准输入输出（stdio）。Codex、Google Antigravity 等支持本地 MCP 的客户端可调用桥接，Python 和 CLI 可复用相同的作业及数据读取逻辑。 |
| 从设计到验证可重复 | 在脚本中创建原理图、生成网表、修改参数、调用真实 ADS 求解器，再从数据集核对规格。无头模式可批量执行，GUI 模式可在 ADS 内查看原理图和 Data Display。 |
| 网表可读、结果可追溯 | 网表解析返回器件、变量、连接拓扑、控制器和自然语言摘要。ERC、评分卡和切片结果给出可定位的异常，作业 ID 可用于回查日志及原始波形。 |
| 降低 AI 上下文占用 | 注册网表后可复用短句柄并传递参数差分。优先返回关键频点、工程指标和精简日志，必要时分页回读原始数据。三个 MCP 配置提供不同范围的工具。 |
| 便于自动化验收与调参 | 支持 AC、S 参数、HB 和瞬态数据分析，提供逐切片验收、PA 功率与效率指标、灵敏度分析和闭环优化。可用显式容差表达项目的验收标准。 |
| 控制工作区写入 | GUI 可从只读模式启动，写入授权限定于当前工作区。设计操作有事务回滚，无头写入会检查目标是否已由在线 GUI 打开。 |

## 安装

需要 **Windows、Keysight ADS 2026 Update 0.1（含 64 位仿真器）和有效 ADS 仿真许可证**。客户端 Python 需要 **3.11 或以上**。ADS 会用自带 Python 操作其工作区和仿真器。

```powershell
git clone https://github.com/Raven49963/ADS-Bridge.git
Set-Location ADS-Bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[mcp]"
.\.venv\Scripts\ads-bridge.exe status
```

若自动探测不到 ADS，可设置安装目录，再次运行 `status`：

```powershell
$env:HPEESOF_DIR = 'C:\Program Files\Keysight\ADS2026_Update0.1'
.\.venv\Scripts\ads-bridge.exe status
```

`.[rf]` 安装 RF 数据和优化依赖。桥接源码使用 MIT 许可证。Keysight ADS、仿真许可证、第三方器件模型与 PDK 由各自供应方授权，需独立安装。

## 使用与接口

### 1. 运行完整的 RC 示例

```powershell
.\.venv\Scripts\python.exe -B .\verify_closed_loop.py
```

脚本通过 ADS 无头构建和修改 RC 原理图，导出网表、识别 RC 拓扑、运行仿真并与解析解比较。后续步骤还验证 S 参数、批处理、优化、GDSII、Data Display 和 MCP。可使用 [RC 原理图 JSON](examples/rc_autowire.json) 起步。在线 ADS 会话可达时，脚本也会尝试联调在线桥接。

### 2. 从已有网表仿真

使用 ADS 导出的 `.ckt` 或编写的小网表。测试台用 `tb` 模式检查，独立 DUT 用 `dut` 模式。

```powershell
.\.venv\Scripts\ads-bridge.exe netlist-inspect .\my_circuit.ckt
.\.venv\Scripts\ads-bridge.exe netlist-check .\my_circuit.ckt --mode tb
.\.venv\Scripts\ads-bridge.exe simulate .\my_circuit.ckt
.\.venv\Scripts\ads-bridge.exe --help
```

Python 可直接调用同一套功能：

```python
from pathlib import Path

import bridge

job = bridge.simulate_netlist(Path("my_circuit.ckt"))
print(job)
```

完整流程见 [验证脚本](verify_closed_loop.py)。`headless_workflow` 可在一个 ADS Python 进程内连续执行建库、器件内省、建图、改参和导出。写入时传绝对工作区路径并设置 `write=True`，创建测试工作区时加 `create_workspace=True`。

### 3. 接入 AI 客户端

```powershell
.\setup_client.ps1
```

脚本创建或复用外部 Python 环境并输出 `mcpServers.ads_bridge` 配置。Google Antigravity 可在 MCP 管理界面的原始配置中合并该条目。其他支持本机 **stdio MCP** 的客户端按其配置格式填入同一命令和参数。以 **Codex** 为例，在项目的 `.codex/config.toml` 中配置如下，替换两处本机路径：

```toml
[mcp_servers.ads_bridge]
command = 'C:\path\to\ADS-Bridge\.venv\Scripts\python.exe'
args = ['-B', 'C:\path\to\ADS-Bridge\mcp_server.py', '--profile', 'headless']
```

如已配置 `HPEESOF_DIR` 或 `ADS_BRIDGE_RUNTIME`，确保客户端启动的子进程和 ADS 使用相同设置。编辑配置后重载客户端，先调用 `bridge_status`，再尝试 `component_describe` 或 `simulate_netlist`。连接已打开的 GUI 时使用 `full` 配置，并从 `session_list` 获取会话。

| 配置 | 工具数 | 适用任务 |
| --- | ---: | --- |
| `full` | 55 | GUI、无头、仿真作业、全部设计与 RF 工具 |
| `headless` | 19 | 无头设计、网表、批量仿真及 RF 分析 |
| `rf-core` | 13 | 网表句柄、评估、优化和压缩，减少发送给 AI 的工具定义 |

### 4. 连接在线 ADS

在 ADS 主窗口的 **Tools → Python Console** 执行一次注册：

```python
import sys

sys.path.insert(0, r"C:\path\to\ADS-Bridge")
import install_addon

install_addon.install()
```

从 **Tools → ADS Bridge** 启动桥接。先选择 **Start read-only** 检查现有原理图与生成网表，需要改图时在测试工作区使用 **Start with write access to this workspace**。用 CLI 的 `sessions` 或 MCP 的 `session_list` 查看在线会话，并使用 `session_id` 与 `workspace_id` 定位设计。GUI 模式可打开原理图、Data Display 报告及抓取窗口图像。

## 工具与工作流索引

| 任务 | 常用操作 |
| --- | --- |
| 查本机环境、文档与器件 | `bridge_status`、`docs_search`、`pdk_list`、`pdk_search`、`pdk_describe`、`component_describe` |
| 建库、原理图、修改连线和参数 | `library_create`、`design_list`、`design_read`、`design_build`、`design_update`、`design_check` |
| 生成、理解、检查及修改网表 | `netlist_generate`、`netlist_inspect`、`netlist_check`、`netlist_register`、`netlist_patch`、`netlist_compress` |
| ADS 求解及作业管理 | `simulate_netlist`、`simulate_batch`、`simulation_submit`、`simulation_status`、`simulation_cancel`、`simulation_result`、`simulation_log_summary` |
| 数据集回查与分析 | `dataset_describe`、`dataset_trace`、`dataset_table`、`dataset_analyze`、`dataset_compress`、`touchstone_compress` |
| 规格验收与设计迭代 | `design_eval_step`、`sensitivity_scan`、`optimize_netlist` |
| 版图与报告 | `layout_build`、`layout_read`、`layout_export_gds`、`dds_build`、`window_open` |
| GUI 与跨客户端交接 | `gui_launch`、`session_inspect`、`session_capture`、`request_get`、`request_reconcile` |

MCP 工具参数见 [工具定义](mcp_server.py)，Python SDK 和 CLI 见 [入口代码](bridge.py)。[双客户端验收指南](ANTIGRAVITY-ACCEPTANCE.md) 给出如何利用会话和作业 ID 交接数据。

### 规格验收建议流程

1. 对网表运行 ERC，确认器件端子、参考地、分析控制器和外部模型路径。
2. 运行仿真后通过数据目录确定 **block、axis、signal**。RF 功率需要明确负载，PAE 还需要输入功率和直流供电参数。
3. 用 `design_eval_step` 执行改参、检查、求解和评分。逐行查看 `scorecard` 与总判定 `all_pass`，扫参时查看各切片。需要验收指定切片时传 `filters`。
4. 对 `eq` 规格可传 `tolerance` 指定测量单位下的绝对容差。例如 `pout_w` 的目标为 `0.01`、容差为 `0.0002`，表示 10 mW ±0.2 mW。若使用 dBm，应按 dB 量纲定义门限。
5. 需要保留可复核证据时，记录网表、规格、ADS 版本、作业 ID 和关键频点原始数据。先跑灵敏度扫描再使用优化器，可以判断哪些参数值得调整。

## 测试与排查

```powershell
# 离线单元测试
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v

# 在线工作区测试，请使用已授权写入的临时 ADS 工作区
.\.venv\Scripts\python.exe -B .\verify_live.py "your-session-id" "C:\path\to\test_wrk"
```

- **找不到 ADS 或仿真许可证**：检查 `status`、`HPEESOF_DIR` 和 ADS 许可环境。
- **找不到在线会话**：确认插件已启动，ADS 和客户端指向同一运行目录（默认用户目录下的 `.ads_bridge`）。用 `sessions` 检查。
- **仿真失败**：查看作业状态、`simulation_log_summary` 或完整日志，核对模型目录、控制器和收敛设置。
- **数据为空或存在多维扫描**：先用 `dataset_describe` 确认分析块、轴和信号，在选定外层坐标后读回原始数据。

架构参考 [Arcadia-1/virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite) 的多入口与可恢复工作流。欢迎通过 GitHub Issues 提交问题，附上 ADS 版本、最小网表或示例以及相关报错。
