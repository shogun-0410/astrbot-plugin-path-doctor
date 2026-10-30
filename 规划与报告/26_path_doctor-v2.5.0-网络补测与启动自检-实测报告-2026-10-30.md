# path_doctor v2.4.0 → v2.5.2：网络实测补测 + 启动自检清单 + 帮助清理 + 探测抗抖 · 实测报告

> 本文件是交付到 GitHub 发布目录的**脱敏版**：宿主家目录路径、机器地址、真实测试群号一律替换为占位符；
> 完整未脱敏的实测证据见 `F:\Test\astrbot_vm\方案\05_运维诊断\26_path_doctor-v2.5.0-网络补测与启动自检-实测报告-2026-10-30.md`。

> 日期：2026-10-30
> 任务（开场消息）：`/diag net` 网络实测补测 + shared/net 接入启动自检清单
> 范围：仅诊断命令与文档 · 约束：不改现有命令语义 · 优先级：先 net 实测，再自检接入
> 环境：VM `测试 VM`（Ubuntu 24.04）· AstrBot 4.29.3 容器 · mock OneBot 群 `900001`
> 判据：日志 + 源码 + 实测（三证）；命令输出均为 VM 上原样回执
> 结构：§1–§8 = v2.5.0；**§9 = v2.5.1（帮助与指令清理）**；**§10 = v2.5.2（探测抗抖）**

## 0. 结论速览

| 项 | 结果 | 证据 |
| --- | --- | --- |
| `/diag net` 三结论 | ✅ 国内 / 海外 / 代理 三条独立结论 | §4.1 原样回执 |
| 代理「真的实测过」 | ✅ 四平面探测 + 经代理真实取海外 204（`socks5h://127.0.0.1:7890` → 204 · 431ms） | §4.1、§4.3、§4.4 |
| `socks5(h)` 支持 | ✅ 手写 SOCKS5 CONNECT + TLS（标准库不支持 socks），离线用本地 SOCKS5 桩验证 | §4.3 |
| 启动自检清单 | ✅ 新增 `/diag startup`，6 项；**②shared 与 ③net 与对应子命令同源同判定** | §4.2 原样回执 |
| 不改现有语义 | ✅ `tcp/https/proxy.env/proxy.config/ok/verdict` 原样；`snapshot.schema` 仍为 1 | §3.3、§4.5 |
| 回归 | ✅ `help / shared / json / md / report / txt / pack` 全通过 | §4.5 |
| Excel 渲染 | ✅ 网络页 `A1:E24`、总览页 `A1:D26` 无截断、`missingFonts: []` | §4.6 |
| 部署一致性 | ✅ 本地包 = VM 上 `main.py`（md5 `03aca1ee…`、sha256 `34345e28…`） | §4.7 |
| 顺手修的既有 bug | ✅ Excel 中文长文本换行行数被低估 → 下半截被裁；现按显示宽度折算 | §3.4、§4.6 |

## 1. 需求与验收对照

| 验收项 | 结果 |
| --- | --- |
| `/diag net` 给出**国内 / 海外 / 代理**结论 | ✅ 【结论】四行：国内、海外、代理、综合（含联网可用性） |
| 自检清单**包含 shared 与 net** | ✅ `/diag startup` 的 ②「共享目录可见（shared）」与 ③「网络（net）」 |
| 不改现有命令语义 | ✅ 原字段与别名全保留，只新增 |
| 只动诊断命令与文档 | ✅ 未碰业务插件、未改 AstrBot 配置、未改 mock 桩 |
| 文件按目录放 / 完工更新状态 | ✅ 见 §5、§6 |

## 2. 现状核实（三证）

### 2.1 源码

