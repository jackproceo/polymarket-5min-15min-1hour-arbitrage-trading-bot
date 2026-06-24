# Git 更新与服务器部署流程

> 本地修改 → 推送到 GitHub → 服务器拉取 → 容器重建

---

## 一、本地提交并推送

```bash
# 1. 查看改动的文件
git status
git diff

# 2. 添加改动
git add .
# 或只添加特定文件：git add 文件名

# 3. 提交
git commit -m "描述改了什么"

# 4. 推送到 GitHub
git push
```

---

## 二、服务器拉取并重建容器

```bash
# 1. SSH 登录到服务器后，进入项目目录
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot

# 2. 拉取最新代码
git pull

# 3. 重新构建镜像（有缓存的层不会重下，很快）
docker compose build

# 4. 重建并重启容器（不中断的滚动更新）
docker compose up -d --force-recreate
```

---

## 三、验证

```bash
# 查看容器运行状态
docker compose ps

# 查看启动日志，确认无报错
docker compose logs --tail=50

# 单独查看某个机器人
docker compose logs bot-vwap --tail=20
docker compose logs bot-meridian --tail=20
docker compose logs bot-ptb --tail=20
```

---

## 四、完整示例

```bash
# 本地
git add .
git commit -m "fix: VWAP web dashboard host 0.0.0.0 for Docker access"
git push

# 服务器
ssh root@你的服务器IP
cd /opt/polymarket-5min-15min-1hour-arbitrage-trading-bot
git pull
docker compose build
docker compose up -d --force-recreate
```

> **注意**：配置文件（`.env`、`config.json`、`config.env`）已被 `.gitignore` 排除，`git pull` **不会覆盖**你的本地配置，密钥和参数安全。
