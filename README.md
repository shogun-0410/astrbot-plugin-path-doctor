# path_doctor 后台体检

在 AstrBot 里敲一条 `/diag`，一次看清 **插件 / AstrBot / 协议端 / 宿主机** 四层状态，外加文件链路与网络。

面向 AstrBot 4.x + `aiocqhttp`（OneBot v11）部署。**无第三方依赖**（纯标准库），`openpyxl` 存在时导出 xlsx，否则自动降级 CSV。默认只允许管理员使用。

> 它不修复任何东西——只负责把「现在到底是什么状态、哪里坏了、下一步该看什么」讲清楚。
> 排错判据按本项目约定：**日志 + 源码 + 实测**，不猜。

## 解决什么问题

| 你遇到的现象 | 用哪条命令看 | 通常会看到 |
| --- | --- | --- |
| 装完 / 重启后「到底该验哪些」 | `/diag startup` | 6 项启动自检清单（含**共享目录**与**网络**） |
| 插件改了代码但「没生效」 | `/diag plugin <名字>` | 目录已更新但进程里还是旧模块 / 未加载 / 缺依赖 |
| 插件报 `ImportError`、配置读不出来 | `/diag plugin` | 配置 JSON 坏了、缺键、依赖没装、`main.py` 语法错 |
| 发出的文件对方收不到（EACCES） | `/diag shared` | 哪一级目录 others 缺 x、文件 others 不可读 |
| 机器人不回话 | `/diag proto` | 协议端 API 无响应 / 某个适配器端口没有 ESTABLISHED |
| 所有联网插件一起挂 | `/diag net` | 「国内 / 海外 / 代理」三条结论，指出是节点还是 DNS |
| 机器变慢、日志写不进 | `/diag host`、`/diag astrbot` | 磁盘/inode 快满、内存吃紧、事件循环阻塞 |
| 想把群成员导出来 | `/diag export [群号]` | 直接得到 xlsx，走共享目录发送 |

## 功能

### 1. 插件体检 `/diag plugin [名字|all]`

- **加载状态**：扫描 `data/plugins/*` 与运行中的插件注册表（`star_registry.root_dir_name` / `name`）双向比对，
  报出「有目录但没加载」「已停用（`preferences.inactivated_plugins`）」「已注册未激活」。
- **元数据**：`metadata.yaml` 是否缺失、`name` / `version` / `author` / `astrbot_version` 是否可解析；
  声明了 `astrbot_version` 但当前版本不满足时直接标红。
- **版本漂移**：`metadata.version` 与代码里 `register(...)` / `*VERSION` 常量的字面量对比
  （本项目历史上出过 `vv` 双前缀、metadata 与代码不同步的问题）。
- **依赖**：`requirements.txt`（PEP 508 行式与 JSON 字典式都支持）逐条核对：
  先查 pip 分发包元数据，再退回 import 名，顺带校验版本约束（`fpdf2`→`fpdf`、`opencc-python-reimplemented`→`opencc` 这类包名≠模块名不会误报）。
- **配置**：`<目录名>_config.json` 是否存在、是否合法 JSON、`_conf_schema.json` 里声明的键缺了哪些、
  是否因为改了 `metadata.name` 导致配置文件名对不上。
- **源码**：`main.py` 语法检查（`utf-8-sig` 读取，带 BOM 不误报）。
- **卫生**：`plugins/` 里混入的备份目录 / 非插件目录（没有 `metadata.yaml`）、目录 others 不可读、体积与文件数。

### 2. 框架体检 `/diag astrbot`

版本、Python 版本、启动时间与运行时长、RSS / 线程 / 打开 fd、**事件循环延迟**（`sleep(50ms)` 实测漂移）、
平台适配器逐个状态（按 `/proc/net/tcp` 统计每个端口的 ESTABLISHED 数）、WebUI 端口是否监听、
数据库与 WAL 体积、关键库表行数、临时目录占用与配额、`http_proxy`、管理员列表、日志开关与最近错误行。

### 3. 协议端实测 `/diag proto [群号]`

真实调用 `get_login_info` / `get_status` / `get_version_info` / `get_group_list` 并计时，
给出「协议端响应正常 / 偏慢 / 无响应」结论；带群号时再抽样 `get_group_info` 与 `get_group_member_list`。
这是判断 SnowLuma（或任意 OneBot 实现）是否活着最直接的方式。

