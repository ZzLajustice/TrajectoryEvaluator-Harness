# 任务 12：`LocalExecutor`（Windows 进程树处理）

> **所属里程碑**：M2 · **前置任务**：任务 5（`Executor` 协议） · **代码位置**：`src/harness/core/executors/base.py`、`local.py`、`docker.py`（stub）、`remote.py`（stub）

## 1. 总体目标

提供整个系统**唯一的执行边界**：起进程、读写文件、列目录、清理工作目录。所有需要碰宿主机的操作都必须经过它——文件工具（任务 13）和 `run_command`（任务 14）都只认 `ws.executor`。

**为什么必须是一个抽象，而不是到处调 `Path.read_text()`。** 两个理由，一个关于未来，一个关于安全。关于未来：Docker 化的那天只换 Executor 实现，工具与 loop 一行不改。关于安全：如果文件工具自己读本地文件系统，容器化之后它会**读到宿主机的文件**——这类漏洞在 demo 里完全看不出来，在安全审查里是致命项。计划在任务 13 的标题下把这条写死了：「文件操作**必须经过 Executor**，不得直接 `Path.read_text()`。」

**本任务真正的硬骨头是设计文档的风险 R1，也是本项目的最高风险项**：Windows 上 `asyncio.wait_for` 超时**不会杀死子进程**，而 `proc.kill()` 只杀直接子进程。结果是 `run_command("pytest")` 超时后留下孤儿 python 进程，并导致工作目录删不掉（`PermissionError: [WinError 32]`）。不解决会怎样：workdir 越积越大、旧进程还在往「已废弃」的目录里写文件、下一次跑同名 case 时新旧文件混在一起——**产出的是错的但看起来正常的结果**，这比崩溃危险得多。Part 1 的验收标准里专门有一条：「`run_command` 的孙进程在超时后被杀死，工作目录能正常删除。」

## 2. 实现流程

1. **先写 7 条测试**，其中 `test_timeout_kills_process_tree` 是关键：脚本自己 `subprocess.Popen` 一个睡 60 秒的孙进程再自己睡 60 秒，用 `timeout_s=1` 跑，断言 `timed_out is True`——**并手工确认没有残留进程**。为什么这条必须先有：进程泄漏是「看不见的失败」，单元断言只能确认「我杀了我看见的那个」，孙进程还在不在得靠外部观察（步骤 5 的 `tasklist`）。
2. **定义 `ProcessResult` 值对象**（stdout / stderr / returncode / timed_out / truncated）与 `LocalExecutor`（`setup` / `run_process` / `_kill_tree` / `_cap` / 读 / 写 / 列目录 / `teardown`）。
3. **实现超时路径**：`asyncio.wait_for` 抛超时 → `await self._kill_tree(proc.pid)` → `await proc.communicate()` 收尸 → `timed_out = True`；`finally` 里再兜一次 `if proc.returncode is None: await self._kill_tree(...)`。
4. **实现 `teardown`**：`PermissionError` 重试 3 次、每次间隔 50ms，最后一次仍失败才 `raise`。
5. **跑测试 + 手工确认无孤儿进程**（`tasklist | grep -i python`）。
6. **`docker.py` / `remote.py` 只声明类与 `NotImplementedError`**——可扩展性的声明式表达，不写半成品。

**顺序不能反的地方：杀进程树必须在目录清理重试之前。** 计划注释原文是「杀进程树是目录清理重试能生效的前提，顺序不能反」。先删目录再杀进程，删不掉是必然的，三次重试只是浪费 150ms；反过来杀完树，第一次尝试通常就能删掉。**这是资源依赖决定的顺序，不是风格偏好。**

## 3. 具体技术实现

### 为什么 `proc.kill()` 不够

`proc.kill()` 终结的只是**你亲手创建的那一个进程**（`asyncio.subprocess.Process` 对应的直接子进程）。Windows 的进程模型里没有「杀死所有后代」的原生语义，后代只通过父进程 ID（PPID）这条链被记录。而 `run_command` 传进来的 `argv[0]` 是 `python` / `pytest`，它自己还会再 fork 出东西——`pytest-xdist` 的 worker、被测代码里的 `subprocess`、编译器、启动的守护进程。这些孙进程对「父进程被 kill」完全免疫。

