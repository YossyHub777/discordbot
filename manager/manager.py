"""
もち神さま Bot Manager - Flask Webサーバー
Raspi5 / Windows PC 上のDiscord Botを管理するWebアプリケーション
"""

import json
import os
import socket
import subprocess
import threading
import time

import paramiko
from flask import Flask, jsonify, render_template, request

# ============================================================
# 定数
# ============================================================
RASPI_BOT_DIR = "/data/compose/discord-bot"
RASPI_COMPOSE = f"docker compose -f {RASPI_BOT_DIR}/docker-compose.yml"

WINDOWS_HOST = "YOSSYHUB-PC.local"
WINDOWS_USER = "yossy.hub"
WINDOWS_BOT_DIR = r"C:\Users\yossy.hub\discord-bot"
WINDOWS_COMPOSE = f"docker compose -f {WINDOWS_BOT_DIR}\\docker-compose.yml"
WINDOWS_CHECK_PORT = 50021
WINDOWS_CHECK_TIMEOUT = 3

GITHUB_REPO_URL = "https://github.com/YossyHub777/discordbot.git"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
SSH_PASSWORD = os.environ.get("SSH_PASSWORD", "")

# ============================================================
# Flask アプリ
# ============================================================
app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0


@app.after_request
def add_no_cache(response):
    """キャッシュを無効化"""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response


# SSH接続のロック（同時アクセス防止）
ssh_lock = threading.Lock()