### 4. 宿主机 `/diag host`

容器 ID、数据目录的 bind mount 反推**宿主真实路径**、负载（宿主全局）、内存 / Swap、
磁盘与 inode、各数据目录体积、cgroup 与 PSI 压力。

### 5. 网络 `/diag net`（v2.5.0 补测）

给出**三条独立结论**，而不是一句综合判断：

| 结论 | 怎么测出来的 |
| --- | --- |
| **国内** | DNS 解析 + TCP 连通 + HTTPS 取回（`www.baidu.com`） |
| **海外** | 同上对 `github.com`；解析结果落在 `198.18.x.x`（mihomo TUN fake-ip）时额外标注「出网已走宿主 TUN 代理」 |
| **代理** | 四个平面探测 + **真实出网实测**（见下） |

代理平面探测（只读，**不读也不打印任何密钥**）：

1. **配置来源**：`HTTP_PROXY/https_proxy/ALL_PROXY` 环境变量与 AstrBot `http_proxy`（用户名密码一律脱敏成 `<带认证>`）；
2. **本机代理端口**：`127.0.0.1:7890/7891/1080` 是否在监听（混合口会 socks5 与 http 各试一次，避免误判）；
3. **宿主网关的代理内核 API**：由 `/proc/net/route` 推出网关（如 `172.x.0.1`，以实际值为准）再探 `:9090`，
   只报「端口可达 + HTTP 401（需鉴权）」——**故意不使用 `host.docker.internal`**：TUN fake-ip 环境下它会解析到
   `198.18.x.x`，任何端口都「通」，是假阳性（实测踩过）；
4. **容器内代理内核进程**：扫 `/proc/*/comm` 找 mihomo / clash / xray 等（容器里通常没装 `ps`）。

**真实出网实测**：对第一个「端口开着」的端点取一次 `https://www.gstatic.com/generate_204`——
`http(s)` 代理走标准库，`socks5(h)` 用手写的 SOCKS5 CONNECT + TLS（标准库不支持 socks，所以自己握手，
这样「代理到底能不能出网」是真测过的，而不是看端口猜）。结论分四档：

- `✅ 代理可用（socks5h://127.0.0.1:7890 → 204 · 270ms）`
- `❌ 代理端口在监听但出网失败 —— 先换节点 / 查订阅`
- `❌ 配置了代理但端口都连不上（地址写错 / 代理没起来）`
- `⚠️ 未发现可用出网端口；网关上有代理内核 API（401，需鉴权）—— 出网可能走宿主 TUN`

另外还会打印 DNS 服务器（`/etc/resolv.conf`）与各目标的解析 IP。
开关：`probe_net_proxy=false` 可关掉端口与出网实测（省 1–6 秒）。

### 6. 文件链路 `/diag shared`、`/diag writetest`

- `shared`：在共享目录写探针（目录 777 / 文件 666），从 `/` 到探针逐级判定 others 的 x/r 位，
  指出是哪一级卡住、协议端能不能读到，最后清理探针。
- `writetest`：实测可写性，显示当前 `umask` 与新建文件的实际权限（解释「为什么产物默认 644」）。

### 7. 启动自检清单 `/diag startup`

一条命令按固定顺序跑 6 项，与对应子命令**同源同判定**（不会出现两处结论打架）：

| 序号 | 项 | 同源命令 |
| --- | --- | --- |
| ① | 框架与协议端（版本 / 适配器在线 / API 往返） | `/diag astrbot`、`/diag proto` |
| ② | **共享目录可见（shared）** | `/diag shared` |
| ③ | **网络（net）：国内 / 海外 / 代理三条结论** | `/diag net` |
| ④ | 落盘可写（含 umask） | `/diag writetest` |
| ⑤ | 插件健康（已加载 / 异常 / 警告） | `/diag plugin` |
| ⑥ | 关键配置（`admin_only` / `admin_list` / `send_dir` 是否在共享目录内 / 两个 probe 开关） | 配置面板 |

每项给 ✅/⚠️/❌ + 一句对策；末尾给未通过项与**排错顺序**（③ 网络 → ② 共享目录 → ① 协议端 → 其余）。
别名：`check`、`自检`、`启动自检`、`上线自检`。云端上线时先发这一条，再按需逐项复核。

