# xhs-monitor · 小红书博主监控与管理后台

> 一个本地运行的小红书（RedNote）博主笔记监控工具 + Web 管理后台。自动按间隔轮巡你关注的博主，发现新笔记即时推送通知；并提供账号管理、博主配置、笔记记录与系统设置界面。

⚠️ **法律与合规**：本项目仅供个人学习与技术研究。请遵守小红书平台规则与相关法律规定，勿用于商业批量抓取、勿侵犯他人权益。账号 Cookie 属于敏感凭据，**切勿分享或提交到公开仓库**。

## 功能特性

- **博主监控**：按分钟级间隔轮巡指定博主，自动识别新笔记并去重（已剔除置顶笔记干扰）。
- **新笔记推送**：发现新笔记时通过可配置通道即时通知（Server 酱 / 企业微信 / 飞书 / Bark / Telegram，见 `notifier.py`）。
- **账号管理**：提供 4 种小红书账号录入方式（见下）。
- **Web 管理后台**：Vue 3 + Element Plus 单文件前端，零构建零 npm 依赖。
- **浏览器登录**：通过 CDP 驱动本机 Chrome 完成登录，绕过服务器直连的登录风控。
- **本地优先**：数据全部存在本地 SQLite，无外部服务依赖。

## 目录结构

```
xhs-monitor/
├── xhs_admin/                 # Web 管理后台（FastAPI + Vue3）
│   ├── app.py                 # 主应用（接口、鉴权、账号/博主/笔记/浏览器登录）
│   ├── browser_login.py       # 浏览器登录（CDP 驱动本机 Chrome）
│   ├── config.py              # 配置（SDK 路径 / 默认密码 / 调度间隔）
│   ├── db.py                  # SQLAlchemy 模型与建表
│   ├── notifier.py            # 推送通道
│   ├── scheduler.py           # 博主轮巡调度（基于 APScheduler）
│   ├── xhs_client.py          # Spider_XHS SDK 封装（抓取 / Cookie 校验）
│   ├── requirements.txt
│   ├── run_admin.bat          # Windows 一键启动（自动清代理）
│   ├── static/index.html      # 前端单文件
│   └── data/                  # 运行时数据库（.gitkeep 占位，首次启动自动建）
├── Spider_XHS/                # 小红书纯 SDK（含本项目所需修复，源自 cv-cat/Spider_XHS）
├── README.md
├── .gitignore
└── LICENSE
```

## 工作原理

`xhs_admin` 是一个自包含的后台：它直接调用 `Spider_XHS` 的纯 SDK，用已录入账号的 Cookie 向小红书接口请求博主最新笔记。调度器（APScheduler）按各博主配置的间隔，错峰轮巡（每轮最多 3 个，避免风控），对比基线识别新笔记并入库、推送。

> 因此本仓库**不依赖任何第三方采集平台**（如 Apify / Rnote）。你只需在后台录入有效的小红书账号 Cookie 即可。

## 环境要求

- **Python 3.12+**（本项目在 3.13 验证）
- **Google Chrome**（浏览器登录功能需要；数据抓取也会用到系统 Chrome 的指纹环境）
- 操作系统：Windows（提供 `run_admin.bat`）；其他系统用 `uvicorn` 命令启动
- 代理：后台请求需清除系统代理（见启动说明）

## 安装

```bash
# 1. 克隆
git clone <your-repo-url> xhs-monitor && cd xhs-monitor

# 2. 创建虚拟环境并安装依赖
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r xhs_admin/requirements.txt
pip install -r Spider_XHS/requirements.txt
```

> 依赖要点：两处都锁定 `curl_cffi==0.16.3`（指纹与请求逻辑已针对本项目调校，请勿随意升级，见“已知限制”）。安装源建议用国内镜像（如阿里云）以避免缺包（`PyExecJS` 在部分源缺失）。

## 配置

