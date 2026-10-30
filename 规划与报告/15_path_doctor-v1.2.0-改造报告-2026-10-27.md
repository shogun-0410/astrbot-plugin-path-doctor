# path_doctor v1.1.0 → v1.2.0 正式化报告（2026-10-27）

> 定位：运维诊断工具，独立插件（不并入群文件管理）。
> 依据：`方案/08_方案-检测插件正式化.md`；环境：VM 测试 VM + mock OneBot 群 900001。

## 改造内容

1. **命令重组（语义化）**

   | 命令 | 作用 |
   | --- | --- |
   | `/diag` | 总览：路径逐级权限 + 写入实测 + 磁盘 + 进程身份 + 环境变量 |
   | `/diag shared [dir]` | **共享目录对端可见性**：写探针（目录 777/文件 666），逐级判定 others 可进可读，给出「协议端能否读」结论 |
   | `/diag writetest [dir]` | 写入实测 |
   | `/diag report` | 完整报告发 txt 文件 |

   旧写法 `path`/`perm`/`write`/`txt` 仍兼容（perm→shared、write→writetest、txt→report）。

2. **`/diag shared` 核心逻辑**：不再用 `su nobody` 这种容器里不可靠的跨 uid 实测，改为
   「权限位 + 从 `/` 到探针的逐级 others-x/other-r 判定」，输出每一级 mode/uid 与结论，
   并指出是哪一级卡住——这是 cloud 上 EACCES 问题最直接的判据。

3. **权限控制**：新增 `admin_only`（默认 true）+ `admin_list`；`admin_list` 为空时放行
   （避免默认配置把工具锁死），非管理员提示「仅管理员可用」。

4. **代码清理**：顶层 import（去掉 `_import_plain`/`_import_file` 延迟导入与未用 `json`）；
   删除空子类 `PathDoctorPlugin`，`@register` 直接挂 `PathDoctor`；`__init__.py` 同步。

5. **正式化配套**：新增 `_conf_schema.json`（admin_only / admin_list / shared_dir）；
   metadata 补齐 `support_platforms`、`astrbot_version`、desc。

## VM 实测（mock）

| 验收项 | 结果 |
| --- | --- |
| `/diag` 总览 | ✅ 报告 v1.2.0，路径逐级权限/写入/磁盘/身份/框架路径/环境变量齐全 |
| `/diag shared` | ✅ 探针目录 777、文件 666，5 级权限链逐级 ✅，结论「✅ others 可进可读」，探针已清理 |
| `/diag writetest` | ✅ 可写 + 权限回显 + 清理 |
| `/diag report` | ✅ 生成 diag_report.txt（2018 字）并发 File |
| 非管理员被拦 | ✅ admin_list=<管理员QQ> 时，10001 调 `/diag` 返回「❌ 仅管理员可用」 |
| 默认配置 | ✅ admin_list 空 → 放行（提示配置） |
| 代码质量 | ✅ 编译通过、无未定义名、无空子类 |

## 交付物

- `F:\Test\哦鲸鲸\astrbot_plugin_path_doctor-v1.2.0.zip`
  （SHA256 `3bcc3e532adfea8984d02a8017deb187f134dad795edc270058cc59b6cc05b3b`）
- 已部署到 VM（`/home/<user>/bot-stack/astrbot/data/plugins/astrbot_plugin_path_doctor/`，v1.2.0）

## 备注

- `/diag report` 的 txt 落在 `plugin_data/path_doctor/`，该目录链 755 + 文件 666，协议端可读；
  若某环境 plugin_data 链被收紧成 700，可用 `/diag shared` 的结论判断是哪级卡住。
- 后续可加：`/diag env`（过滤展示）、把 `/diag shared` 接入一键部署自检清单。