- 基线 v2.4.0：`main.py` 150141 字节，SHA256 `cb8b6604…613b09`；本地源码此前只存在于发布包内，
  本次先由 `astrbot_plugin_path_doctor-v2.4.0.zip` 还原到 `插件\05_运维诊断\path_doctor\astrbot_plugin_path_doctor\`。
- 旧 `_net_probe()`：TCP 探 `github.com/baidu/1.1.1.1:443`，HTTPS 用 `ProxyHandler({})`（**强制直连**），
  代理只读环境变量与 AstrBot 配置 → **从不真的走代理测一次**；结论只有一句综合判断。
- 旧 `_net_section()`：`【代理】` 段落只有「环境变量 / 配置 / 是否存在宿主 runtime.yaml」三行。
- 全库 grep `自检`：只命中「自检快照」（`/diag snapshot`），**没有**启动自检清单概念。

### 2.2 实测基线（改动前在 VM 上跑 `/diag net`，原样）

```text
🌐 网络实测
【TCP】✅ 海外 github.com:443 / ✅ 国内 www.baidu.com:443 / ✅ 公共 DNS 1.1.1.1:443
【HTTPS】✅ 国内 https://www.baidu.com 200 · 201ms / ✅ 海外 https://github.com 200 · 3828ms
【代理】环境变量 <未设置> / AstrBot 配置 <未设置>
   结论: ✅ 内外网均可达              ← 只有一句综合结论，没有「代理结论」
```

### 2.3 环境事实（这轮实测出来的，决定了方案怎么写）

| 事实 | 证据 | 对方案的影响 |
| --- | --- | --- |
| 容器内确有代理内核在监听 | 容器内 `127.0.0.1:7890`、`127.0.0.1:9090` 均 open；`/proc/net/tcp` 见 `0100007F:1ED2/2382`；`/proc/*/comm` 里有 `mihomo` | 代理端口 + 内核进程可探测 |
| 本机代理真的能出网 | 容器内 `socks5h://127.0.0.1:7890`→github 200；python 自写 SOCKS5 CONNECT + TLS→gstatic 204 (343ms)、github 200 (2834ms)；`urllib` 走 http 代理→gstatic 204 (270ms) | 纯标准库就能做「真实出网实测」 |
| 宿主侧代理也能看到 | 宿主 mihomo 是 TUN 模式进程；容器经网关 `172.19.x.1:9090` 能打到宿主 API（未授权 401，带密钥 `v1.19.33`） | 可区分「本机代理」与「宿主代理内核」 |
| `host.docker.internal` 是陷阱 | 容器内它解析到 `198.18.0.99`（TUN fake-ip），**任何端口探测都返回 open** | 探测必须用 `/proc/net/route` 推的网关，不能用它 |
| DNS 走 fake-ip | 容器内解析 github/baidu/google 全是 `198.18.x.x`；`google` 直连 000（不通）、`github` 直连 200 | 海外结论要标出「fake-ip = 宿主 TUN 生效」 |
| 宿主 mihomo 只监听回环 | 宿主 `ss -lntp`：`127.0.0.1:7890`、`*:9090` | 容器探不到宿主 7890，属预期，要在结论里说清 |

## 3. 做了什么

### 3.1 `/diag net` 补测

- **代理四平面探测**：① 环境变量 / AstrBot `http_proxy`（账号密码脱敏为 `<带认证>`）
  ② 本机端口 `7890/7891/1080`（混合口 socks5 与 http 各试一次）③ 宿主网关 `:9090` 内核 API（只判可达 + 是否要鉴权）
  ④ `/proc/*/comm` 里的代理内核进程名。
- **真实出网实测**：对第一个端口开着的端点取一次 `https://www.gstatic.com/generate_204`；
  `http(s)` 走标准库代理，`socks5(h)` 走手写的 SOCKS5 CONNECT（无认证）+ TLS。
- **三结论 + 综合**：`verdicts = {domestic, overseas, proxy}`；`usable = 国内通 且（海外直连通 或 代理可用）`。
- **附带**：DNS 服务器（`/etc/resolv.conf`）、各目标解析 IP、fake-ip 标注。
- 新配置 `probe_net_proxy`（默认 `true`）：关掉则跳过端口与出网实测。

### 3.2 `/diag startup` 启动自检清单（新命令）

别名 `check` / `自检` / `启动自检` / `上线自检`。6 项固定顺序，每项 ✅/⚠️/❌ + 对策：

