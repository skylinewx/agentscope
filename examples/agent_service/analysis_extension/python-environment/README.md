# Python 分析运行环境

Agent 工作区继续使用 Local。所有 AnalysisPython（包括未 import 第三方库的脚本）都在同一套隔离策略下执行，每次创建新容器。Calculate、排序、原生公式及统计算子不依赖 Docker。

部署时在 `examples/agent_service` 执行：

```powershell
python -m analysis_extension.execution_environment --prepare
```

此命令使用固定基础镜像、锁定依赖和当前执行包装器构建镜像，并输出镜像 ID。业务请求只检查和使用镜像，不自动安装、不回退宿主；修改包装器或依赖后重新执行准备命令。镜像 ID、包装器版本、数值规则版本都进入执行记录和新请求身份。历史报告保持原版本。

## 8 核 / 16 GiB 部署建议

以下均为服务进程环境变量，默认值即此配置：

这是面向 8 核 / 16 GiB 的初始配置。当前本机真实验证环境为 4 核、约 8 GB；已验证额度收缩、排队和清理，目标主机的吞吐与峰值资源仍须实测。

| 环境变量 | 默认值 | 含义 |
| --- | ---: | --- |
| ANALYSIS_PYTHON_MEMORY_BYTES | 2147483648 | 单任务 2 GiB 内存上限，包含临时内存文件 |
| ANALYSIS_PYTHON_CPUS | 2 | 单任务 CPU 上限 |
| ANALYSIS_PYTHON_MEMORY_BUDGET | 8589934592 | 分析执行总预算 8 GiB |
| ANALYSIS_PYTHON_CPU_BUDGET | 6 | 分析 CPU 总预算 |
| ANALYSIS_PYTHON_MAX_PARALLEL | 3 | 全局最多同时执行 3 个 |
| ANALYSIS_PYTHON_SESSION_PARALLEL | 1 | 同会话同时执行上限 |
| ANALYSIS_PYTHON_QUEUE_CAPACITY | 32 | 排队上限 |
| ANALYSIS_PYTHON_QUEUE_SECONDS | 30 | 排队超时，排队期间不启动容器 |
| ANALYSIS_PYTHON_PREPARE_SECONDS | 15 | 环境检查或容器创建阶段超时 |
| ANALYSIS_PYTHON_CLEANUP_SECONDS | 10 | 单次清理命令超时 |
| ANALYSIS_PYTHON_LOG_BYTES | 1048576 | 保留日志上限，溢出继续排空并标记 |
| ANALYSIS_PYTHON_TMP_BYTES | 67108864 | 临时文件内存上限 64 MiB |
| ANALYSIS_PYTHON_PIDS | 64 | 子进程和线程总数上限 |
| ANALYSIS_PYTHON_LIBRARY_THREADS | 1 | BLAS 等库内部线程数 |

Docker 引擎实际资源不足时，总预算按实际 CPU 与内存收缩；内存至少预留 2 GiB。不能容纳单任务配置时明确失败。增大物理内存不等于加速单次计算，必须结合排队时间、启动时间、传输量和真实运算时间评估。

### 会话存储容量

Python 执行额度不包含 Redis、应用服务及其他常驻容器。Redis 保存会话和事件，完整材料增多时也要独立核对 `used_memory`、`maxmemory`、容器上限和持久化开销。保留 `noeviction`，不要用自动淘汰历史会话来掩盖容量不足；达到上限会拒绝写入，不能当作模型或预算数据错误。

2026-11-16 本机回归曾达到 Redis 384 MiB 的上限。现场将容器限制从 512 MiB 调到 1.5 GiB、Redis `maxmemory` 调到 1 GiB，未清理历史。该数字是此次开发机设置，不是所有部署的固定要求。`CONFIG SET` 只修改运行时配置；持久部署应将 `maxmemory`、`maxmemory-policy noeviction` 写入现有 Redis 配置或启动参数，并保留既有认证、数据卷及持久化设置。当前开发容器没有配置文件，此次 `maxmemory` 调整在 Redis 重启后需要重新应用；Docker 容器内存限制本身已保存。

`ANALYSIS_RUNTIME_ROOT` 默认是服务目录下的 `data/analysis-runtime`。同一 Docker 主机上的服务 worker 必须使用同一共享目录及一致的资源配置，SQLite 统一登记排队和额度。跨主机部署每台单独预算；本实现不提供跨主机调度器。此目录需要持久化，并只允许宿主服务账号访问，不挂载给执行容器。

## 数据与恢复

容器无网络、非 root、根目录只读、无宿主挂载，不提供 Docker socket 或服务密钥。只允许临时目录写入，所有进程随任务容器清理。

宿主按授权引用读取网格，以有界消息流发送。发送前记录 PENDING，传输完成后记录 SENT；只由宿主提交输入台账。`scanComplete` 与 `sendComplete` 不能证明算法使用全部记录或业务正确；输出仍接受来源、逻辑和交付核对。模型看到摘要，完整块记录保存在宿主。

摘要以 `inlineCount` 和“本次冻结的全部非网格输入”说明内联范围，不重复展开完整引用列表；引用仍保存在执行记录和宿主台账。每次执行的游标重放及预读缓存合计限制为 256 MiB 的编码大小，关闭游标立即释放缓存，避免同时打开多个大单元格来源而累积宿主内存；这不改变原网格或单条记录允许的容量。

失败返回 `retryInput.retryOfRunRef`；修代码时复用该次完整冻结输入，新尝试保留父运行引用。输入变化会使依赖结果失效，不能仅凭模型声明的窄依赖绕过核对。

容器未确认删除时保留资源额度并标记 CLEANUP_PENDING。服务后台每 30 秒检查过期任务和受本运行时标签管理的容器；宿主重启后继续清理。过期 RUNNING 记录转为失败并保留输入与中断台账，不伪造成功；最长执行期限前不会误回收其他 worker 的任务。后台清理失败不影响普通工具，下次继续重试。

## 验证

主机协议及业务测试使用显式的测试子进程后端，仅运行固定合成脚本，不能证明隔离安全。真实容器测试必须单独启用：

```powershell
$env:RUN_ANALYSIS_RUNTIME_TESTS = '1'
$env:RUN_ANALYSIS_LIBRARY_TESTS = '1'
python -m unittest discover -s tests -p 'analysis_isolated_execution_test.py'
python -m unittest discover -s tests -p 'analysis_python_libraries_test.py'
```

测试覆盖普通语言/Decimal 类型、半入舍入、网格来源、无网络/无挂载/只读/非 root、取消和超时、子进程清理、日志上限、内存受限、伪造协议、重启清理，以及 pandas/NumPy/科学库的历史能力。资源超限可能由 OOM 或超时终止，必须确认任务失败、额度释放和后续任务可执行。