输出示例：

```text
🧭 path_doctor v2.5.0 启动自检清单
   2026-10-31 00:20:11 · UTC+08:00 CST
   结论：✅ 6/6 全通过（可以上线）

① 框架与协议端            ✅ AstrBot 4.29.3 · 平台 2 个（在线 2）· API 12ms
② 共享目录可见（shared）   ✅ /AstrBot/data/temp · drwxrwxrwx 0o777/-rw-rw-rw- 0o666 · others 可进可读
③ 网络（net）             ✅ 国内 ✅ 可达 · 海外 ✅ 直连可达（DNS 为 fake-ip…）· 代理 ✅ 代理可用（…）
④ 落盘可写                ✅ /AstrBot/data/temp · -rw-rw-rw- · umask 0o22
⑤ 插件健康                ⚠️ 20 个目录 · 已加载 18 · 异常 0 · 警告 2
⑥ 关键配置                ⚠️ admin_only=开 · admin_list 已配 1 人 · send_dir 默认（共享目录内）

   判据：② 与 /diag shared 同源、③ 与 /diag net 同源，两处结论永远一致。
```

### 8. 群成员导出

| 命令 | 说明 |
| --- | --- |
| `/diag export [群号]` 或 `/导出群数据 [群号]` | 导出当前群（或指定群）成员到 xlsx |
| `/diag exportall` 或 `/导出所有群数据` | 导出全部群成员到多 sheet xlsx（含汇总页，管理员） |
| `/diag groups` 或 `/群信息` | 机器人所在群列表（按人数排序，标出本群） |

导出字段：群号、群名、QQ、昵称、群名片、角色、等级、头衔、加入时间、最后发言、禁言到期、不良记录。
时间戳格式化为 `YYYY-MM-DD HH:MM:SS`，首行冻结 + 自动筛选。

**与 `astrbot_plugin_group_information` 的差异**（能力参考其 MIT 项目，实现为本项目重写，未复制代码）：

- 不依赖 `pandas`，直接 `openpyxl` 写盘；缺 `openpyxl` 时降级 CSV（`utf-8-sig`，Excel 可直接打开）。
- 产物落**共享目录** `<temp>/path_doctor_export/`（777 / 666）并用 `file://` 发送，
  不用 `base64://` 塞协议帧，也不写协议端读不到的 `plugin_data/`。
- 单群导出有 `export_max_members` 上限，导出目录按 `export_keep_files` 自动清理旧文件。

## 命令一览

| 命令 | 作用 | 权限 |
| --- | --- | --- |
| `/diag` | 体检总览（四层结论 + 问题清单） | 管理员 |
| `/diag startup` | **启动自检清单**（含 shared 与 net，②③ 与对应子命令同源） | 管理员 |
| `/diag plugin [名字\|all]` | 插件逐个体检 | 管理员 |
| `/diag astrbot` | 框架体检 | 管理员 |
| `/diag proto [群号]` | 协议端 API 实测 | 管理员 |
| `/diag host` | 宿主机资源（含 docker 容器，若挂了 socket） | 管理员 |
| `/diag net` | 网络实测（国内 / 海外 / **代理**三结论 + 代理出网实测） | 管理员 |
| `/diag log [n]` | 日志按插件聚合（需开 `log_file_enable`） | 管理员 |
| `/diag caps` | 协议端能力矩阵 | 管理员 |
| `/diag commands` | 命令重名扫描（跨插件静态检查） | 管理员 |
| `/diag snapshot` | 写一份自检快照到 `plugin_data` | 管理员 |
| `/diag shared [目录]` | 共享目录对端可见性 | 管理员 |
| `/diag writetest [目录]` | 写入实测 | 管理员 |
| `/diag groups` | 群列表 | 任何人 |
| `/diag export [群号] [xlsx\|json\|csv]` | 导出群成员（默认带统计样式的 xlsx） | 任何人 |
| `/diag exportall [xlsx\|json\|csv]` | 导出全部群成员（多 sheet / 结构化） | 管理员 |
| `/diag json` | 结构化快照 JSON（给别的插件/脚本读） | 管理员 |
| `/diag md` | Markdown 报告（可选，贴文档用） | 管理员 |
| `/diag pack` | 一键归档 zip（体检报告.xlsx + snapshot.json + caps.json + manifest.json） | 管理员 |
| `/diag report` | **体检报告（7 页 Excel）** | 管理员 |
| `/diag txt` | 体检报告（纯文本，排查用） | 管理员 |
| `/diag help [子命令]` | 命令表；带子命令时看单个命令的用法/示例/相关（如 `/diag help net`） | 任何人 |

