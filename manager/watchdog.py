"""
もち神さま Bot Manager - Watchdog（死活監視）
Windowsが落ちた場合にRaspi側Botを自動起動する
"""

import json
import logging
import os
import socket
import subprocess
import time
import threading
from datetime import datetime

import paramiko

# ============================================================
# 設定
# ============================================================
WINDOWS_HOST = "YOSSYHUB-PC.local"
WINDOWS_USER = "yossy.hub"
WINDOWS_BOT_DIR = "C:\\yossyhub\\discord-bot"
WINDOWS_COMPOSE = f"docker compose -f {WINDOWS_BOT_DIR}\\docker-compose.yml"
WINDOWS_CHECK_PORT = 50021
CHECK_INTERVAL = 600  # 10分
TIMEOUT = 3

SSH_PASSWORD = os.environ.get("SSH_PASSWORD", "")
ssh_lock = threading.Lock()

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


def ssh_exec(command: str, timeout: int = 10) -> tuple[int, str]:
    """Windows PCにSSH接続してコマンドを実行"""
    with ssh_lock:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                WINDOWS_HOST,
                username=WINDOWS_USER,
                password=SSH_PASSWORD,
                timeout=10,
            )
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            output = (stdout.read().decode() + stderr.read().decode()).strip()
            return exit_code, output
        except Exception as e:
            return -1, f"SSH接続エラー: {e}"
        finally:
            client.close()

def is_container_running(compose_output: str, service_name: str) -> bool:
    """docker compose ps の出力から指定サービスが動作中か判定"""
    lines = compose_output.splitlines()
    if len(lines) < 2:
        return False

    header = lines[0]
    service_col = header.find("SERVICE")
    created_col = header.find("CREATED")
    if created_col == -1:
        created_col = header.find("STATUS")

    if service_col == -1 or created_col == -1:
        for line in lines[1:]:
            parts = line.split()
            if service_name in parts:
                lower = line.lower()
                if "running" in lower or "up" in lower:
                    return True
        return False

    for line in lines[1:]:
        if len(line) < service_col:
            continue
        service_value = line[service_col:created_col].strip()
        if service_value == service_name:
            lower = line.lower()
            if "running" in lower or "up" in lower:
                return True
    return False

def check_windows_online() -> bool:
    """Windows側のmochigamiコンテナが起動しているか確認"""
    try:
        # PC自体がオフラインかどうかはSSH接続の成否で判定
        exit_code, output = ssh_exec(f"{WINDOWS_COMPOSE} ps")
        if exit_code != 0:
            return False
            
        return is_container_running(output, "mochigami")
    except Exception:
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