| 序号 | 项 | 同源 |
| --- | --- | --- |
| ① | 框架与协议端（版本 / 适配器在线 / API 往返） | `/diag astrbot`、`/diag proto` |
| ② | **共享目录可见（shared）** | `/diag shared` |
| ③ | **网络（net）：国内 / 海外 / 代理** | `/diag net` |
| ④ | 落盘可写（含 umask） | `/diag writetest` |
| ⑤ | 插件健康（已加载 / 异常 / 警告） | `/diag plugin` |
| ⑥ | 关键配置（`admin_only`/`admin_list`/`send_dir` 是否在共享目录内/两个 probe 开关） | 配置面板 |

②③ 直接复用 `_shared_probe()` / `_net_probe()`，**同源同判定**，不会两处结论打架；末尾给未通过项与排错顺序。

### 3.3 兼容性（不改现有语义）

| 保留 | 新增 |
| --- | --- |
| `tcp[]`（+`addr`/`fake_ip`）、`https[]`、`proxy.env`、`proxy.config`、`ok`、`verdict`、`checked_at`；`snapshot.schema = 1` | `verdicts{domestic,overseas,proxy,proxy_short}`、`usable`、`dns{servers,fake_ip,targets}`、`proxy.{endpoints,api,kernels,active,egress,level,verdict,verdict_short,gateway,test_url,probed}` |

原有命令语义与别名（`path`/`perm`/`write`/`txt`）未动；`ok` 的判定公式与原版逐字一致。

### 3.4 顺手修的既有 bug（Excel 布局）

`_ws_table()` 估算自动换行行数时按「字符数」算，中文每字占 2 列 → 行数被低估 → 行高不够、下半截被裁。
现按显示宽度（CJK 记 2 列）折算，并把总览页「值」列改为自动换行。§4.6 有渲染前后对比。

## 4. VM 实测证据

### 4.1 `/diag net`（v2.5.0，原样回执）

```text
🌐 网络实测

【TCP】
   ✅ 海外       github.com:443 DNS 1ms · TCP 0ms · 198.18.0.138
   ✅ 国内       www.baidu.com:443 DNS 1ms · TCP 0ms · 198.18.0.157
   ✅ 公共 DNS   1.1.1.1:443 DNS 0ms · TCP 0ms · 1.1.1.1

【HTTPS】
   ✅ 国内     https://www.baidu.com 200 · 169ms
   ✅ 海外     https://github.com 200 · 738ms

【DNS】
   服务器 127.0.0.11
   解析结果含 198.18.x.x（mihomo TUN fake-ip）：DNS 已被代理接管

【代理】
   环境变量 <未设置>
   AstrBot 配置 <未设置>
   ✅ 本机端口  127.0.0.1:7890  clash/mihomo 混合口
   ❌ 本机端口  127.0.0.1:7891  clash socks 口
   ❌ 本机端口  127.0.0.1:1080  通用 socks 口
   ❌ 宿主网关  172.19.x.1:7890  宿主代理混合口（宿主只监听回环时探不到，属正常）
   ✅ 代理内核 API 127.0.0.1:9090 HTTP 401（需鉴权属正常；只看不控、不读密钥）
   容器内代理内核进程：mihomo
   出网实测 ✅ socks5h://127.0.0.1:7890 → https://www.gstatic.com/generate_204 204 · 431ms

【结论】
   国内 ✅ 可达
   海外 ✅ 直连可达（DNS 为 fake-ip：出网已走宿主 TUN 代理）
   代理 ✅ 可用（socks5h://127.0.0.1:7890 → 海外 204 · 431ms）
   综合 ✅ 内外网均可达 · 联网可用性 ✅ 可用
```

### 4.2 `/diag startup`（v2.5.0，原样回执）

