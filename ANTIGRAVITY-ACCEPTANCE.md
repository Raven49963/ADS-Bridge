# ADS Bridge 1.0 双客户端交接验收指南

本指南用于验证主客户端与交接客户端（如 CLI）之间通过 ADS Bridge 1.0 完成状态确认与仿真结果读取。请全程使用**你自己的实际 `session_id`、`job_id` 与 `challenge`**，切勿直接复用他人示例 ID。

本页由 Google Antigravity 在隔离目录编写，Codex 核对了工具名、返回值和验收命令。

## 1. 本机配置与权限隔离

- **项目级配置**：将 MCP 服务配置放在当前项目的 `.agents/mcp_config.json`，通过本机 `stdio` 启动，并设置环境变量 `ADS_BRIDGE_RUNTIME` 指向实际运行目录。
- **验收权限**：本次非交互联调只开放 `dataset_trace`。在服务配置中将 `enabledTools` 设为 `["dataset_trace"]`，再用 `agy --settings verify-settings.json -p "…"` 传入以下单次权限。工作区配置和单次权限均保留在当前项目。 [Antigravity 工具权限](https://antigravity.google/docs/permissions/)

```json
{"permissions":{"allow":["mcp(ads_bridge/dataset_trace)"]}}
```

## 2. 双客户端交接验收步骤

1. **会话核验**：主客户端提供实际 `session_id` 与 `challenge`，交接端调用 `session_inspect(session_id, challenge)` 确认会话归属与通道连通。
2. **任务状态确认**：调用 `simulation_status(session_id, job_id)`，确认目标仿真任务已完成，并记录对应作业状态。
3. **数据集结构核对**：调用 `dataset_describe(session_id, job_id)`，确认存在目标数据块、坐标轴与变量名。
4. **轨迹抽样读取**：在受限 CLI 中调用 `dataset_trace(session_id, job_id, block, axis, variable, offset, limit)` 分页读取数据。

## 3. 合成基准验收标准

使用合成基准参数 `block="SP1.SP"`、`axis="freq"`、`variable="S[2,1]"`（可设 `offset=0, limit=3`）进行比对：

| 频率 (`freq`) | 理论基准值 (`S[2,1]`) | 验收误差阈值 |
| --- | --- | --- |
| 100 MHz | `2/3 + 0j` (≈ `0.666667 + 0j`) | 实部与虚部绝对误差 ≤ `1e-6` |
| 200 MHz | `2/3 + 0j` (≈ `0.666667 + 0j`) | 实部与虚部绝对误差 ≤ `1e-6` |
| 300 MHz | `2/3 + 0j` (≈ `0.666667 + 0j`) | 实部与虚部绝对误差 ≤ `1e-6` |

- 若三个频点均满足 `|Re - 2/3| <= 1e-6` 且 `|Im| <= 1e-6`，判定数值验收通过。

## 4. ADS 会话停止后的结果读取区分

- **会话运行中**：`session_inspect` 应回显挑战并返回 `in_ads_gui=true`、`on_gui_thread=true`。通过 `simulation_status` 查询作业状态。
- **ADS 会话已停止**：运行目录和 ADS 数据集解析环境仍可用时，凭已有 `session_id` 与 `job_id` 通过 `dataset_describe`、`dataset_trace` 读取已完成作业。中断且丢失 worker 的作业可返回 `outcome_unknown`。先查日志再决定是否重新提交。

## 5. 误差超限与超时排查

- **误差超限（> `1e-6`）**：核对 `block`、`axis`、`variable` 是否误选其他端口或数据块，确认频率单位（Hz/MHz）及 `offset`/`limit` 切片未错位。
- **调用超时**：检查 `.agents/mcp_config.json` 中 `stdio` 启动命令与 `ADS_BRIDGE_RUNTIME` 路径是否正确；排查本机是否有残留进程阻塞标准输入输出；调小 `limit` 分页重试，并确认 CLI 已正确放行 `mcp(ads_bridge/dataset_trace)`。
