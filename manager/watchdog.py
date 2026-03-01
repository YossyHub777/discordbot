"""
もち神さま Bot Manager - Watchdog（死活監視）
Windowsが落ちた場合にRaspi側Botを自動起動する
"""

import json
import logging
import socket
import subprocess
import time
from datetime import datetime

# ============================================================
# 設定
# ============================================================
WINDOWS_HOST = "YOSSYHUB-PC.local"
WINDOWS_CHECK_PORT = 50021
CHECK_INTERVAL = 60  # 秒
TIMEOUT = 3

RASPI_BOT_DIR = "/data/compose/discord-bot"
RASPI_COMPOSE = f"docker compose -f {RASPI_BOT_DIR}/docker-compose.yml"

STATUS_FILE = "/tmp/watchdog_status.json"

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("watchdog")


# ============================================================
# ユーティリティ
# ============================================================
def check_windows_online() -> bool:
    """WindowsのVOICEVOXポートへTCP接続して死活確認"""
    try:
        with socket.create_connection(
            (WINDOWS_HOST, WINDOWS_CHECK_PORT), timeout=TIMEOUT
        ):
            return True
    except (socket.timeout, socket.error, OSError):
        return False


def is_raspi_bot_running() -> bool:
    """Raspi側Botが起動中か確認"""
    try:
        result = subprocess.run(
            f"{RASPI_COMPOSE} ps mochigami",
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout.lower()
        return "running" in output or "up" in output
    except Exception:
        return False


def start_raspi_bot():
    """Raspi側Botを起動"""
    try:
        subprocess.run(
            f"{RASPI_COMPOSE} start mochigami",
            shell=True,
            capture_output=True,
            timeout=60,
        )
        logger.info("Raspi Bot を起動しました ✅")
    except Exception as e:
        logger.error(f"Raspi Bot の起動に失敗: {e}")


def write_status(running: bool, windows_online: bool, next_check_seconds: int):
    """ステータスファイルを書き出し"""
    now = datetime.now()
    data = {
        "running": running,
        "next_check_seconds": next_check_seconds,
        "last_check": now.strftime("%Y-%m-%d %H:%M:%S"),
        "last_check_ts": time.time(),
        "interval": CHECK_INTERVAL,
        "windows_online": windows_online,
    }
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"ステータスファイル書き出しエラー: {e}")


# ============================================================
# メインループ
# ============================================================
def main():
    logger.info("=== Watchdog 起動 ===")
    prev_windows_online = None

    while True:
        windows_online = check_windows_online()

        # 状態変化時のみログ出力
        if prev_windows_online is not None and windows_online != prev_windows_online:
            if windows_online:
                logger.info("Windows: 🟢 復活検知 → 手動切り替えをお待ちください")
            else:
                logger.info("Windows: 🔴 オフライン検知 → Raspi Bot を確認します")
                # Windowsが落ちた場合、Raspi側Botが停止していれば自動起動
                if not is_raspi_bot_running():
                    logger.info("Raspi Bot が停止中 → 自動起動します")
                    start_raspi_bot()
                else:
                    logger.info("Raspi Bot は既に起動中です")
        elif prev_windows_online is None:
            # 初回起動時のログ
            if windows_online:
                logger.info("Windows: 🟢 オンライン")
            else:
                logger.info("Windows: 🔴 オフライン")
                if not is_raspi_bot_running():
                    logger.info("Raspi Bot が停止中 → 自動起動します")
                    start_raspi_bot()

        prev_windows_online = windows_online

        # ステータスファイル書き出し（毎回更新）
        write_status(
            running=True,
            windows_online=windows_online,
            next_check_seconds=CHECK_INTERVAL,
        )

        # 次回チェックまで待機
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