```text
🧭 path_doctor v2.5.0 启动自检清单
   2026-10-31 00:25:57 · UTC+08:00 CST
   结论：⚠️ 4/6 通过 · 2 项注意（插件健康、关键配置）

① 框架与协议端            ✅ AstrBot 4.29.3 · 平台 2 个（在线 2） · API 1ms
② 共享目录可见（shared）  ✅ /AstrBot/data/temp · drwxrwxrwx 0o777/-rw-rw-rw- 0o666 · others 可进可读（协议端 realpath + 读文件应成功）
③ 网络（net）             ✅ 国内 ✅ 可达 · 海外 ✅ 直连可达（DNS 为 fake-ip：出网已走宿主 TUN 代理） · 代理 可用（socks5h://127.0.0.1:7890 → 海外 204 · 311ms）
④ 落盘可写                ✅ /AstrBot/data/temp · -rw-rw-rw- · umask 0o22
⑤ 插件健康                ⚠️ 19 个目录 · 已加载 17 · 异常 0 · 警告 2
⑥ 关键配置                ⚠️ admin_only=开 · admin_list 为空（等于不限制） · send_dir 默认（共享目录内）
     → admin_list 为空 → /diag 任何人可调，建议填管理员 QQ

   判据：② 与 /diag shared 同源、③ 与 /diag net 同源，两处结论永远一致。
   排错顺序：③ 网络（联网插件全挂的根因）→ ② 共享目录（发文件 EACCES）→ ① 协议端 → 其余
   完整报告 /diag report（Excel） · 归档 /diag pack · 结构化 /diag json
```

> ⑤⑥ 的 ⚠️ 是这台 VM 的真实状态（`ConvetPicture`/`plugin_upload_*` 三个目录没有 `metadata.yaml`；
> `admin_list` 为空），不是插件缺陷。云端按要求填了 `admin_list` 且清掉无 metadata 目录后应为 6/6。

### 4.3 离线单测（本机，不依赖 AstrBot）

`python archive\tmp\pd_helpers_test.py` → **ALL HELPER TESTS PASSED**（16 项断言）：

| 组 | 覆盖 |
| --- | --- |
| `_parse_proxy` | `socks5h://host:port`、裸 `host:port`、带认证、socks 默认端口 1080、空值/非法值 |
| `_mask_proxy` | 带认证 → `http://<带认证>10.0.0.1:8080`（断言：用户名与密码都不出现） |
| `_is_fake_ip` | `198.18.0.138`/`198.19.1.1` 为真，普通 IP / 空值为假 |
| `_tcp_open` | 开 / 关 / 空 host |
| `_socks5_tunnel` | **对着本地最小 SOCKS5 桩 + echo 后端**跑通握手与数据往返（`ECHO:hello`）；连不上时报错不挂 |
| `_http_probe_code` | 连不上返回 `None`（不抛） |

> 首轮测试暴露了一个真实缺陷：SOCKS5 握手用 `recv(n)` 直接读，**TCP 不保证按写入边界返回**，
> 碎片化时会解析错位。已改为 `_recv_exact()` 精确读（响应行也改为读到 `\r\n` 为止），重测通过。

### 4.4 容器内分支覆盖（`docker exec -i astrbot python3 -`，真实网络 + 少量模拟）

| 分支 | 构造 | 结果 |
| --- | --- | --- |
| A 环境变量指向死端口 | `HTTP_PROXY=http://127.0.0.1:7999`（真实） | ✅ 自动兜底到可用的本机 7890，报「代理可用」 |
| B 配了代理但端口都连不上 | 配置 + 端口探测模拟全关 | ✅ `bad`：配置了但端口都连不上（代理没起来，或地址 / 端口写错） |
| C 端口在监听但出网失败 | 出网取回模拟抛错 | ✅ `bad`：端口在监听但出网失败 —— 先换节点 / 查订阅 |
| D 只有内核 API 可达 | 端口探测模拟只开 9090 | ✅ `warn`：未发现可用出网端口；…:9090 上有代理内核 API（HTTP 401，需鉴权） |
| E `probe_net_proxy=false` | 配置关 | ✅ `info`：已关闭代理实测 |
| F 真实环境 | 无环境变量 | ✅ `ok`：代理可用（socks5h://127.0.0.1:7890 → 204 · 331ms） |
| G 带认证地址不泄漏 | AstrBot `http_proxy=http://user:secret@10.0.0.1:8080` | ✅ 只输出 `http://<带认证>10.0.0.1:8080`，`secret` 不出现 |

### 4.5 回归（mock，原样回执摘要）

