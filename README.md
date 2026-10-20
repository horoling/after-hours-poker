# 同桌 · Tongzhuo Poker

给朋友内部使用的私人德州扑克网页，支持 **2–11 人**，手机和电脑浏览器均可加入。

## 功能

- 创建私人房间，通过完整邀请链接加入，无广告。
- 支持 6／7／9／11 人等不同桌型；房主可在两手之间调整人数。
- 无限注德州：自定义合法加注、全下、主池与边池、平分及无人跟注筹码退回。
- 连续保留筹码、轮庄、改昵称、两手之间补码和离桌。
- 默认不限思考时间；可设置行动计时、暂停及超时过牌或弃牌。
- 同一浏览器刷新或断线后恢复座位；房间和牌局保存至 SQLite。
- 手机布局，保留最近 50 手结算。

规则由 PokerKit 执行，服务器校验玩家身份、行动顺序和下注金额，并按座位过滤底牌。

## 本机运行

需要 Python 3.11 或以上版本。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-multiplayer.txt
.venv/bin/python multiplayer_app.py
```

打开 <http://127.0.0.1:8780/friends> 创建房间。

## 手机与电脑在同一个 Wi-Fi 测试

```sh
HOST=0.0.0.0 .venv/bin/python multiplayer_app.py
```

电脑保留创建房间时使用的浏览器页面。在邀请链接中，将 `127.0.0.1` 替换为电脑的局域网 IP，端口、房间路径及 `#invite=...` 部分保持完整；手机浏览器打开替换后的链接，再输入另一个昵称入座。至少两位有筹码的玩家即可开局。

电脑需要保持运行；网络应允许设备互访，并允许 Python 的传入连接。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check web/friends.js
```

23 项后端测试覆盖多人对局、底牌隔离、加注权、全下边池、零头、盲注顺序、重连与并发重复提交。JavaScript 语法检查需要 Node.js。

## 部署

仓库包含 Docker 与 Docker Compose 配置。具体启动、HTTPS、持久化备份及 Cloudflare 的适配取舍见 [DEPLOYMENT.md](DEPLOYMENT.md)。

GitHub 仓库只保存源码；上传代码不会自动提供公网游戏地址。当前服务需部署到持续在线的主机。

## 当前限制

- 原型面向朋友间虚拟筹码娱乐，尚未经过商业平台级审计或长时间公网验证。
- 同步使用约 1.1 秒 HTTP 轮询。
- 座位凭据保存在浏览器本地。清除网站数据、换浏览器或换手机后，目前无法自动找回原座位。
- 未提供账号系统、邀请撤销、过期房间自动清理或多主机部署。
- 房间数据库含完整牌局及底牌，仅应由服务器管理员访问。数据库、邀请密钥、模型、个人笔记和运行日志不属于本仓库。

## 依赖

- [Flask](https://flask.palletsprojects.com/)
- [PokerKit](https://pokerkit.readthedocs.io/)
- [Gunicorn](https://gunicorn.org/)（Linux 生产部署）
