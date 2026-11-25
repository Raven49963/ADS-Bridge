# 版本记录

## 2.3.5 尺度无关等值容差与边界数值全面加固（修复小目标 `pout_w == 0.01` 误判 `PASS`）

- **尺度无关默认 `norm` 与等值容差（`rf_optimizer._evaluate_cost`）**：移除 `max(abs(target), 1.0)` 下限钳位——对任意非零目标（如 `pout_w == 0.01 W`、`pout_w == 1e-6 W`、`mag_at == 0.05 V`）默认按 `norm = abs(target)` 以精确 **±2% 相对容差**判定，使 `pout_w == 0.01`（`10 mW`）与 `pout_dbm == 10` 实测 `1 mW (0 dBm)` 一致判为 **`FAIL`**；零目标按物理量纲区分（dB/百分比用 `±0.02`，线性功率/电压/频率用 `1e-9`），并原生支持显式 `tolerance` 参数。
- **同源边界全面加固（防止引入新误判）**：
  - `le`/`ge` 容差由固定 `1e-9` 改为相对浮点容差 `1e-12 * max(|target|, 1e-18)`（杜绝 `0 W` 误过 `pout_w >= 1e-9 W`）；
  - 非有限实测值（`NaN`/`Inf`）在所有操作符（含 `min`/`max`）中一律判 `satisfied=False`；
  - `_validate_objectives` 前置拦截非正/非有限 `norm`、负 `tolerance`、`load_ohm <= 0`、`at_hz < 0` 与非有限 `target`；
  - `measured`/`margin` 改用有效数字感知展示 `_display_float`，不再把 `1e-7 W` 或微小裕度截断成 `0.0`，`target_str` 升级至 12 位有效数字。

## 2.3.4 扫描切片行名无损唯一化与按行独立评分（杜绝同名覆盖误判 `all_pass=true`）

- **高精度行名 + 冲突强制消歧（`rf_optimizer.evaluate_specs_scorecard`）**：扫描值格式化由 6 位（`:g`）提升至 12 位有效数字（`:.12g`），使 `1000.001 Ω` 与 `1000.004 Ω` 分别清晰展示为 `gain[R_val=1000.001]` 与 `gain[R_val=1000.004]`；同时预先保留全部手工规格 ID 并维护全局已用 ID 集合，凡出现与手工规格 ID 或其他切片 ID 重名（例如高精度仍重合或手工 ID 恰为 `gain[R_val=1000]`），自动附加 `#<index>` 消歧，数学上杜绝任何键名覆盖。
- **按行独立求值与一致性双保险**：评分卡改为逐行独立计算代价与 `satisfied` 状态，不再通过共享 `dict[id, val]` 汇总后二次覆盖，`all_pass` 严格等于全部评分行与全部切片统计的一致通过。

## 2.3.3 多维扫描全切片严格验收与坐标去重守卫（禁止单切片误判 `all_pass=true`）

- **全指标统一多维/重复坐标硬守卫（`rf_optimizer.py`）**：在 `extract_objective_metric` 入口（早于 `db20_at`/`mag_at`/`s_db_at` 等所有分支）与 `_interp_at` 中强制拦截未过滤的多维 `outer_axes` 及重复/非单调频率/时间坐标，彻底杜绝 `searchsorted` 任取某一扫描点误报 `all_pass=true` 的缺陷。
- **`design_eval_step` 多维扫描全切片自动验收与 `filters` 定向切片（`worker.py` / `rf_workflow.py` / `rf_optimizer.py` / `bridge.py` / `mcp_server.py`）**：
  - `specs`/`objectives` 与 `design_eval_step` 均支持可选 `filters`（定向验收指定扫描点，如 `filters={"R_val": 6000}`）；
  - 当 `design_eval_step` 执行 2D `ParamSweep` 且未指定 `filters` 时，常驻 worker 单次按扫描轴切分全部切片并逐一评分，**全部切片均达标才返回 `all_pass=true`**，任一切片不达标即判 `all_pass=false` 并在 `scorecard` 与 `sweep_evaluation` 中逐点列出失败切片（例如 5 电阻 RC 扫描中 2 kΩ `-22.01 dB` / 4 kΩ `-28.01 dB` 均标 `FAIL`）；配套压缩摘要亦自动走 `compress_sweep_slices`。

## 2.3.2 评分卡与优化器物理效率上限硬校验（禁止 >100% PAE 误判 PASS）