### 孤儿是怎么产生的，又是怎么锁住目录的

直接子进程被 kill 后，孙进程的父进程消失了，它被系统**过继**给别的进程继续运行。此时它仍然持有两样要命的东西：

1. **进程的当前工作目录（cwd）**。在 Windows 上，cwd 是进程持有的一个内核对象引用；只要还有进程把它当 cwd，**这个目录就删不掉**（哪怕那个进程和你的 harness 毫无关系）。
2. **打开的文件句柄**。`__pycache__/*.pyc`、日志文件、临时文件——Windows 上「文件被打开」默认就禁止删除，POSIX 上则允许先 unlink 再由内核在句柄关闭后回收。这是两个平台**根本的语义差别**，所以「Linux 上测试全绿、Windows 上目录删不掉」是常态而非意外。

### `WinError 32` 的来历

`shutil.rmtree` 删除一个被占用的文件/目录时，Windows 返回 `ERROR_SHARING_VIOLATION (32)`，Python 把它包装成 `PermissionError: [WinError 32] The process cannot access the file because it is being used by another process`。看到这个错误信息时，**不要去找「哪个文件被谁打开」——先怀疑有孤儿进程**，因为占位者往往不在你的进程树里。

### 正解：`taskkill /F /T`

```python
if os.name == "nt":
    await asyncio.create_subprocess_exec("taskkill", "/F", "/T", "/PID", str(pid), ...)
else:
    os.killpg(os.getpgid(pid), signal.SIGKILL)
```

- `/T` = tree：连同该进程启动的子进程一起终止；`/F` = 强制。启动侧对应 `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`（Windows）/ `preexec_fn=os.setsid`（POSIX，新建会话组，配合 `os.killpg` 对整组发信号）。
- **`/T` 依赖 PPID 链，所以必须在直接子进程还活着的时候杀。** 等它自己退出后再拿它的 PID 去 `taskkill`，树已经散了（孤儿被挂到别的父进程下），找不到孙进程。计划里 `_kill_tree` 在超时分支和 `finally` 兜底里都调用，正是为了覆盖这条。
- **杀完必须 `await proc.communicate()` 收尸。** 一是回收进程状态与管道句柄（不收尸会留下「僵尸」般的资源占用），二是拿到超时前已经产出的部分输出——这部分输出恰恰是排障最有价值的材料（agent 卡在哪一步、卡之前的日志是什么）。

### 两条平台注意事项

1. **`asyncio.create_subprocess_exec` 在 Windows 上只支持 `ProactorEventLoop`**（3.8+ 的默认）。**绝不能**写 `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`，否则 `NotImplementedError`；`uvloop` 在 Windows 上不可用。这条要写进模块 docstring，因为它的报错信息不带任何上下文，指不回根因。
2. **`except (asyncio.TimeoutError, TimeoutError)` 两个都要捕**：从 Python 3.11 起 `asyncio.TimeoutError` 就是内置 `TimeoutError` 的别名，写两个是为了兼容旧版本的写法习惯，也让意图更明确。

### 输出截断：按字节，头尾各留一半

`_cap` 按 **UTF-8 字节数**判断（不是字符数——多字节字符会让「字符数够小、字节数超标」），超限时保留头 `half` 字节 + 中间标记 + 尾 `half` 字节。为什么头尾都留：**头部是命令与初始输出，尾部是失败信息，中间的过程日志价值最低。** 截断标志一路传到 `ToolResultEvent.truncated`，因为 `GroundingChecker` 要区分「agent 没检查输出」和「输出本身被截断了」——设计文档 §4.4 规定截断时该给 `WARN` 而不是判 `FAIL`。

### 其余接口的语义约定

- **非零退出不是异常**：`raise SystemExit(3)` 正常返回 `ProcessResult(returncode=3)`。执行器不替调用方做判断，「退出码 3 意味着什么」是任务 14 的事。
- **env 是「继承但可覆盖」**：`{**os.environ, **(env or {})}`。禁网靠覆盖代理变量实现（任务 14）。
- **文件操作走 `asyncio.to_thread`**：`os` / `shutil` / `pathlib` 都是阻塞的系统调用，直接在事件循环里做会卡住所有并发 run。
- **类型归属提醒**：`Executor` 协议在 `contracts/`（L0）里签名写的是 `-> "ProcessResult"` 与 `list["DirEntry"]`，而这两个类型目前定义在 `core/executors/local.py`（L1）。实现时应把它们下沉到 `contracts/`（与 `ToolCall` / `ToolResult` 同类），否则 L0 的类型签名指向了 L1 的类型。