| 命令 | 结果 |
| --- | --- |
| `/diag help` | ✅ 新增 `/diag startup` 行、`net` 行改为「三结论」、别名行 |
| `/diag shared` | ✅ 5 级权限链、探针 777/666、结论「others 可进可读」、已清理 |
| `/diag json` | ✅ `snapshot_*.json` 15K；`net` 新字段齐全，旧字段仍在，`schema=1` |
| `/diag md` | ✅ 3900 字；新增「代理平面」「三条结论」两节 |
| `/diag report` | ✅ `体检报告_*.xlsx` 16K，7 页（总览/插件/框架/协议端/宿主机/网络/文件链路） |
| `/diag txt` | ✅ 8084 字纯文本报告 |
| `/diag pack` | ✅ zip 20K（体检报告.xlsx + snapshot.json + caps.json + manifest.json） |

### 4.6 Excel 渲染核验（LibreOffice Kit，144dpi）

| 页 | 范围 | 结果 |
| --- | --- | --- |
| 网络 | `A1:E24` | ✅ 22 行全在：TCP/HTTPS 各目标 + 4 个代理端点 + 内核 API + 出网实测 + 内核进程 + 三条结论 + DNS 服务器；`missingFonts: []` |
| 总览 | `A1:D26` | ✅ 新增「网络 / 国内 / 海外 / 代理」行三行内换行完整显示；问题清单 3 条 |

修复前后对比（同为长中文行的行高问题）：修复前「结论 · 海外」「结论 · 代理」等行的第二行被行高裁掉半截；
按 CJK 显示宽度折算行数后，两行/三行文本全部完整显示。

### 4.7 部署一致性

| 位置 | 值 |
| --- | --- |
| 本地包 | `astrbot_plugin_path_doctor-v2.5.0.zip` 92586 字节 · SHA256 `e8dfd68828cc824cc16912cc28f0186178947631c1e26a320dcbcc8769a2d547` |
| 本地源码 | `main.py` SHA256 `34345e289ac0a4c78d0b2b27f7307f15680f3f56155ecbfa83d9df7c7d35c5b6` · md5 `03aca1ee45d322ff033e391d438c15ac` |
| VM 部署 | `/home/<user>/bot-stack/astrbot/data/plugins/astrbot_plugin_path_doctor/`：`metadata.yaml` = `v2.5.0`，`main.py` md5 `03aca1ee45d322ff033e391d438c15ac`（与本地一致） |
| 启动日志 | `Loading plugin astrbot_plugin_path_doctor ...` → `Plugin path_doctor (v2.5.0) by 哦鲸鲸 …`（含「网络实测（国内/海外/代理三结论 + 代理真实出网实测）、启动自检清单 /diag startup」） |
| 备份 | 旧版在 `/home/<user>/plugin_bak_pd25/astrbot_plugin_path_doctor.bak_1030_161645/`；`plugins/` 内没有混入备份目录 |

## 5. 交付物

