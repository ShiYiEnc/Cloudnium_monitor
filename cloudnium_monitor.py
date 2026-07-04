#!/usr/bin/env python3
"""
Cloudnium $2.99 VPS 补货监控脚本
目标页面: https://portal.cloudnium.net/cart/los-angeles---kvm-vps/

用法:
    python cloudnium_monitor.py              # 检查一次
    python cloudnium_monitor.py --daemon 300 # 每300秒检查一次（后台模式）

通知方式（按优先级）:
    1. Bark (iOS 推送) — 推荐，免费
    2. Server酱 (微信推送)
    3. 系统桌面通知
    4. 打印到控制台

配置: 复制 cloudnium_monitor_config.json.example → cloudnium_monitor_config.json
"""

import argparse
import hashlib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import traceback
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── 配置 ──────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "cloudnium_monitor_config.json"

DEFAULT_TARGETS = [
    {
        "name": "LA-1",
        "url": "https://portal.cloudnium.net/cart/los-angeles---kvm-vps/",
        "price": 2.99,
    },
]

TARGET_PRICE = 2.99  # 目标价格（美元）
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "email": {
        "enabled": False,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "smtp_user": "",
        "smtp_pass": "",
        "to": "",
        "use_ssl": True,
    },
    "bark": {
        "enabled": False,
        "key": "",          # Bark App 上的推送 key
        "server": "https://api.day.app",  # 自建服务可改
    },
    "serverchan": {
        "enabled": False,
        "sendkey": "",      # Server酱 SendKey
    },
    "desktop": {
        "enabled": True,    # 桌面通知默认开启
    },
    "discord": {
        "enabled": False,
        "webhook": "",      # Discord Webhook URL
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
    },
}


# ── 配置管理 ──────────────────────────────────────────────────