## 4. 使用的技术栈简介

**本任务全部使用标准库**，一个第三方依赖都没有：

| 能力 | 说明 |
|---|---|
| `asyncio.create_subprocess_exec` | 事件循环上的子进程。**不使用 `shell=True`**，argv 以列表形式传递，天然规避 shell 解析带来的注入面 |
| `asyncio.wait_for` | 给等待加超时。**它的语义是「停止等待」，不是「终止对面」**——这正是本任务所有麻烦的源头，理解这一点是理解 R1 的钥匙 |
| `asyncio.to_thread` | 把阻塞调用（`pathlib` / `shutil` / 文件读写）丢到线程池执行。它存在的理由很直接：**标准库没有异步文件 API**，而 `os` / `shutil` 的每次调用都会阻塞事件循环。用它比自己开线程池更安全：不阻塞事件循环的调用就该这样写。技术选型也出于同样的理由选了 stdlib `sqlite3` + `asyncio.to_thread`（而不是 `aiosqlite`），理由是「每次评测只写几百行索引的场景里，异步驱动的收益接近于零」 |
| `subprocess.CREATE_NEW_PROCESS_GROUP` | Windows 侧让子进程成为新进程组的根，避免父进程收到控制台事件时连坐 |
| `os.setsid` / `os.killpg` / `signal.SIGKILL` | POSIX 侧的对应物：新建会话组 + 对整组发信号。`preexec_fn` **只在 POSIX 有意义**，Windows 上传 `None` |

关于「为什么不引入 `psutil`」：`psutil` 能遍历进程树，写起来更省事，但会新增一个核心依赖；`taskkill /T` 由系统自带且语义正确。少一个依赖就少一个供应链风险面——这与 tech-stack §10 用 `exclude-newer = "7 days"` 做供应链防护是同一种取舍。

测试侧：用 `sys.executable` 而不是字符串 `"python"`（设计文档 R1 明确要求）。在 venv 环境下 `"python"` 可能解析到系统解释器或 PATH 上的其它版本，测试会不可复现。异步测试沿用任务 12 测试文件里的 `pytestmark = pytest.mark.anyio`（anyio 自带插件，见 tech-stack §4.2）。

## 5. 工程化思想

- **平台语义差异是设计输入，不是 bug。** POSIX「打开的文件可以照删」与 Windows「被占用就锁死」是根本差别。把差异收敛到一个类里，上层（工具、loop、评测器）就永远不需要知道自己在哪个平台跑。**平台相关代码的正确位置：最底层的一个实现类，外加一段写清「踩过坑，勿改」的 docstring。**
- **看不见的失败需要外部观察者。** 进程泄漏、句柄泄漏这类问题的特点是：你的断言只覆盖「你以为会发生的事」。所以计划里专门有一步「手工 `tasklist` 确认无孤儿」——**单测断言与外部校验是两件事，不要让前者冒充后者**。
- **顺序是正确性的一部分。** 杀树 → 收尸 → 清理重试，这个顺序由资源依赖决定（谁持有谁）。它会被未来的重构轻易打乱，因为你重排后代码看起来更整齐，而错误只在 Windows 上、只在超时路径上出现。**遇到这种「靠顺序才正确」的逻辑，就把原因写进注释，让重排的人先读到理由。**
- **失败路径必须和成功路径一样被设计。** 正常退出、超时、非零退出、二进制不存在、输出过长、目录删不掉——六条路径各有明确语义和独立测试。只测 happy path 的执行器，第一次真跑 `pytest` 就会崩在没想到的那条分支上。
- **抽象是否成立，看「写下替换实现时会不会被迫改上层」。** `docker.py` / `remote.py` 是空壳，它们的价值不是「以后会实现」，而是**现在就证明了所有调用点都只能走接口**。如果你写下一个空壳实现时会发现「这里得改工具代码」「那里得改 loop」，那这个抽象是假的，早点发现比重构便宜。