# ============================================================
# ユーティリティ
# ============================================================
def run_local(cmd: str, timeout: int = 30) -> tuple[int, str]:
    """ローカルコマンドを実行し、(returncode, stdout+stderr)を返す"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "コマンドタイムアウト"
    except Exception as e:
        return -1, str(e)


def check_windows_online() -> bool:
    """Windows PCが起動しているか判定（SSHポート22番の死活を確認）"""
    try:
        # Windows PC本体が立ち上がっているか（SSHが通るか）でオフライン判定する
        # VOICEVOXが停止していてもPC本体が起動していれば操作可能とするため22番を使用
        s = socket.create_connection((WINDOWS_HOST, 22), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def ssh_exec(command: str, timeout: int = 30) -> tuple[int, str]:
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
    if len(lines) < 2:  # ヘッダー行のみまたは空
        return False

    # ヘッダー行から列位置を検出
    header = lines[0]
    service_col = header.find("SERVICE")
    # SERVICE列の次の列（CREATED）を終端として使う
    created_col = header.find("CREATED")
    if created_col == -1:
        created_col = header.find("STATUS")

    if service_col == -1 or created_col == -1:
        # ヘッダーが見つからない場合はフォールバック
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
        # SERVICE列の値を抽出して完全一致チェック
        service_value = line[service_col:created_col].strip()
        if service_value == service_name:
            lower = line.lower()
            if "running" in lower or "up" in lower:
                return True
    return False


# ============================================================
# Git情報取得
# ============================================================
def get_git_info_local() -> dict:
    """Raspi側のGit情報を取得（fetchは行わない）"""
    git_cmd = f"git -C {RASPI_BOT_DIR}"

    _, branch = run_local(f"{git_cmd} rev-parse --abbrev-ref HEAD")
    _, commit = run_local(f"{git_cmd} rev-parse --short HEAD")
    _, commit_date = run_local(f'{git_cmd} log -1 --format=%ad --date=format:"%Y-%m-%d %H:%M"')
    commit_date = commit_date.strip('"')

    # fetchなしでローカルに存在するリモート追跡情報から差分を計算
    # （git fetchはPull操作時のみ実行する）
    _, behind_str = run_local(
        f"{git_cmd} rev-list HEAD..origin/{branch} --count 2>/dev/null || echo -1"
    )
    try:
        behind = int(behind_str.strip())
        if behind < 0:
            behind = None  # リモート追跡情報がない場合
    except ValueError:
        behind = None

    return {
        "branch": branch,
        "commit": commit,
        "commit_date": commit_date,
        "behind": behind,
    }


def get_git_info_windows() -> dict:
    """Windows側のGit情報をSSH経由で取得（fetchは行わない）"""
    git_cmd = f"git -C {WINDOWS_BOT_DIR}"

    _, branch = ssh_exec(f"{git_cmd} rev-parse --abbrev-ref HEAD")
    _, commit = ssh_exec(f"{git_cmd} rev-parse --short HEAD")
    _, commit_date = ssh_exec(f'{git_cmd} log -1 --format=%ad --date=format:"%Y-%m-%d %H:%M"')
    commit_date = commit_date.strip('"')

    _, behind_str = ssh_exec(
        f'{git_cmd} rev-list HEAD..origin/{branch} --count 2>nul || echo -1'
    )
    try:
        behind = int(behind_str.strip())
        if behind < 0:
            behind = None
    except ValueError:
        behind = None

    return {
        "branch": branch,
        "commit": commit,
        "commit_date": commit_date,
        "behind": behind,
    }


# ============================================================
# API エンドポイント
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """両サーバーの状態をJSON返却"""
    # --- Raspi側 ---
    _, raspi_ps = run_local(f"{RASPI_COMPOSE} ps")
    raspi_bot = is_container_running(raspi_ps, "mochigami")
    raspi_git = get_git_info_local()

    # --- Windows側 ---
    windows_online = check_windows_online()
    windows_data = {
        "online": windows_online,
        "bot_running": False,
        "branch": "",
        "commit": "",
        "commit_date": "",
        "behind": None,
    }

    if windows_online:
        try:
            _, win_ps = ssh_exec(f"{WINDOWS_COMPOSE} ps")
            windows_data["bot_running"] = is_container_running(win_ps, "mochigami")
            win_git = get_git_info_windows()
            windows_data.update(win_git)
        except Exception:
            pass

    # --- Watchdog ---
    watchdog_data = {"running": False, "next_check_seconds": None}
    try:
        with open("/tmp/watchdog_status.json", "r") as f:
            wd = json.load(f)
            watchdog_data["running"] = wd.get("running", False)
            # 残り秒数を動的に計算
            last_check = wd.get("last_check_ts")
            interval = wd.get("interval", 60)
            if last_check:
                elapsed = time.time() - last_check
                remaining = max(0, int(interval - elapsed))
                watchdog_data["next_check_seconds"] = remaining
            else:
                watchdog_data["next_check_seconds"] = wd.get("next_check_seconds")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return jsonify(
        {
            "raspi": {
                "bot_running": raspi_bot,
                **raspi_git,
            },
            "windows": windows_data,
            "watchdog": watchdog_data,
        }
    )


# --- Raspi操作 ---
@app.route("/api/raspi/start", methods=["POST"])
def raspi_start():
    logs = []

    # 排他制御: Windows側Botが動いていれば先に停止
    if check_windows_online():
        try:
            _, win_ps = ssh_exec(f"{WINDOWS_COMPOSE} ps")
            if is_container_running(win_ps, "mochigami"):
                code, output = ssh_exec(f"{WINDOWS_COMPOSE} stop", timeout=60)
                logs.append(f"[Windows Bot 停止] exit={code}\n{output}")
        except Exception as e:
            logs.append(f"[Windows Bot 停止スキップ] {e}")

    code, output = run_local(f"{RASPI_COMPOSE} up -d", timeout=60)
    logs.append(f"[Raspi Bot 起動] exit={code}\n{output}")
    if code == 0:
        return jsonify({"success": True, "message": "Raspi Bot を起動しました", "output": "\n".join(logs)})
    return jsonify({"success": False, "message": "起動エラー", "output": "\n".join(logs)}), 500


@app.route("/api/raspi/stop", methods=["POST"])
def raspi_stop():
    code, output = run_local(f"{RASPI_COMPOSE} stop", timeout=60)
    if code == 0:
        return jsonify({"success": True, "message": "Raspi Bot を停止しました", "output": output})
    return jsonify({"success": False, "message": f"停止エラー", "output": output}), 500


@app.route("/api/raspi/check", methods=["POST"])
def raspi_check():
    """Raspi側のgit fetchを実行してbehind数を返す"""
    git_cmd = f"git -C {RASPI_BOT_DIR}"
    code, output = run_local(f"{git_cmd} fetch origin", timeout=30)
    if code != 0:
        return jsonify({"success": False, "message": "fetch エラー", "output": output}), 500

    _, branch = run_local(f"{git_cmd} rev-parse --abbrev-ref HEAD")
    _, behind_str = run_local(
        f"{git_cmd} rev-list HEAD..origin/{branch.strip()} --count 2>/dev/null || echo 0"
    )
    try:
        behind = int(behind_str.strip())
    except ValueError:
        behind = 0

    msg = f"{behind}件の更新があります" if behind > 0 else "最新です"
    return jsonify({"success": True, "message": msg, "behind": behind, "output": output})


@app.route("/api/raspi/pull", methods=["POST"])
def raspi_pull():
    git_cmd = f"git -C {RASPI_BOT_DIR}"
    pull_url = f"https://{GITHUB_TOKEN}@github.com/YossyHub777/discordbot.git"
    code, output = run_local(f"{git_cmd} pull {pull_url} main", timeout=120)
    if code == 0:
        return jsonify({"success": True, "message": "Pull が完了しました", "output": output})
    return jsonify({"success": False, "message": "Pull エラー", "output": output}), 500


@app.route("/api/raspi/restart", methods=["POST"])
def raspi_restart():
    code, output = run_local(f"{RASPI_COMPOSE} restart", timeout=120)
    if code == 0:
        return jsonify({"success": True, "message": "Raspi Bot を再起動しました", "output": output})
    return jsonify({"success": False, "message": "再起動エラー", "output": output}), 500


# --- Windows操作 ---
@app.route("/api/windows/start", methods=["POST"])
def windows_start():
    if not check_windows_online():
        return jsonify({"success": False, "message": "Windows PCがオフラインです", "output": ""}), 503

    logs = []

    # 排他制御: Raspi側Botが動いていれば先に停止
    _, raspi_ps = run_local(f"{RASPI_COMPOSE} ps")
    if is_container_running(raspi_ps, "mochigami"):
        code, output = run_local(f"{RASPI_COMPOSE} stop", timeout=60)
        logs.append(f"[Raspi Bot 停止] exit={code}\n{output}")

    code, output = ssh_exec(f"{WINDOWS_COMPOSE} up -d", timeout=60)
    logs.append(f"[Windows Bot 起動] exit={code}\n{output}")
    if code == 0:
        return jsonify({"success": True, "message": "Windows Bot を起動しました", "output": "\n".join(logs)})
    return jsonify({"success": False, "message": "起動エラー", "output": "\n".join(logs)}), 500


@app.route("/api/windows/stop", methods=["POST"])
def windows_stop():
    if not check_windows_online():
        return jsonify({"success": False, "message": "Windows PCがオフラインです", "output": ""}), 503

    code, output = ssh_exec(f"{WINDOWS_COMPOSE} stop", timeout=60)
    if code == 0:
        return jsonify({"success": True, "message": "Windows Bot を停止しました", "output": output})
    return jsonify({"success": False, "message": "停止エラー", "output": output}), 500


@app.route("/api/windows/check", methods=["POST"])
def windows_check():
    """Windows側のgit fetchを実行してbehind数を返す"""
    if not check_windows_online():
        return jsonify({"success": False, "message": "Windows PCがオフラインです", "output": ""}), 503

    git_cmd = f"git -C {WINDOWS_BOT_DIR}"
    code, output = ssh_exec(f"{git_cmd} fetch origin", timeout=30)
    if code != 0:
        return jsonify({"success": False, "message": "fetch エラー", "output": output}), 500

    _, branch = ssh_exec(f"{git_cmd} rev-parse --abbrev-ref HEAD")
    _, behind_str = ssh_exec(
        f'{git_cmd} rev-list HEAD..origin/{branch.strip()} --count 2>nul || echo 0'
    )
    try:
        behind = int(behind_str.strip())
    except ValueError:
        behind = 0

    msg = f"{behind}件の更新があります" if behind > 0 else "最新です"
    return jsonify({"success": True, "message": msg, "behind": behind, "output": output})


@app.route("/api/windows/pull", methods=["POST"])
def windows_pull():
    if not check_windows_online():
        return jsonify({"success": False, "message": "Windows PCがオフラインです", "output": ""}), 503

    git_cmd = f"git -C {WINDOWS_BOT_DIR}"
    pull_url = f"https://{GITHUB_TOKEN}@github.com/YossyHub777/discordbot.git"
    code, output = ssh_exec(f"{git_cmd} pull {pull_url} main", timeout=120)
    if code == 0:
        return jsonify({"success": True, "message": "Pull が完了しました", "output": output})
    return jsonify({"success": False, "message": "Pull エラー", "output": output}), 500


@app.route("/api/windows/restart", methods=["POST"])
def windows_restart():
    if not check_windows_online():
        return jsonify({"success": False, "message": "Windows PCがオフラインです", "output": ""}), 503

    code, output = ssh_exec(f"{WINDOWS_COMPOSE} restart", timeout=120)
    if code == 0:
        return jsonify({"success": True, "message": "Windows Bot を再起動しました", "output": output})
    return jsonify({"success": False, "message": "再起動エラー", "output": output}), 500


# ============================================================
# エントリーポイント
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=31173, debug=False)

