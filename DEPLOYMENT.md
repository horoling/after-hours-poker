# After Hours · 夜局：2–11 人私人德州牌桌

支持本机运行、普通 Linux 服务器部署和 GitHub Actions 临时公网测试。长期托管尚未配置，大陆手机网络的稳定性仍需由各位玩家实际验证。

## 本机启动

安装项目依赖并启动：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-multiplayer.txt
.venv/bin/python multiplayer_app.py
```

打开 <http://127.0.0.1:8780/friends>。创建房间后，复制邀请链接给朋友。同机链接只供本机预览，异地玩家需要公网部署后的地址。

公网部署入口为 `multiplayer_app:app`。本仓库只包含多人平台，不包含本地 AI 训练工具、模型和个人牌谱。

## 已实现

- 房主可选择 2–11 人桌，支持 7 人、9 人、11 人；至少两位有筹码的玩家入座即可开局。可在两手之间调整座位数，缩小牌桌前须让超出范围的座位离桌。
- 邀请链接含随机邀请密钥；每个浏览器取得独立座位凭据。
- 标准无限注德州、任意合法整数加注、全下、边池、平分与退回无人跟注的筹码。
- 服务器使用系统随机源洗牌；客户端只收到自己的底牌，以及结算时实际公开的牌。
- 连续保留筹码、轮庄、改昵称、两手之间补码与离桌、房主转移。
- 新房间默认每次行动 30 秒，可调整、关闭或暂停；超时可过牌则过牌，否则弃牌。
- 断线后重试连接；同浏览器重开链接恢复原座位。SQLite 持久化房间、牌局和最近 50 手结算。
- 手机布局。当前状态同步采用约 1.1 秒 HTTP 轮询；尚未使用 WebSocket。
- 新房间默认小盲 1、大盲 2、无前注；旧房间保留原有规则，可在两手之间修改。
- 可由房主添加 GTO-lite Agent 陪打。部署包含共享策略及翻牌前牌力表和 NumPy，无需模型密钥；训练服务器、模型权重及私人手牌不随部署上传。Agent 由在线页面轮询驱动，每步计算在数据库写锁之外执行。
- 新桌初始筹码与每次补码默认均为 100，真人与 Agent 使用相同买入设置。
- 行动区突出显示本轮加注总额与本次投入，½ 底池／1 底池按钮直接下注；滑杆／输入金额和 All-in 需确认。底池比例按跟注后的底池计算并遵守合法加注范围。
- 每手显示实际位置（D、SB、BB、UTG、HJ、CO 等），折牌后保持本手位置，轮庄后更新。单挑显示 D / SB。
- 电脑快捷键 F 弃牌、C 过牌／跟注、1 半池直接下注、2 满池直接下注、A 选择全下、R 确认自定义／全下。输入框、对话框、未轮到自己及重复按键不触发行动。

## 推荐先试香港服务器

六位玩家主要在中国大陆，优先短期试用香港区域的云服务器，并用所有人的实际 Wi-Fi 和移动网络验证。以手机浏览器直接访问为目标，不把 VPN 作为产品使用前提。香港线路仍受运营商、时段和跨境网络影响，不保证仅凭地域选择就稳定。

建议先使用最小规格的一台 Linux 主机，不预先长租；六人桌无需 GPU。主机需要能运行 Docker Compose，并提供持久磁盘。具体价格、区域库存和服务用途限制在购买时以服务商为准。

### Docker 部署

仓库已包含 `Dockerfile`、`compose.yaml` 和最小依赖列表。镜像只打包多人房间代码和对应网页，不包含训练模型、旧牌谱或本机房间数据。

将代码复制到服务器后：

```sh
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8780/healthz
```

Compose 将服务只绑定在服务器回环地址；数据库保存在名为 `poker-data` 的 Docker 卷中。不能使用 `docker compose down -v`，该参数会删除持久卷。

给服务器配置域名 A 记录并安装 HTTPS 反向代理。例如在宿主机上使用 Caddy，填写实际域名：

```caddyfile
poker.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8780
}
```

在 DNS 中将域名指向主机公网 IP，开放必要的 80/443 端口。HTTPS 配置成功后，用 `https://你的域名/friends` 创建房间，再分发该房间的完整邀请链接。若 DNS 使用 Cloudflare，首轮测试可先使用仅 DNS 解析，分别验证直连与代理后的实际表现。

初期可使用单进程多线程 Gunicorn。数据库访问使用 SQLite 事务；如以后扩展到多台主机，需要先替换共享状态存储，不能让各主机分别维护自己的本地数据库。

### 房间恢复与备份

座位凭据只保存在玩家浏览器的 localStorage。普通刷新和服务重启可恢复；清除网站数据、换浏览器或换手机后，当前版本无法自动找回原座位。请在新房间开始前确定使用的浏览器，不要把同一人的邀请链接在多个浏览器重复加入。

房间数据库包含完整牌局和底牌，应仅供服务器管理员访问。使用 SQLite backup API 或 `sqlite3` 的 `.backup` 备份一致快照；运行时不要只复制主数据库文件而遗漏 WAL。需要长期使用时，再增加管理员恢复座位、邀请撤销和过期房间清理。

## Cloudflare 免费方案的取舍

截至 2026-10-20 核对的官方文档：

- [Durable Objects 免费方案](https://developers.cloudflare.com/durable-objects/platform/pricing/)支持 SQLite，包含每日 100,000 次请求、5,000,000 行读取、100,000 行写入和共 5 GB 存储，还有独立的执行时长额度；超出免费额度会导致对应操作失败。账户上的其他应用也会使用这些额度。
- [Python Workers 支持 Flask](https://developers.cloudflare.com/workers/languages/python/packages/flask/)，但这不代表当前代码可以不加修改地部署。当前使用本地文件 SQLite、同步事务和 HTTP 轮询；Cloudflare 版本需要迁移存储、请求协调和连接方式，并验证 PokerKit 在其 Python 运行时的兼容性。
- 可选架构为静态页面 + Workers + 每房间一个带 SQLite 的 Durable Object + WebSocket。使用 [WebSocket Hibernation](https://developers.cloudflare.com/durable-objects/best-practices/websockets/)减少空闲连接的执行时长开销，所有消息仍须按座位过滤底牌。
- 免费额度不能与大陆网络可达性画等号。[Cloudflare 中国网络](https://developers.cloudflare.com/china-network/)是 Enterprise 客户的单独订阅；普通免费方案不包含这项能力。

因此当前建议：**先保留现有 Python 实现，在香港服务器短期试运行；若六位玩家实测 Cloudflare 网络合适，再决定是否投入 Cloudflare 适配。** 当前仓库没有声称可直接运行的 Workers 部署配置。

大陆服务器可作为另一条路线，但上线前需确认服务商接受该用途以及相关备案要求。参见[网站备案说明](https://www.alibabacloud.com/help/en/icp-filing/basic-icp-service/product-overview/icp-filing-requirements-for-a-regular-website)。

## 验证

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check web/friends.js
```

自动化测试覆盖 6/7/9/11 人完整对局及底牌隔离、邀请与权限、并发重复提交、服务重启恢复、暂停和超时、补码、座位复用时的底牌隔离、全下边池与平分、加注权、零头分配、盲注轮转、整场统计、聊天、倒计时和 Agent 陪打。

Docker 构建尚未在当前机器验证。临时公网环境已通过聊天和倒计时的接口自检；上线测试时，各位玩家仍需各自用普通手机网络进入同一房间，至少测试一整场、多次全下、切后台与重连；同时观察服务器日志、延迟和筹码总和。