- **数据摘要与评分卡共用热力学效率校验（`rf_compress.check_pa_efficiency_warnings` / `MAX_PHYSICAL_EFFICIENCY_PERCENT`）**：`pa_metrics` 显式输出 `physically_valid` 布尔字段，并将效率异常判定阈值由 `100.5%` 收紧至物理上限 `100%`（浮点容差 `1e-3%`）。
- **评分卡、评估宏与优化器双层拒收（`rf_optimizer.py`）**：
  - `extract_objective_metric` 对任何 `pae_percent > 100%`、`drain_eff_percent > 100%`（含 `drain_eff > 100%` 但因大 `Pin` 掩盖使 `PAE` 看似 `< 100%` 的情况）或 `physically_valid=False` 的测量直接抛 `ValueError` 拒绝验收（`optimize_netlist` 同步记为失败评估，绝不把 `399% PAE` 采纳为最优解）；
  - `_evaluate_cost` 同步增加第二层防线：即便直接传入超 `100%` 效率值亦强制 `satisfied=False`、`physical_violation=True` 并施加高额惩罚代价；
  - `_validate_objectives` 在仿真前即拒绝 `target > 100%` 的非法效率规格。

## 2.3.1 全流程实战复现 13 项根因修复与 2D 扫描批量加速

基于使用 `2.3.0` 从零复现 Class-E 自适应偏置功放论文全流程中暴露的 13 项真实缺陷，完成 P0/P1/P2 全量根因修复：

- **2D `ParamSweep` 数据安全与单次批量压缩（`worker.py` / `rf_compress.py` / `bridge.py`）**：`extract_block` 自动识别未过滤的外层扫描轴（`outer_axes`）并在 `compress_waveform_block` 中直接拦截（杜绝把多组 `Pin_dBm` 混并后算出 `7,193,110%` THD 的数据失真）；新增 `extract_sweep` + `dataset_compress(..., sweep_axis="Pin_dBm", pin_from_sweep_axis=True)`，单次打开数据集即可压缩整条 2D 扫描曲线（免去 N 次冷启动）；`dataset_compress` 同时支持 `(session_id, job_id)` 与直接传入 `.ds` 路径。
- **自适应偏置 PA 真实 `vds_signal` 与物理合理性告警（`rf_compress.py` / `rf_optimizer.py`）**：支持传入 `vds_signal="VDS"` 直接从仿真直流分量读取动态漏极电压（不再被迫硬编码静态 `vds_v`）；当 `drain_eff_percent` 或 `pae_percent` 超过 `100.5%` 时自动产出 `pa_metrics.warnings`。
- **优化器高精度保留、前置校验与操作符别名（`rf_optimizer.py` / `rf_workflow.py` / `bridge.py`）**：`best_parameters[k]["value"]` 改用 10 位有效数字（修复 pF/nH SI 值被 `round(..., 8)` 截断成 `0.0` 的严重缺陷）；支持 `==`/`<=`/`>=` 操作符别名与 `rload_ohm`/`vdc_signal`/`idc_signal` 字段别名；`design_eval_step`、`optimize_netlist`、`sensitivity_scan` 在启动 ADS 前毫秒级预校验 `specs`/`objectives`；`design_eval_step` 新增 `signals` 白名单过滤并返回 `dataset`/`session_id`；`optimize_netlist` 同步输出 `all_pass` 与 `all_satisfied`。
- **线段避障正交布线与原理图细节修复（`design_ops.py`）**：`route_orthogonal` 升级为引脚 + 既有导线线段双避障（防止共线重叠与 T 型误短接）；修复实例名为 `PIN` 时撞上顶层端口语法的问题；`wires` 兼容 `"net"` 作为 `"label"` 别名；新建 `VAR` 块自动清理 ADS 模板幽灵变量 `X=1.0`；补齐 `SDD1P` 极性提示并清洗 `<ParamItemConst ...=form(...)>` 默认值字符串。
- **分析剥离、PDK 缓存与全入口接线（`netlist_parser.py` / `docs_pdk.py` / `worker.py` / `ads_addon.py` / `mcp_server.py` / `bridge.py`）**：`strip_analysis_controllers` 联动清除关联 `ATAHB`/`ModelExtractor` 伴生语句；`pdk_search` 增加进程内索引缓存并在返回项中直出 `master`；全链路接通 `component.describe(..., compact=True)` 与 `library`+`cell` 入参；`headless_workflow` 支持 `workspace_path=None`（只读内置库查询）、`continue_on_error=True` 及带 `[step i: op]` 索引的清晰报错；`simulate_netlist` 新增 `save_to_data_dir=True`；`netlist_compress` 新增 `store_handle=True`；`job_log_summary` 默认 `STANDALONE_SESSION`；缺失 `numpy`/`scipy` 时返回可读 `BridgeError("dependency_missing", ...)`。

