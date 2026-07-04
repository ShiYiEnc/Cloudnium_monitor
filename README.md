[README.md](https://github.com/user-attachments/files/29660596/README.md)
# Cloudnium_monitor
Cloudnium补货提醒，网页内容变化提醒。由claude code与deepseek v4 pro模型完成。
# Cloudnium VPS 补货监控

自动监控 [Cloudnium](https://portal.cloudnium.net) VPS 产品补货情况，支持多种通知方式。

## ✨ 功能

-   🔍 自动抓取 WHMCS 产品页面，识别库存状态
-   📧 多渠道通知：邮件 / Bark / Telegram / Discord / Server酱 / 桌面弹窗
-   🔄 支持同时监控多个页面、多个价格
-   ⚡ curl + requests 双重抓取兜底
-   🛡 连续失败告警 & 恢复通知
-   🐧 支持 systemd 持久化部署

## 📋 环境要求

-   Python 3.7+
-   curl（系统自带即可）
-   可选：`pip install requests`（备用抓取）

## 🚀 快速开始

### 1. 下载

```bash
wget https://your-host/cloudnium_monitor.py
# 或直接复制文件到服务器
```

### 2. 首次运行

```bash
python3 cloudnium_monitor.py
```

首次运行会自动生成配置文件 `cloudnium_monitor_config.json`。

### 3. 配置通知

编辑 `cloudnium_monitor_config.json`，至少开启一种通知方式：

#### 邮件通知（推荐）

```json
"email": {
    "enabled": true,
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "smtp_user": "你的QQ@qq.com",
    "smtp_pass": "QQ邮箱授权码",
    "to": "接收通知的邮箱",
    "use_ssl": true
}
```

> QQ 邮箱授权码获取：mail.qq.com → 设置 → 账户 → POP3/SMTP 服务 → 生成授权码

#### 常用邮箱 SMTP

| 邮箱 | smtp_host | 端口 |
|------|-----------|------|
| QQ | smtp.qq.com | 465 |
| 163 | smtp.163.com | 465 |
| Gmail | smtp.gmail.com | 465 |
| Outlook | smtp-mail.outlook.com | 587 |

#### 其他通知渠道

```json
// Bark (iOS 推送，免费)
"bark": {
    "enabled": true,
    "key": "你的BarkKey",
    "server": "https://api.day.app"
}

// Telegram Bot
"telegram": {
    "enabled": true,
    "bot_token": "123456:ABC-DEF",
    "chat_id": "你的ChatID"
}

// Discord Webhook
"discord": {
    "enabled": true,
    "webhook": "https://discord.com/api/webhooks/..."
}

// Server酱 (微信推送)
"serverchan": {
    "enabled": true,
    "sendkey": "你的SendKey"
}
```

## 🖥 命令行用法

```bash
# 默认：检查 LA $2.99 VPS（一次性）
python3 cloudnium_monitor.py

# 指定 URL 和名称
python3 cloudnium_monitor.py --name Buffalo \
  --url "https://portal.cloudnium.net/cart/buffalo--ny----kvm-vps/"

# 指定价格
python3 cloudnium_monitor.py --price 3.99

# 后台持续监控（每5分钟）
python3 cloudnium_monitor.py --daemon 300

# 完整示例
python3 cloudnium_monitor.py \
  --name "LA" \
  --url "https://portal.cloudnium.net/cart/los-angeles---kvm-vps/" \
  --price 2.99 \
  --daemon 300

# 调试：只看页面有什么产品
python3 cloudnium_monitor.py --dump

# 调试：指定页面
python3 cloudnium_monitor.py --dump \
  --url "https://portal.cloudnium.net/cart/buffalo--ny----kvm-vps/"
```

### 参数说明

| 参数 | 简写 | 说明 |
|------|------|------|
| `--url` | `-u` | 目标页面 URL，默认 LA KVM VPS |
| `--name` | `-n` | 监控名称，用于通知标题和状态文件名 |
| `--price` | `-p` | 目标价格，默认 2.99 |
| `--daemon` | `-d` | 后台模式，参数为间隔秒数（最低 30） |
| `--dump` | | 抓取页面保存 HTML，不检查库存 |
| `--quiet` | `-q` | 安静模式 |

## 🖥 服务器部署 (systemd)

### 单服务

```bash
cat > /etc/systemd/system/cloudnium-monitor.service << 'EOF'
[Unit]
Description=Cloudnium VPS Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/cloudnium_monitor.py \
  --name LA \
  --url "https://portal.cloudnium.net/cart/los-angeles---kvm-vps/" \
  --price 2.99 \
  --daemon 300
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cloudnium-monitor
systemctl start cloudnium-monitor
```

### 多页面监控

创建多个 service 文件，分别指向不同页面：

```bash
# LA 服务
cat > /etc/systemd/system/cloudnium-la.service << 'EOF'
[Unit]
Description=Cloudnium LA Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/cloudnium_monitor.py \
  --name LA --url "https://portal.cloudnium.net/cart/los-angeles---kvm-vps/" \
  --price 2.99 --daemon 300
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

# Buffalo 服务
cat > /etc/systemd/system/cloudnium-buffalo.service << 'EOF'
[Unit]
Description=Cloudnium Buffalo Monitor
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/cloudnium_monitor.py \
  --name Buffalo --url "https://portal.cloudnium.net/cart/buffalo--ny----kvm-vps/" \
  --price 2.99 --daemon 300
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable cloudnium-la cloudnium-buffalo
systemctl start cloudnium-la cloudnium-buffalo
```

### 服务管理命令

```bash
systemctl status cloudnium-la          # 查看状态
journalctl -u cloudnium-la -f          # 实时日志
systemctl stop cloudnium-la            # 停止
systemctl restart cloudnium-la         # 重启
systemctl disable cloudnium-la         # 取消开机自启
```

## 🔔 通知逻辑

脚本只在特定事件发生时通知，不会频繁打扰：

| 事件 | 行为 |
|------|------|
| 缺货 → 有货 | 🎉 立即通知 |
| 有货 → 缺货 | 📊 状态变化通知 |
| 连续抓取失败 5 次 | ⚠️ 告警（可能被拦截） |
| 之后每失败 10 次 | 再次提醒 |
| 失败后恢复正常 | ✅ 恢复通知 |
| 监控启动 | 测试通知（仅 daemon 模式） |

## 📁 生成的文件

```
当前目录/
├── cloudnium_monitor.py           # 脚本本体
├── cloudnium_monitor_config.json  # 通知配置
├── cloudnium_state_LA.json       # LA 检查状态（自动生成）
├── cloudnium_state_Buffalo.json  # Buffalo 检查状态（自动生成）
└── cloudnium_debug_*.html        # 调试页面快照（自动生成）
```

## 🧹 卸载

```bash
systemctl stop cloudnium-la cloudnium-buffalo 2>/dev/null
systemctl disable cloudnium-la cloudnium-buffalo 2>/dev/null
rm -f /etc/systemd/system/cloudnium-*.service
systemctl daemon-reload
rm -f /root/cloudnium_monitor*
```

## 📄 License

MIT