def load_config():
    """加载配置文件，不存在则创建默认配置"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        # 合并默认值（保证新增字段有默认值）
        merged = DEFAULT_CONFIG.copy()
        for section in merged:
            if section in config:
                merged[section].update(config[section])
        return merged
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"[INFO] 默认配置已创建: {CONFIG_FILE}")
        print(f"[INFO] 请编辑配置文件填入通知渠道信息，然后重新运行")
        return DEFAULT_CONFIG


def get_state_file(name):
    """根据监控名称获取状态文件路径"""
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
    return SCRIPT_DIR / f"cloudnium_state_{safe_name}.json"


def load_state(name):
    """加载上次检查的状态"""
    sf = get_state_file(name)
    if sf.exists():
        with open(sf, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(name, state):
    """保存本次检查的状态"""
    with open(get_state_file(name), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── 页面抓取 ──────────────────────────────────────────────────

def fetch_page(url, timeout=15):
    """抓取页面 HTML（curl 优先，requests 兜底）"""
    sys.stdout.write("  抓取页面中... ")
    sys.stdout.flush()

    headers = [
        ("User-Agent", USER_AGENT),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"),
        ("Cache-Control", "no-cache"),
    ]

    # ── 方法 1: curl（已验证可用） ──
    try:
        cmd = ["curl", "-sS", "--compressed", "--connect-timeout", str(timeout),
               "--max-time", str(timeout + 10)]
        for k, v in headers:
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)

        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout + 15)
        if result.returncode == 0 and result.stdout and len(result.stdout) > 500:
            print(f"完成 ({len(result.stdout):,} bytes)")
            return result.stdout
        else:
            stderr = result.stderr[:200] if result.stderr else ""
            raise RuntimeError(f"curl 失败 (code={result.returncode}, size={len(result.stdout)}): {stderr}")
    except FileNotFoundError:
        print("curl 未安装, ", end="")
    except Exception as e:
        print(f"curl 失败 ({e}), ", end="")

    # ── 方法 2: requests ──
    if HAS_REQUESTS:
        print("尝试 requests...", end=" ")
        try:
            resp = requests.get(url, headers=dict(headers), timeout=timeout)
            resp.raise_for_status()
            html = resp.text
            if html and len(html) > 500:
                print(f"完成 ({len(html):,} bytes)")
                return html
        except Exception as e:
            print(f"失败 ({e})")

    raise RuntimeError("无法抓取页面。请确保 curl 或 requests 可用，且网络通畅")


# ── HTML 解析（针对 WHMCS cart-product 页面） ─────────────────

def find_products_by_regex(html):
    """
    从 WHMCS 产品页面提取产品信息。
    目标结构: div.card.cart-product[data-value][class*="outofstock"]

    返回: [{"name": str, "price": float, "in_stock": bool, "product_id": str, ...}, ...]
    """
    products = []

    # 匹配每个产品卡片: <div class="... cart-product ..." data-value="NNN">
    # 到下一个同类卡片或结束
    card_pattern = re.compile(
        r'<div[^>]*class\s*=\s*"[^"]*cart-product[^"]*"[^>]*data-value\s*=\s*"(\d+)"[^>]*>'
        r'(.*?)'
        r'(?=<div[^>]*class\s*=\s*"[^"]*cart-product[^"]*"[^>]*data-value\s*=)',
        re.IGNORECASE | re.DOTALL
    )

    matches = card_pattern.findall(html)

    # 如果正则没匹配到（末尾卡片），尝试宽松匹配
    if not matches:
        # fallback: 逐个匹配
        card_pattern2 = re.compile(
            r'<div[^>]*class\s*=\s*"([^"]*cart-product[^"]*)"[^>]*data-value\s*=\s*"(\d+)"[^>]*>'
            r'(.+?)'
            r'</div>\s*(?:</div>\s*)?</div>\s*</div>',
            re.IGNORECASE | re.DOTALL
        )
        for class_attr, pid, body in card_pattern2.findall(html):
            _parse_one_card(products, class_attr, pid, body)
    else:
        for pid, body in matches:
            # 从匹配的 div 中提取 class 属性
            class_match = re.search(
                r'<div[^>]*class\s*=\s*"([^"]*cart-product[^"]*)"',
                body if body else html, re.IGNORECASE
            )
            # 实际上 pid 已经是从外层 div 提取的，我们需要在 html 中定位该卡片
            # 重新设计：直接从外层匹配中获取 class
            pass

        # 改用更可靠的方法：逐卡片解析
        products.clear()
        _parse_all_cards(html, products)

    return products


def _parse_all_cards(html, products):
    """解析所有 WHMCS cart-product 卡片"""
    # 先找到每个卡片的起始位置
    card_starts = []
    for m in re.finditer(
        r'<div[^>]*class\s*=\s*"([^"]*cart-product[^"]*)"[^>]*data-value\s*=\s*"(\d+)"',
        html, re.IGNORECASE
    ):
        card_starts.append({
            "pos": m.start(),
            "class": m.group(1),
            "product_id": m.group(2),
        })

    for i, card in enumerate(card_starts):
        start = card["pos"]
        end = card_starts[i + 1]["pos"] if i + 1 < len(card_starts) else len(html)
        body = html[start:end]

        _parse_one_card(products, card["class"], card["product_id"], body)


def _parse_one_card(products, class_attr, product_id, body):
    """解析单个产品卡片"""
    # ── 库存状态 ──
    cls_lower = class_attr.lower()
    has_outofstock_class = "outofstock" in cls_lower
    has_outofstock_div = "product-out-of-stock" in body.lower()
    has_outofstock_badge = "cart-product-outofstock-badge" in body.lower()

    in_stock = not (has_outofstock_class or has_outofstock_div or has_outofstock_badge)

    # ── 产品名称 ──
    name = product_id  # fallback
    name_match = re.search(r'<h4[^>]*>([^<]+)</h4>', body, re.IGNORECASE)
    if name_match:
        name = name_match.group(1).strip()

    # ── 月付价格 ──
    monthly_price = None
    price_match = re.search(
        r'product-price\s+cycle-m[^>]*>\s*\$?(\d+\.?\d*)',
        body, re.IGNORECASE
    )
    if price_match:
        monthly_price = float(price_match.group(1))
    else:
        # fallback: 找所有价格中的第一个
        all_prices = re.findall(r'\$\s*(\d+\.?\d*)', body)
        if all_prices:
            monthly_price = float(all_prices[0])

    # ── 规格信息 ──
    specs = {}
    spec_rows = re.findall(
        r'<div[^>]*text-right[^>]*>([^<]+)</div>\s*<div[^>]*text-left[^>]*>([^<]+)</div>',
        body, re.IGNORECASE
    )
    for label, value in spec_rows:
        specs[label.strip()] = value.strip()

    products.append({
        "name": name,
        "product_id": product_id,
        "price": monthly_price,
        "in_stock": in_stock,
        "specs": specs,
        "raw_context": body[:500],
    })


# ── 通知发送 ──────────────────────────────────────────────────

def notify_bark(config, title, body):
    """通过 Bark 发送 iOS 推送"""
    try:
        bark = config["bark"]
        url = f"{bark['server'].rstrip('/')}/{bark['key']}/{title}/{body}"
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[WARN] Bark 通知失败: {e}")
        return False


def notify_serverchan(config, title, body):
    """通过 Server酱 发送微信推送"""
    try:
        sc = config["serverchan"]
        url = f"https://sctapi.ftqq.com/{sc['sendkey']}.send"
        import urllib.parse
        data = urllib.parse.urlencode({
            "title": title,
            "desp": body,
        }).encode()
        req = Request(url, data=data, method="POST")
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[WARN] Server酱通知失败: {e}")
        return False


def notify_desktop(title, body):
    """桌面通知：Windows 弹出对话框 / macOS 通知中心 / Linux notify-send"""
    try:
        if sys.platform == "win32":
            # Windows: 用 ctypes 直接调 Win32 MessageBoxW（可靠弹出）
            import ctypes
            MB_OK = 0x00000000
            MB_ICONINFORMATION = 0x00000040
            MB_SYSTEMMODAL = 0x00001000  # 置顶，确保能看到
            ctypes.windll.user32.MessageBoxW(
                0, str(body), str(title),
                MB_OK | MB_ICONINFORMATION | MB_SYSTEMMODAL
            )
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run([
                "osascript", "-e",
                f'display notification "{body}" with title "{title}"'
            ], timeout=10)
        else:
            # Linux: notify-send
            import subprocess
            subprocess.run(["notify-send", title, body], timeout=10)
        return True
    except Exception as e:
        print(f"[WARN] 桌面通知失败: {e}")
        return False


def notify_email(config, title, body):
    """通过 SMTP 发送邮件通知"""
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled"):
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = email_cfg["smtp_user"]
        msg["To"] = email_cfg["to"]
        msg.set_content(body)

        ctx = ssl.create_default_context()
        if email_cfg.get("use_ssl", True):
            with smtplib.SMTP_SSL(
                email_cfg["smtp_host"],
                email_cfg["smtp_port"],
                context=ctx,
                timeout=15
            ) as server:
                server.login(email_cfg["smtp_user"], email_cfg["smtp_pass"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(
                email_cfg["smtp_host"],
                email_cfg["smtp_port"],
                timeout=15
            ) as server:
                server.starttls(context=ctx)
                server.login(email_cfg["smtp_user"], email_cfg["smtp_pass"])
                server.send_message(msg)
        return True
    except Exception as e:
        print(f"[WARN] 邮件通知失败: {e}")
        return False


def notify_discord(config, title, body):
    """通过 Discord Webhook 发送通知"""
    try:
        import json as _json
        payload = _json.dumps({
            "content": f"**{title}**\n{body}"
        }).encode()
        req = Request(config["discord"]["webhook"], data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"[WARN] Discord 通知失败: {e}")
        return False


def notify_telegram(config, title, body):
    """通过 Telegram Bot 发送通知"""
    try:
        tg = config["telegram"]
        text = f"*{title}*\n{body}"
        import urllib.parse
        url = (
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage?"
            f"chat_id={tg['chat_id']}&"
            f"text={urllib.parse.quote(text)}&"
            f"parse_mode=Markdown"
        )
        req = Request(url, method="GET")
        with urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[WARN] Telegram 通知失败: {e}")
        return False


def send_notification(config, title, body):
    """依次尝试所有已启用的通知渠道"""
    sent = False

    if config.get("email", {}).get("enabled") and config.get("email", {}).get("smtp_user"):
        if notify_email(config, title, body):
            print(f"[OK] 邮件已发送")
            sent = True

    if config["desktop"]["enabled"]:
        if notify_desktop(title, body):
            print(f"[OK] 桌面通知已发送")
            sent = True

    if config["bark"]["enabled"] and config["bark"]["key"]:
        if notify_bark(config, title, body):
            print(f"[OK] Bark 通知已发送")
            sent = True

    if config["serverchan"]["enabled"] and config["serverchan"]["sendkey"]:
        if notify_serverchan(config, title, body):
            print(f"[OK] Server酱通知已发送")
            sent = True

    if config["discord"]["enabled"] and config["discord"]["webhook"]:
        if notify_discord(config, title, body):
            print(f"[OK] Discord 通知已发送")
            sent = True

    if config["telegram"]["enabled"] and config["telegram"]["bot_token"]:
        if notify_telegram(config, title, body):
            print(f"[OK] Telegram 通知已发送")
            sent = True

    if not sent:
        print(f"[INFO] 没有可用的通知渠道，仅输出到控制台")
        print(f"  → {title}: {body}")

    return sent


# ── 核心检查逻辑 ──────────────────────────────────────────────

def check_once(config, verbose=True, target_url=None, target_name="VPS", target_price=2.99):
    """执行一次检查，返回检查结果"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = load_state(target_name)
    prev_status = state.get("last_status")  # "in_stock" | "out_of_stock" | "unknown" | None

    if verbose:
        print(f"[{now}] [{target_name}] 检查 ${target_price} VPS...")
        print(f"  目标: {target_url}")

    try:
        html = fetch_page(target_url)
    except Exception as e:
        error_msg = f"页面抓取失败: {e}"
        fail_count = state.get("consecutive_failures", 0) + 1
        print(f"[ERROR] [{target_name}] {error_msg}  (连续失败 {fail_count}/5)")

        if fail_count == 5:
            send_notification(
                config,
                f"⚠️ Cloudnium {target_name} 连续失败5次",
                f"{target_name} 页面抓取已连续失败 {fail_count} 次\n"
                f"最新错误: {error_msg}\n时间: {now}"
            )
        elif fail_count > 5 and fail_count % 10 == 0:
            send_notification(
                config,
                f"Cloudnium {target_name} 已连续失败 {fail_count} 次",
                f"最新错误: {error_msg}\n时间: {now}"
            )

        state["consecutive_failures"] = fail_count
        state["last_error"] = error_msg
        state["last_error_time"] = now
        save_state(target_name, state)
        return {"status": "error", "error": error_msg}

    # 重置失败计数
    prev_failures = state.get("consecutive_failures", 0)
    if prev_failures >= 5:
        send_notification(
            config,
            f"✅ Cloudnium {target_name} 已恢复",
            f"页面抓取恢复正常，之前连续失败 {prev_failures} 次。\n时间: {now}"
        )
    state["consecutive_failures"] = 0
    state.pop("last_error", None)
    state.pop("last_error_time", None)

    # 保存 HTML 用于调试
    debug_file = SCRIPT_DIR / f"cloudnium_debug_{target_name.replace(' ', '_')}.html"
    with open(debug_file, "w", encoding="utf-8") as f:
        f.write(html)

    # 解析产品
    products = find_products_by_regex(html)

    if verbose:
        print(f"  页面共有 {len(products)} 个产品:")
        for p in products:
            stock_icon = "✅" if p["in_stock"] else ("❌" if p["in_stock"] is False else "❓")
            price_str = f"${p['price']}/月" if p["price"] else "价格未知"
            status_str = "有货" if p["in_stock"] else ("缺货" if p["in_stock"] is False else "未知")
            marker = " ★目标" if (p["price"] and abs(p["price"] - target_price) < 0.01) else ""
            print(f"    {stock_icon} {p['name']:<6} {price_str:<12} {status_str}{marker}")

    if not products:
        warning = (f"[{target_name}] 未解析到任何产品卡片。"
                   f"页面结构可能已变化，请检查 {debug_file}")
        print(f"[WARN] {warning}")

        all_prices = re.findall(r'\$\s*(\d+\.?\d*)', html)
        prices_found = sorted(set(float(p) for p in all_prices), reverse=True)[:20]
        print(f"[DEBUG] 页面中所有价格: {prices_found}")

        if state.get("last_status") != "warning":
            send_notification(config, f"Cloudnium {target_name} 监控警告", warning)
        state["last_status"] = "warning"
        save_state(target_name, state)
        return {"status": "warning", "message": warning, "prices_found": prices_found}

    # 找目标产品
    target_product = None
    for p in products:
        if p["price"] and abs(p["price"] - target_price) < 0.01:
            target_product = p
            break

    if target_product is None:
        all_prices = [p["price"] for p in products if p["price"]]
        info = f"[{target_name}] 未找到 ${target_price} 产品，当前产品价格: {sorted(all_prices)}"
        print(f"[INFO] {info}")
        state["last_status"] = "not_found"
        save_state(target_name, state)
        return {"status": "not_found", "message": info}

    # 判断库存
    new_status = (
        "in_stock" if target_product["in_stock"] is True
        else "out_of_stock" if target_product["in_stock"] is False
        else "unknown"
    )

    result = {
        "status": new_status,
        "product": target_product,
        "time": now,
    }

    # 状态变化 → 通知
    if new_status == "in_stock" and prev_status != "in_stock":
        title = f"🎉 Cloudnium {target_name} 已补货！"
        body = (
            f"{target_product['name']} — ${target_price}/月\n"
            f"时间: {now}\n"
            f"立即购买: {target_url}"
        )
        send_notification(config, title, body)
        result["notified"] = "restock"

    elif new_status == "in_stock" and prev_status == "in_stock":
        if verbose:
            print(f"  [OK] 状态未变化，仍在售")

    elif new_status != prev_status:
        title = f"📊 Cloudnium {target_name} 状态变化"
        body = (
            f"之前: {prev_status}\n"
            f"现在: {new_status}\n"
            f"产品: {target_product['name']}\n"
            f"时间: {now}"
        )
        send_notification(config, title, body)
        result["notified"] = "status_change"

    # 未知状态
    if new_status == "unknown" and prev_status != "unknown":
        title = f"⚠️ Cloudnium {target_name} 状态未知"
        body = (
            f"无法自动判断库存状态，请手动检查。\n"
            f"产品: {target_product['name']}\n"
            f"链接: {target_url}"
        )
        send_notification(config, title, body)
        result["notified"] = "unknown_status"

    # 保存状态
    state["last_status"] = new_status
    state["last_check"] = now
    state["last_product_name"] = target_product["name"]
    save_state(target_name, state)

    return result