## 2.3.0 性能提速与防返工加固（常驻仿真池 + 极性/重叠/日志全链路优化）

- **常驻 ADS Python 仿真池（`worker.py serve` + `rf_workflow.PersistentSimPool`）**：`optimize_netlist`、`sensitivity_scan` 与 `design_eval_step` 不再每轮冷启动两次 ADS Python 进程，而是复用常驻工作进程同进程完成仿真与 `.ds` 块提取（带 `__ADS_BRIDGE_JSON__:` 帧前缀隔离 C++ 噪声），大幅消除多轮优化与灵敏度扫描卡顿。
- **故障日志缩进详情保留（`distill_simulation_log`）**：自动合并 `hpeesofsim` 错误/警告行后的缩进根因（如 `No convergence.`、`Number of nodes with no DC path to ground`），无需再人工翻原始 `sim.log`。
- **原理图防返工体验（`design_ops.py`）**：`component.describe` 为 `V_DC`、`P_1Tone`、`Diode`、`MOSFET_NMOS`、`SDD2P`/`SDD3P`、`I_Probe` 等常用器件输出 `polarity_hint` 与端子 `role`，并支持 `compact=True` 精简输出；`design.check` 新增实例包围盒重叠告警（`overlapping_instances`）；`design.update` 的 `add_instance` 同时兼容平铺与嵌套 `instance` 结构。
- **僵尸会话自动清理与一键前台拉起（`bridge.py` / `mcp_server.py`）**：`list_sessions()` 基于 Win32 `OpenProcess` 毫秒级清理已退出进程的僵尸会话文件；新增 `bridge.gui_launch` / MCP `gui_launch` 以 `DETACHED_PROCESS` 脱离启动前台 ADS 2026、自动连上在线桥接并打开指定原理图与 `.dds` 报表；`netlist_parser.strip_analysis_controllers` 安全移除多行 `\` 续行分析卡；`install_addon.py` 在脱离 ADS GUI 环境下友好提示而非抛 `ImportError`。

## 2.2.2 独立复查 5 项物理校验与边界修复

- **统一分析类型与信号量纲校验（`rf_compress.py` / `rf_optimizer.py`）**：
  - `is_sparam_block` 与 `is_sparam_signal_name` 供压缩与评分卡共用，对 AC 电压信号（如 `VOUT`）请求 `s_db_at` 等 S 参数指标立即报错拒收，同时保持支持单条合法 `S[1,1]` 曲线评估。
  - 移除 `vout_signal` 对 `_is_current_signal` 的覆盖：凡 `.i` 电流探针（如 `VOPA.i`、`IDS.i`）在摘要与评分卡中均严格保持 `quantity="current"`，拒绝 `pout_w`/`pout_dbm`/`pae_percent` 评分；并校验 AC/HB 专属指标不得跨分析类型误用。
- **缺样摘要与二次扫描加固（`rf_compress.py` / `netlist_parser.py`）**：
  - `matched_bw_hz` 在完整带内频点网格上计算，缺失频点直接断开连续匹配区间而不再桥接；
  - HB 高次谐波存在缺失时 `thd_percent` 返回 `null`（未知）而不再误报 `0%`；
  - `inject_sweep_plan` 对已含 `SW_BRIDGE_CTRL`/`SW_BRIDGE` 的网表先清理旧桥接扫描控制器与计划再写入新范围，杜绝重复定义报错。

## 2.2.1 独立复查缺陷修复

- **规格与优化器严格校验**：禁止重复的规格/目标 ID（避免后者静默覆盖前者导致误判 `PASS`）；缺失或非有限复数分量（`null`/`NaN`/`Inf`）在解码、插值、压缩与目标提取中全程传播为 `NaN` 并在验收点直接报错，杜绝置零伪造 `-360 dB` 陷波。
- **频段隔离与物理量纲准确性**：`band_hz` 严格限定 `matched_bw_hz`、`-3 dB` 交点、群时延与双端口稳定性在带内计算，若目标频段无采样直接报错而不回退全频段；HB 省略 `pin_dbm` 时 `pae_percent`/`gain_db` 返回 `null` 而只报告漏极效率；电流探针（如 `IDS.i`）标记为 `current` 且不再套用电压公式换算 `dBm`；`AC` 块优先按分析类型分类（纯电阻分压器与零起点线性扫频不再误判为瞬态或 HB）。
- **多行续行网表与批量分组**：`inject_sweep_plan` 对 AC/SP/HB 的参数扫描原生注入 `ParamSweep`（实测输出数据集同时保留 `R_val` 与 `freq` 双轴且不破坏 `\` 续行）；`apply_netlist_parameters` 支持 `\` 续行语句、严格隔离行尾注释并回读重解析验证；`sensitivity_scan`（最多 16 参/33 作业）与 `grid_refine` 自动按 32 作业上限分批保序执行。
- **Wheel 打包完整性**：`pyproject.toml` 声明 `packages = ["addon_entry", "examples"]` 及 `examples/*.json`，从 `.whl` 安装后亦可直接注册 ADS Addon 与运行闭环验收。

## 2.2.0 升级（极致降 Token 工作流 + 模块化架构）

- **内容寻址网表句柄与列式超紧压缩（[rf_workflow.py](rf_workflow.py) / [netlist_parser.py](netlist_parser.py)）**：新增 `netlist_register`（返回 `nl_<12hex>` 短句柄与 `format="tabular"` 极简列式拓扑/参数表，消除重复 JSON 键名）与 `netlist_patch`（仅返回新句柄与参数差分 `delta`）；所有仿真、检查、优化和压缩接口均可直接传入 `nl_...` 句柄，网表正文零往返。
- **单轮原子评估宏工具（`design_eval_step`）**：一轮工具调用内串行执行「改参 → 静态 ERC 守卫 → 64 位 ADS 求解 → 规格判定表（`Spec Scorecard`）」，将原本 5–6 轮对话压缩为 1 轮。
- **并发参数灵敏度排序（`sensitivity_scan`）**：在桥接层并发执行 `±step_pct` 局部扰动仿真，返回按加权影响排序的参数排行榜与单位百分比变化斜率（`delta_per_1pct`）。
- **仿真日志蒸馏（`simulation_log_summary` / `distill_simulation_log`）**：剥离 `hpeesofsim` 版权与进程横幅，对 `ERROR/WARNING` 去重计数并输出可执行修复提示（`< 300` tokens 故障卡）。
- **MCP 工具目录分级瘦身（`--profile` / `ADS_BRIDGE_MCP_PROFILE`）**：支持 `full`（全部 54 个工具）、`headless`（19 个无头工具）与 `rf-core`（13 个极简核心 RF 工具，每轮工具 Schema 开销降低 **~76%**）。

## 2.1.0 升级（网表压缩 + `scikit-rf` 仿真数据压缩 + 桥接层自动参数优化）

- **网表语义压缩（`netlist_compress`）**：剥离 ADS 冗余显示/选项/版图元数据，仅向 LLM 输出紧凑的电路拓扑、变量表达式与可调参数引用（`VAR:*` 与 `Instance.Param`），并附字节/Token 压缩比统计；配套 `apply_netlist_parameters` 与 `inject_sweep_plan`（`simulate_netlist(..., sweep=...)`）直接驱动参数化网表与 `SweepPlan` 扫描。
- **射频仿真数据压缩（`rf_compress.py` / `dataset_compress` / `touchstone_compress`）**：基于 **Python + NumPy + scikit-rf (`skrf`)** 在桥接层完成数据特征提取，模型只接收紧凑电路指标 JSON：
  - N 端口 S 参数自动构建 `skrf.Network`，输出无源性（最大奇异值）、互易性、Rollett 稳定因子 $K/\Delta/\mu$、最小/最大回波损耗与插入损耗、VSWR、匹配带宽、最佳匹配点复阻抗 $Z$、群时延，并支持可选导出标准 Touchstone `.sNp` 文件与离线 `.sNp` 压缩；
  - AC / HB / 瞬态波形自动提取幅值、`dB20`、`-3 dB` 截止频率、谐波/THD 与自适应 LTTB 极值保留降采样曲线（严格保留首尾、最大/最小峰值点）。
- **桥接层自动参数优化（`rf_optimizer.py` / `optimize_netlist`）**：无需 LLM 逐轮手调网表，直接在桥接层闭环驱动 64 位 `hpeesofsim` 与 `scipy.optimize`（`nelder_mead`、`powell`、并行 `grid_refine` 及纯 NumPy 兜底），支持线性/对数归一化有界空间、评估缓存、多目标加权代价函数（`cutoff_3db_hz` / `db20_at` / `mag_at` / `ripple_db` / `s_db_max` / `s_db_min` / `s_db_at` / `vswr_max` / `matched_bw_hz` / `rollett_k_min` / `pout_dbm` / `pout_w` / `pae_percent` / `drain_eff_percent` / `h2_dbc` / `thd_percent`），仅返回最优参数与压缩指标 JSON，并将最优网表落盘。
- **工程体验加固**：`design_ops.route_orthogonal` 升级为避障正交布线（自动避让其他器件引脚坐标，降低意外短接风险）；新增 `dds_build` 原生 `.dds` 报表生成与 `window_open`（在线 GUI 打开指定原理图/版图或 `.dds`）；MCP `stdio` 服务端扩展至 **49 个工具**。

## 2.0.3 修订

- `design.build`、`design.update`、`design.edit` 和 `design.add_instance` 接受 ADS 复合参数的完整数组。单值用字符串，普通多值用字符串数组，SDD 的多组复合方程用二维数组。一组标量重复参数仍可用字符串。数组须与该元件当前的组数和每组字段数一致，新增实例可先用 `component.describe` 查看 `default` 和 `input_shape`，已有实例可用 `design.read` 回读当前值。
- 形状不匹配时返回 `invalid_parameter_shape`。完整参数组写入后，在 ADS 事务提交前回读核对，读写不一致时返回 `parameter_readback_mismatch`。增量写入失败会回滚同一事务内的更改，替换原理图失败会保留原图。
- `design.check` 和 `netlist-check` 检查 ADS SDD 每个端口是否具有至少一条非空的 I/F 方程，以及方程端口号是否超出节点对数。校验通过后还需运行 ADS 求解并用数据集验收物理指标。

例如，三端口 SDD 的 `set_param` 操作可以传入以下参数。每行是端口号、权重编号和 ADS 表达式。

```json
{
  "op": "set_param",
  "instance": "OPA548",
  "parameters": {
    "SDD": [
      ["1", "0", "(_v1-20*_v2)/0.15"],
      ["2", "0", "0"],
      ["3", "0", "0.011+(20*_v2-_v1)/0.15"]
    ]
  }
}
```

## 2.0.2 修订

- 修复异步仿真取消与超时终止竞态：子进程 PID 独立写入 `worker.pid`，取消操作先行写入 `cancel.flag`，消除 `worker.py` 启动阶段无 PID 可杀以及状态回写覆盖 `cancelled` 的 TOCTOU 竞态；同步统一在线 GUI `AdsBridge.submit` 与独立仿真的模型上下文重试校验。
- 修复 `parameters` 混合语法与网表 ERC 边界：正确解析带单位默认值后紧跟无默认值参数（如 `parameters R1=10 Ohm R2`）；补齐顶层子电路实例参数引用检查、重复端口号 `duplicate_port_number` 检查、子电路内部器件重名/自短路/非正物理量检查；统一顶层与子电路对外部 `#include` 声明的解析与告警策略，并将多调用方作用域传播改为活动调用链交集。
- 修复波形越界采样、双端口映射与闭环验收：`dataset.analyze` 对越界 `at` 采样点显式返回 `in_range=false`、`status="out_of_range"`（原复数值置 `null` 并保留 `clamped_endpoint` 供参考）；双端口 S 参数摘要严格按 `Port` 的 `Num=1/2` 映射物理端口；`component.describe` 自动释放临时只读符号句柄；默认运行目录由 `%LOCALAPPDATA%\AdsBridge` 改为用户私有目录 `~/.ads_bridge`（消除 Windows 应用商店版 Python 对 `%LOCALAPPDATA%` 的 MSIX 虚拟化隔离导致 Win32 ADS 子进程不可见的问题）；`verify_closed_loop.py` 严格校验 `2.x` 在线插件版本并验证 `2.0` 专有操作。

## 2.0.1 修订

- 独立仿真用同一个 `request_id` 重试时，会同时比较网表、采样请求、数据目录和有序模型搜索目录。参数不同会返回 `request_conflict`。在原路径更新模型文件内容后，使用新的请求 ID 提交。
- `netlist-check --mode tb` 要求有实际的求解分析控制器，扫频计划和输出计划仍可在解析结果中查看。子电路检查考虑本地参数、变量及可见的上层声明。外部包含文件中的声明由求解器解析，静态检查将其可能提供的未解析引用列为警告。
- CLI 在网表检查失败或仿真未成功时返回退出码 1，标准输出仍保留原有 JSON。通过 `job` 查询失败作业时，退出码代表查询是否成功。