### Spider_XHS（可选）
浏览器登录不需要 Cookie；若想用 Cookie 导入方式，可复制 `Spider_XHS/.env.example` 为 `Spider_XHS/.env` 并填入 `COOKIES`。**`.env` 已被 `.gitignore` 排除，不会提交。**

### SDK 路径
`xhs_admin/config.py` 默认通过相对路径 `../Spider_XHS` 引用 SDK，仓库内解压即用。若 Spider_XHS 在其他位置，设置环境变量：
```bash
export XHS_SDK_DIR=/path/to/Spider_XHS
```

### 后台密码
默认密码 `admin123`，首次启动写入数据库，可在「系统设置」中修改。

## 启动

**Windows**：双击 `xhs_admin/run_admin.bat`（脚本自动切换目录、清理代理、定位 Python）。

**其他系统 / 手动**：
```bash
cd xhs_admin
NO_PROXY='*' uvicorn app:app --host 127.0.0.1 --port 8000
```
浏览器访问 http://127.0.0.1:8000 ，用默认密码 `admin123` 登录。

> 后台服务需清除系统代理（`NO_PROXY='*'`），否则请求可能被代理拦截导致抓取失败。

## 添加小红书账号（4 种方式）

进入「账号管理 → 添加账号」，标签页顺序：扫码登录 / 手机验证码 / Cookie 导入 / 浏览器登录。

1. **浏览器登录（推荐）**：点击「打开登录浏览器」，平台启动一个**独立 Chrome 窗口**（专属数据目录，不影响你自己的浏览器），在其中扫码或手机验证码登录；平台每 3 秒检测登录态，成功即自动读取 Cookie、校验、建号并关闭浏览器。**这是唯一稳定可靠的方式**——它把登录交给真实浏览器，绕开服务器直连的风控。
2. **扫码登录**：页面内二维码（服务器直连，**可能受风控失败**，失败时改用浏览器登录）。
3. **手机验证码**：服务器直连（**可能被风控不下发短信**，失败时改用浏览器登录）。
4. **Cookie 导入**：直接粘贴浏览器中的 Cookie 字符串。

> 默认打开「Cookie 导入」标签；**强烈推荐改用「浏览器登录」**。

## 安全注意事项

- `xhs_admin/data/*.db` 含**明文小红书 Cookie 与账号信息**，已被 `.gitignore` 排除，**切勿提交**。
- `Spider_XHS/.env` 含 `COOKIES`，已被排除。
- 本仓库**不包含任何真实凭据**，可安全公开；但其中包含登录绕过相关逻辑，建议设为私有仓库。
- 数据库首次启动由 `db.py` 的 `Base.metadata.create_all` 自动建表；`settings` 为空时后台自动重建默认密码 `admin123`。

## 已知限制 / 踩坑笔记

- **登录风控**：小红书对登录类接口（扫码换 session、发短信）的风控远严于数据接口。服务器用 `curl_cffi` 模拟浏览器直连登录会被拦截（数据抓取不受影响）。因此登录必须走真实浏览器（浏览器登录方式）。
- **`get_user_me` 游客态**：未登录时该接口仍返回 `success: true`（游客带匿名 user_id + `guest: true`）。Cookie 有效性检测必须校验 `guest` 字段，否则会把游客态误判为已登录。
- **Spider_XHS 修复**：本仓库内置的 Spider_XHS 做了三处必要修复——`curl_cffi` 锁 `0.16.3`、指纹 `chrome146 → chrome150`、以及 `curl_cffi 0.16.x` 下 `data` 不再当 raw body 发送的 body 修复。重装依赖或升级 `curl_cffi` 前请确认这些修复不丢失。
- **CDP 连接**：Python 用 `websocket-client` 连 Chrome 调试端口必须 `suppress_origin=True`（否则 Chrome 403 拒绝）；启动 Chrome 时加 `--remote-allow-origins=*` 兜底。
- **代理**：务必在启动前清除代理（`NO_PROXY='*'`），否则 SDK 请求会被代理劫持。

## 许可证

[MIT](./LICENSE)