# ── 主入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Cloudnium VPS 补货监控",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
示例:
  python cloudnium_monitor.py                                    # 检查 LA-1
  python cloudnium_monitor.py --url URL --name NAME              # 自定义页面
  python cloudnium_monitor.py --daemon 300                       # 每5分钟
  python cloudnium_monitor.py --daemon 300 --name Buffalo --url "https://..."

通知渠道配置: {CONFIG_FILE}
        """
    )
    parser.add_argument("--url", "-u", type=str, default=None,
                        help="目标页面 URL")
    parser.add_argument("--name", "-n", type=str, default=None,
                        help="监控名称（用于通知和状态文件）")
    parser.add_argument("--price", "-p", type=float, default=2.99,
                        help="目标价格（默认 2.99）")
    parser.add_argument("--daemon", "-d", type=int, default=0, metavar="SECONDS",
                        help="后台模式，每隔 SECONDS 秒检查一次（建议 >= 60）")
    parser.add_argument("--dump", action="store_true",
                        help="只抓取页面保存 HTML，不做检查")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="安静模式，减少输出")

    args = parser.parse_args()
    config = load_config()

    # 确定目标
    if args.url:
        target_url = args.url
        target_name = args.name or "VPS"
    else:
        target_url = DEFAULT_TARGETS[0]["url"]
        target_name = DEFAULT_TARGETS[0]["name"]
    target_price = args.price

    if args.dump:
        print(f"抓取页面: {target_url}")
        html = fetch_page(target_url)
        dump_file = SCRIPT_DIR / f"cloudnium_debug_{target_name.replace(' ', '_')}.html"
        with open(dump_file, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"已保存到: {dump_file}")
        print(f"文件大小: {len(html):,} bytes")
        prices = re.findall(r'\$\s*(\d+\.?\d*)', html)
        print(f"页面中的价格: {sorted(set(prices))}")
        return

    if args.daemon > 0:
        interval = max(args.daemon, 30)
        print(f"🔄 后台监控模式启动")
        print(f"   目标: [{target_name}] ${target_price} @ {target_url}")
        print(f"   间隔: {interval} 秒")
        print(f"   按 Ctrl+C 停止\n")

        send_notification(
            config,
            f"Cloudnium {target_name} 监控已启动",
            f"目标: ${target_price}/月\n"
            f"页面: {target_url}\n"
            f"间隔: {interval}秒\n"
            f"补货时立即通知!"
        )

        first_run = True
        while True:
            try:
                check_once(config, verbose=first_run or not args.quiet,
                           target_url=target_url, target_name=target_name,
                           target_price=target_price)
                first_run = False
            except KeyboardInterrupt:
                print("\n👋 监控已停止")
                break
            except Exception as e:
                print(f"[ERROR] 检查异常: {e}")
                traceback.print_exc()

            try:
                for _ in range(interval):
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n👋 监控已停止")
                break
    else:
        result = check_once(config, verbose=not args.quiet,
                            target_url=target_url, target_name=target_name,
                            target_price=target_price)

        if result["status"] == "error":
            sys.exit(2)
        elif result["status"] == "in_stock":
            sys.exit(1)
        else:
            sys.exit(0)


if __name__ == "__main__":
    main()