| 文件 | 说明 |
| --- | --- |
| `GitHub发布\05_运维诊断\path_doctor\插件包\astrbot_plugin_path_doctor-v2.5.0.zip` | 交付包（含 `README.md` / `_conf_schema.json` / `logo.png`） |
| `插件\05_运维诊断\path_doctor\astrbot_plugin_path_doctor\` | 源码目录（v2.5.0，`main.py` 单文件 + README + 配置 schema） |
| `插件\05_运维诊断\path_doctor\交付说明.md` | 版本 / 依赖 / 安装 / 变更 / 已知限制 / 回滚 |
| `插件\05_运维诊断\path_doctor\说明.md` | 项目索引（源码、包、报告位置） |
| `GitHub发布\05_运维诊断\path_doctor\规划与报告\26_…（本报告）` | 实测报告（脱敏版） |
| `astrbot_vm\方案\05_运维诊断\26_…（本报告）` | 实测报告（内部完整版） |
| `astrbot_vm\云端交付-2026-10-28\插件\astrbot_plugin_path_doctor-v2.5.0.zip` | 云端交付件（v2.4.0 包保留不删） |

## 6. 云端接入（启动自检清单）

`astrbot_vm\云端交付-2026-10-28\00_安装与配置手册.md` 第三节「上线自检」已改为：

1. `/diag startup` → 6 项清单一次看完（**② 共享目录、③ 网络** 就在这里）；
2. 有 ❌/⚠️ 时按清单里的对策与排错顺序逐项复核，重点两项：
   - `/diag shared` → 「✅ others 可进可读」= 共享目录对端可见（EACCES 判据）；
   - `/diag net` → 国内 / 海外 / 代理三条结论（海外不通先查节点，代理「端口在但出网失败」= 换节点 / 查订阅）；
3. 其余（`/jmstatus`、各插件核心命令、`/clash status`）保持不变。

同时更新了该手册 §二 path_doctor 行（v2.4.0 → v2.5.0）与 `01_交付清单.md` 的 path_doctor 版本 / SHA256。

## 7. 遗留与下一步

1. **宿主 TUN 场景下的代理端口不可见**：容器看不到宿主绑在 `127.0.0.1` 的代理端口（本 VM 即如此），
   只能靠「网关 9090 有内核 API」间接说明；若要让 `/diag net` 直接读节点状态，需要用户提供密钥配置项
   （当前设计**故意不读密钥**，也不打印节点名 —— 节点状态归 clashmate 面板）。
2. **代理 API 的节点级结论**：现在是「HTTP 401 = 内核在、需鉴权」，不含「当前节点/延迟」。
   若需要，可加一个可选配置 `proxy_api`（含 secret），但那会引入密钥落盘，建议先问用户。
3. **真实 QQ / cloud 复验**：`/diag net` 的三结论在 cloud 上要复核一次（那边代理是容器内 clashmate，
   端口 7890/9090 与 VM 的宿主 TUN 不是一回事，正好覆盖「本机端口可用」这条主路径）。
4. **IPv6**：仍只测 IPv4；如需可加 `AAAA` 解析与 v6 连通（当前环境 v6 关闭，属「做不到」）。
5. 未做的事：不改业务插件、不挂 docker.sock、不改 AstrBot 核心配置、不改 mock 桩。

## 8. 给统筹的复核清单（建议顺序）

```text
1. /diag net        —— 看是否给出 国内 / 海外 / 代理 三条结论 + 出网实测
2. /diag startup    —— 看清单里是否有 ② 共享目录（shared）与 ③ 网络（net）
3. /diag            —— 总览四层结论与问题清单不回归
4. /diag shared     —— 与 startup ② 结论一致（同源）
5. /diag json | md  —— net 结构与两节新内容；旧字段没丢
6. /diag report     —— 7 页 Excel：网络页有代理区块、总览页有「国内/海外/代理」行
7. /diag pack       —— 归档 zip 内容不变（报告 xlsx + snapshot + caps + manifest）
8. cloud 上重跑 1、2 —— 容器内代理场景（clashmate mihomo）下的三结论
```

> 交接文档 `交接\03_VM环境\02_虚拟机工作台-交接-给其他对话.md` §7.1 的命令表已在本轮同步到 v2.5.1
> （见 §9）。

---

## 9. v2.5.1 追加（2026-10-30）：帮助与指令清理

> 包 `astrbot_plugin_path_doctor-v2.5.1.zip`，**95221 字节**，
> SHA256 `386cc28e974d077dc88f45c2d11396b2386c3d5b4ad24b7bc5ce15d8fbbf1f04`；
> `main.py` SHA256 `383d446c…66798c` · md5 `e09c33f3e93327d4139ed44a3679ab20`
> 已部署到 VM 并重启验证（容器内 `main.py` md5 与本地一致，启动日志 `Plugin path_doctor (v2.5.1)`）。

### 9.1 起因

用户问「有没有帮助类的指令或者对话？」并把「无效的指令」交给我清理。于是先做了一次**静态指令审计**，
再据此补齐帮助能力。

### 9.2 审计结果（脚本 `archive\tmp\pd_cmd_audit.py`）

| 发现 | 证据 | 处理 |
| --- | --- | --- |
| 死别名 `all` | 分派链第 1 个分支 `("path","overview","all")` 先命中，第 17 个分支 `pack` 里的 `all` **永远不可达** | 删掉 pack 分支里的 `all`；`all` 仍是总览别名，**行为零变化**（实测 `/diag all` 仍出总览） |
| help 里 `pack` 说明错误 | 写「txt+md+json+caps 打成一个 zip」，实际产物是「体检报告.xlsx + snapshot.json + caps.json + manifest.json」 | 改正 |
| `export/exportall` 缺格式参数 | 代码支持 `xlsx\|json\|csv`，help 没写 | 补上 |
| README 命令一览重复 | `/diag caps` 出现两行 | 删一行 |
| 无单命令帮助 | `/diag help` 只有总表；不存在的子命令静默给表 | v2.5.1 新增（见下） |

新增的**交叉校验**（防以后再冒出死写法）：分派里认识的每个写法都要有 help 条目、别名表指向的 canonical 必须存在
——当前 **0 例外**；「被前面分支遮蔽」的别名 **0 个**。

### 9.3 新增能力

1. **`/diag help <子命令>`**：输出「作用 / 示例 / 相关命令」，例 `/diag help net`；
   **别名也能查**（`/diag help proxy` → net、`/diag help 自检` → startup）。
2. **未知子命令先提示**：`⚠️ 没认出来「X」这个子命令` + 「怎么看单命令」+ 整张命令表。
3. **显式帮助入口**：`/diag help`、`/diag 帮助`、`/diag ?`、`/diag ？` 等价；`/diag help` 无参数时输出与旧版一致。
4. **明确边界（回答「有没有对话式的」）**：**没有**多轮对话/向导式命令——诊断全是一次性问答
   （发一条 → 回一份），交互只体现在输出的「对策 / 排错顺序」里；要做向导得另加带状态机的命令，本轮未做。

### 9.4 v2.5.1 实测（VM，mock 群 900001，原样回执摘要）

| 命令 | 结果 |
| --- | --- |
| `/diag help` | ✅ 总表（v2.5.1，与旧版结构一致，`pack` 说明已改正、`export` 补了格式参数） |
| `/diag help net` | ✅ `📖 /diag net` + 作用 / 示例 / 相关（`/diag startup · /diag proto`） |
| `/diag help proxy` | ✅ 别名解析到 net，输出同上 |
| `/diag help 没这个命令` | ✅ `⚠️ 没找到子命令「没这个命令」` + 21 个可用子命令列表 |
| `/diag 帮助` / `/diag ?` | ✅ 总表 |
| `/diag 没认出来的东西` | ✅ `⚠️ 没认出来「没认出来的东西」这个子命令` + 怎么看单命令 + 总表 |
| `/diag all` | ✅ 仍出总览（死别名删除后行为不变） |
| `/diag` | ✅ 总览（19 目录 / 已加载 17 / 警告 2；共享目录 835 文件 756.2M） |
| `/diag zip` | ✅ 归档 zip 20K（4 个文件），`pack` 分支正常 |

### 9.5 本轮同步的文档（含工作区外）

- 插件：`README.md`（命令一览去重 + help 段 + 版本历史 v2.5.1）、`交付说明.md`、源码 docstring、`metadata.yaml`。
- 发布：`发布状态.md`、`GitHub发布\_发布状态表.md`（v2.5.1 + 复测结论）。
- 云端：`00_安装与配置手册.md`（§一 装 v2.5.1、§二 行、§三 提 `/diag help <子命令>`）、`01_交付清单.md`（版本/SHA256）、
  `云端交付-2026-10-28\插件\astrbot_plugin_path_doctor-v2.5.1.zip`（v2.4.0 / v2.5.0 包保留不删）。
- 状态位：`F:\Test\AGENTS.md` 当前已知状态、`交接\01_总体框架\00_总体框架.md` §五。
- **`交接\03_VM环境\02_虚拟机工作台-交接-给其他对话.md` §7 表与 §7.1 命令表已改成 v2.5.1**
  （补 `/diag startup`、`/diag help`、`/diag net` 三结论与代理四档结论说明）。

---

## 10. v2.5.2 追加（2026-10-31）：探测抗抖

> 包 `astrbot_plugin_path_doctor-v2.5.2.zip`，**95907 字节**，
> SHA256 `420bdb10cbe80b385f9bed5e58433100bbe5a8199c9e0d2ab20eea859e0cf840`；
> `main.py` SHA256 `4b6242bfce5e920ab592c5ac2db31f18db4c15465392e8a430f585a07962aa06` · md5 `1f9d775e3c25dd3c0f7bc672c014ed3f`
> 已部署到 VM 并重启验证（容器内 `main.py` md5 与本地一致，启动日志 `Plugin path_doctor (v2.5.2)`）。

### 10.1 起因：冒烟复测抓到一次「假失败」

v2.5.1 交付后按用户要求做当前状态冒烟（01:36）：`/diag`、`/diag startup`、`/diag help net` 全正常，
`docker logs` 里 path_doctor 的 ERROR/Traceback/Exception 计数为 **0**，但 `/diag net` 出现：

```text
【HTTPS】
   ✅ 国内     https://www.baidu.com 200 · 165ms
   ❌ 海外     https://github.com URLError: <urlopen error _ssl.c:993: The handshake operation timed out>