兼容旧写法：`path` → 总览，`perm` → shared，`write` → writetest，`txt` → report。
自检清单别名：`check`、`自检`、`启动自检`、`上线自检`。
群信息别名：`群信息`、`导出群数据 [群号]`、`导出所有群数据`。

看单个命令：`/diag help <子命令>`（例 `/diag help net`、`/diag help proxy` —— 别名也能查），
输出「作用 / 示例 / 相关命令」。输入了不存在的子命令时，会先提示「没认出来 X」再给整张命令表
（`/diag`、`/diag help`、`/diag 帮助`、`/diag ?` 都是同一张表）。
没有多轮对话式向导：所有命令都是一次性问答（发一条 → 回一份），需要交互的部分都在输出的「对策」行里。

## 一份数据，多种出口

| 出口 | 形态 | 适合 |
| --- | --- | --- |
| `/diag report` | `体检报告_*.xlsx` | **体检文档（7 页 Excel）**：总览 / 插件 / 框架 / 协议端 / 宿主机 / 网络 / 文件链路 |
| `/diag pack` | `diag_pack_*.zip` | 体检报告.xlsx + snapshot.json + caps.json + manifest.json |
| `/diag export` | `群成员_*.xlsx` | **成员文档**：汇总页 + 每群一页 + 统计图 + 角色着色 |
| `/diag json` / `md` | JSON / Markdown | 给脚本读、或贴文档 |
| `/diag export ... json\|csv` | JSON / CSV | 给脚本读、或进 Excel |

两份 **Excel 是主交付物**：体检报告用同一套配色（深蓝表头 / 隔行浅蓝 / 状态色块），
成员表用同一套字体与边框；`manifest.json` 记了每个文件的字节数与 SHA256，转手时能核对有没有被改过。
## 安装