【代理】… 出网实测 ✅ socks5h://127.0.0.1:7890 → https://www.gstatic.com/generate_204 204 · 279ms
【结论】… 综合 ❌ TCP 都通，但 海外 的 HTTPS 失败 —— 多半是代理 / TLS 层 · 联网可用性 ✅ 可用
```

同目标历史耗时（同一台 VM、同一条链路）：

| 时间 | 海外 HTTPS 耗时 |
| --- | --- |
| v2.4.0 基线 | 3828ms |
| v2.5.0 实测 | 6151ms / 5105ms / 738ms |
| 本轮冒烟 | **超时（>6000ms）** |

**结论**：0.7s～6.2s 的抖动下，「6 秒 × 1 次」必然偶发假失败——不是网络坏了，是探测本身太脆弱。

### 10.2 修复

| 项 | 处理 |
| --- | --- |
| 超时与重试 | HTTPS 两段由「6s × 1 次」→「**8s × 最多 2 次**」，两次间隔 0.4s |
| 重试可见 | 第 2 次才成功：文本 `✅ … 200 · 435ms · 第 2 次成功（首次 TimeoutError: …）`；JSON 增 `attempts` / `note` |
| 判定收紧 | 两次都失败才判 ❌，综合结论写「**重试后仍失败**」，不再把「慢」当「坏」 |
| Excel | 网络页 HTTPS 行备注带重试信息 |

### 10.3 验证

**① VM 连跑 3 次 `/diag net`（01:39–01:40）**：三次全部 `✅ 内外网均可达`，
海外 HTTPS **435ms / 422ms / 425ms**、代理出网 441/260/275ms —— 结论稳定。原样回执摘录：

```text
【HTTPS】
   ✅ 国内     https://www.baidu.com 200 · 155ms
   ✅ 海外     https://github.com 200 · 435ms