> 仓库：<https://github.com/shogun-0410/astrbot-plugin-path-doctor> · 许可：MIT
> 发布包（zip）在 [Releases](https://github.com/shogun-0410/astrbot-plugin-path-doctor/releases) 下载；
> 本仓库根目录**就是插件目录**（`main.py` / `metadata.yaml` / `_conf_schema.json` / `logo.png`），
> 可直接整目录放进 `data/plugins/astrbot_plugin_path_doctor/`。

1. 解压到 AstrBot 的 `data/plugins/`，形成 `data/plugins/astrbot_plugin_path_doctor/`。
2. 重启 AstrBot 或重载插件。
3. 打开插件配置，填 `admin_list`（不填则任何人都能用，会提示配置）。
4. 群里发 `/diag startup` 看 6 项自检清单，再发 `/diag` 验证四层结论；建议 `/diag report` 留档。

无需 `pip install`；只有导出 xlsx 需要 `openpyxl`（多数 AstrBot 镜像自带）。
升级时 AstrBot 无法覆盖同名目录：先在插件页卸载再装新包（配置数据在 `data/config/`，不受影响）。

## 配置

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `admin_only` | bool | `true` | 仅管理员可用；`admin_list` 为空时放行并提示 |
| `admin_list` | string | `""` | 逗号分隔的管理员 QQ |
| `shared_dir` | string | `""` | 默认共享目录，留空 = AstrBot 临时目录 |
| `send_dir` | string | `""` | 报告/导出输出目录，留空 = `<temp>/path_doctor_export` |
| `export_max_members` | int | `5000` | 单群导出成员上限 |
| `export_keep_files` | int | `5` | 导出目录保留份数 |
| `probe_protocol` | bool | `true` | `/diag proto` 是否真的打协议端 API |
| `probe_net_proxy` | bool | `true` | `/diag net` 是否探代理端口并做一次真实出网实测（1–6 秒，只读、不读密钥） |
| `auto_snapshot` | bool | `false` | 定时写自检快照到 `plugin_data/path_doctor/snapshots/` |
| `report_interval_hours` | int | `24` | 自检快照间隔（小时） |
| `report_keep` | int | `7` | 自检快照保留份数 |

## 导出文件长什么样

`/diag export` 与 `/diag exportall` 产出的是排版过的 xlsx，不是「能打开」而已：

- **汇总页**：每群人数 / 群主 / 管理员 / 普通成员 / 近 7 天发言 / 30 天未发言 / 90 天未发言；
  多群时附「各群成员数」柱状图，单群时附「角色构成」饼图。
- **群页**：标题 + 一句话统计摘要 + 14 列表头；表头深蓝白字、隔行浅蓝、细边框；
  行按「群主 → 管理员 → 成员，再按入群时间」排序，所以打开就是有意义的顺序。
- **列**：序号、群号、群名、QQ号、昵称、群名片、角色、等级、头衔、加入时间、最后发言、
  沉默天数、禁言到期、不良记录。QQ 号与群号按**文本**存，不会变成 `1.09E+04`。
- **时间**是真日期（`yyyy-mm-dd hh:mm`），可排序筛选；空值统一 `—`。
- **沉默天数**带数据条，超过 90 天自动标红 —— 一眼看出谁不说话了。
- 冻结前 3 列 + 表头、自动筛选、打印横向/缩放到一页宽、标题行重复。

## 输出示例

```text
🧭 path_doctor v2.1.0 体检总览
   2026-10-28 01:46:37 · UTC+08:00 CST
   结论：⚠️ 发现 2 个待处理项

【插件】19 个目录 · 已加载 17 · 异常 0 · 警告 2
【框架】AstrBot 4.29.3 · 平台 2 个（在线 2）· WebUI :6185
【协议端】已连接 · :6199✓, :6200✓
【宿主机】load 0.18/0.09/0.04 · 内存 50% · 磁盘 67%
【共享】/AstrBot/data/temp · 737 文件 357.6M · drwxrwxrwx 0o777

问题清单（/diag report 看完整报告）：
  1. astrbot_plugins_ConvetPicture · 没有 metadata.yaml（AstrBot 会忽略该目录，可考虑清理）
  2. plugin_upload_9qhzgowe · 没有 metadata.yaml（AstrBot 会忽略该目录，可考虑清理）
```

## 设计原则

- **只读优先**：诊断不修改任何配置；唯一副作用是写探针与导出文件，探针用完即删，导出目录限量保留。
- **`docker.sock` 只读、可选**：挂了就顺带列出宿主容器（`/containers/json`，只看不控），
  没挂就明确说「看不到」，绝不去要特权。
- **不打印密钥**：配置里的 token / password / cookie 一律不落到输出里。
- **单节失败不影响全篇**：任一检测抛异常只在报告里留下 `<ERR ...>`，其余照常。

## 已知限制

- 容器内**默认没有 `docker.sock`**：看不到宿主上其它容器（如 SnowLuma、Suwayomi）的状态，
  协议端是否存活靠「适配器端口有 ESTABLISHED + OneBot API 往返」间接判断；
  若要直接看容器，可把 `docker.sock` **只读**挂进容器（`-v /var/run/docker.sock:/var/run/docker.sock:ro`）。
- `/diag net` 与 `/diag proto` 会真的发网络请求（各自带超时），在很慢的网络下整体会多花几秒；
  HTTPS 两段各是 **8 秒 × 最多 2 次**（走 TUN/代理时首次超时很常见，见 v2.5.2），
  `probe_net_proxy=true`（默认）时还会对可用代理真实取一次海外 204（1–6 秒），
  代理端点连不上时按超时计；嫌慢可把 `probe_net_proxy` 关掉。
- 容器内**看不到宿主机绑在 `127.0.0.1` 上的代理端口**（例如宿主 mihomo 只监听回环）：
  此时 `/diag net` 会如实报「未发现可用出网代理端口」，并用「网关 9090 上有代理内核 API」间接说明。
- 事件循环延迟测量会 `sleep 50ms`，`/diag astrbot` 与 `/diag report` 各引入约 50ms 停顿。
- `/diag log` 依赖 `log_file_enable=true`；AstrBot 默认不写文件日志，此时只能提示去开。
- mock 环境里 `get_group_member_list` 由测试桩返回假数据，真实人数与字段以线上为准。

## 版本历史

| 版本 | 变化 |
| --- | --- |
| **v2.5.3** | **公开发布**：`metadata.repo` 指向本仓库；源码注释与文档里的宿主路径 / 网关示例改为通用写法（`/home/<user>/…`、`172.x.0.1`），不再出现任何真实环境信息。功能与 v2.5.2 完全一致 |
| **v2.5.2** | **探测抗抖**：`/diag net` 的 HTTPS 两段（国内/海外）从「6 秒单发」改为「8 秒 × 最多 2 次」——走宿主 TUN/代理时这条链路实测 0.7s～6.2s 波动，单发超时会让结论在 ✅/❌ 之间抖（本轮实测抓到一次假失败）；重试成功后标「第 2 次成功（首次 xxx）」，两次都失败才判 ❌ 并写「重试后仍失败」。Excel 网络页与文本同步显示重试信息 |
| **v2.5.1** | **帮助与指令清理**：新增 `/diag help <子命令>`（单命令用法/作用/示例/相关，别名也能查，如 `/diag help proxy`）；未知子命令现在会先提示「没认出来 X」再给命令表（含中文 `/diag 帮助`、`/diag ?`）；删掉被总览分支永久遮蔽的死别名 `pack` 分支里的 `all`；修正帮助文本里 `pack` 的错误说明（实际是 体检报告.xlsx + snapshot.json + caps.json + manifest.json）与 `export/exportall` 的格式参数；README 命令一览去掉重复的 `/diag caps` 行。**无命令语义变更**（`/diag`、`/diag help` 无参数时输出与 v2.5.0 一致） |
| **v2.5.0** | **网络实测补测 + 启动自检清单接入**：`/diag net` 结论拆成「国内 / 海外 / 代理」三条，新增代理四平面探测（配置 / 本机端口 / 宿主网关 API / 容器内核进程）与**经代理的真实出网实测**（http(s) 走标准库、socks5(h) 手写 SOCKS5 CONNECT + TLS），DNS 服务器与解析 IP（含 fake-ip 标注）；新增 **`/diag startup` 启动自检清单**（6 项，②共享目录与③网络与 `/diag shared`、`/diag net` 同源）；新增配置 `probe_net_proxy`；Excel 体检报告网络页同步补代理端点 / 内核 API / 出网实测 / 三条结论 / DNS 行；`net` 结构只增字段，`tcp`/`https`/`proxy.env`/`proxy.config`/`ok`/`verdict` 全部保持原样 |
| **v2.4.0** | 交付文档统一为 Excel：`/diag report` 产出 7 页工作簿（总览/插件/框架/协议端/宿主机/网络/文件链路），`/diag pack` 内含该 xlsx；两份 Excel 共用一套排版规则 |\n| v2.3.0 | 移除 WebUI 面板（在当前 AstrBot 上不可靠，且与文件出口重复）；新增 `/diag pack` 一键归档 zip；版本回到「纯文件」路线 |\n| v2.2.0 | 一份快照、多种视图：`/diag json`（结构化）、`/diag md`（Markdown）、**WebUI 体检面板**（`pages/diag`）、`/diag caps`（协议端能力矩阵）；成员导出支持 `xlsx/json/csv`；网络结论细分「TCP 通但 HTTPS 失败」 |\n| v2.1.0 | Excel 重做（汇总页 + 统计图 + 角色着色 + 沉默天数数据条/红色告警 + 打印与冻结）；新增 `/diag log`、`/diag commands`、`/diag snapshot` 与定时快照；`/diag host` 支持只读 docker.sock；补 `logo.png` |
| v2.0.0 | 四层体检（插件/AstrBot/协议端/宿主机）+ 网络实测；`/diag report` 改落共享目录；新增群成员导出、群列表；修复依赖误判与 BOM 误报 |
| v1.2.0 | 命令正式化：`/diag`、`shared`、`writetest`、`report`；新增 `admin_only` / `admin_list` |
| v1.1.0 | 增加共享目录对端可见性判定 |
| v1.0.0 | 初版：路径逐级权限 + 写入实测 |

## 致谢

群成员导出的能力参照了 [Futureppo/astrbot_plugin_group_information](https://github.com/Futureppo/astrbot_plugin_group_information)（MIT）的使用场景，实现为本项目重写。