【结论】国内 ✅ 可达 · 海外 ✅ 直连可达（DNS 为 fake-ip：出网已走宿主 TUN 代理） ·
       代理 ✅ 可用（socks5h://127.0.0.1:7890 → 海外 204 · 441ms） · 综合 ✅ 内外网均可达
```

**② 定向测试重试两条路径**（容器内跑真实 `_net_probe` + 模拟取回失败，`archive\tmp\pd_retry_test.py`）：

```text
A 首次超时、第二次成功 → 两段 ok=True attempts=2 note='第 2 次成功（首次 TimeoutError: simulated handshake timeout）'
                        综合「内外网均可达」                                                        PASS
B 两次都失败           → 两段 ok=False attempts=2
                        综合「TCP 都通，但 国内/海外 的 HTTPS 失败（重试后仍失败）—— 多半是代理 / TLS 层」  PASS
```

### 10.4 本轮同步的文档

`README.md`（版本历史 v2.5.2 + 已知限制改为 8s×2）、`交付说明.md`、源码 docstring、`metadata.yaml`、
`发布状态.md`、`GitHub发布\_发布状态表.md`、云端手册（§一/§二）、`01_交付清单.md`（版本/SHA256）、
`AGENTS.md`、`00_总体框架.md §五`、交接 §7/§7.1、`27_…交付通知.md`。

> 过程小事故（已闭环）：本次为图快用 PowerShell `Get-Content/Set-Content` 改版本号，**引入了 UTF-8 BOM**
> （项目记忆里警告过的坑）。已用 Python 以 `utf-8-sig` 读、`utf-8` 无 BOM 写回，并核对行尾仍是 LF
> （包内 4316 行、无 CRLF）；终检脚本确认所有文件 BOM=0、mojibake=0。此后改源码一律用编辑工具 / Python。
