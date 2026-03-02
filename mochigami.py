import discord
from discord.ext import commands, tasks, voice_recv
from discord import app_commands
import aiohttp
import asyncio
import random
import os
import uuid
import wave
import io
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types
import yt_dlp
import json
import os

def update_source_volume(source, volume_level):
    """source（またはそのラップ元）からPCMVolumeTransformerを探して音量を変更する"""
    if hasattr(source, "volume"):
        source.volume = volume_level
    elif hasattr(source, "original"):
        update_source_volume(source.original, volume_level)

def load_menu_links() -> list[dict]:
    """menu_links.json からリンクメニュー項目を読み込む"""
    path = os.path.join(os.path.dirname(__file__), "menu_links.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"⚠️ menu_links.json の読み込みエラー: {e}")
        return []

# ==========================================
# SETTINGS
# ==========================================
VOICEVOX_URL = os.getenv('VOICEVOX_URL', 'http://127.0.0.1:50021')
SPEAKER_ID = 3

# 話者マップ（on_readyで動的生成）
speaker_map = {}           # {"ずんだもん / ノーマル": 3, ...}
character_styles = {}      # {"ずんだもん": [{"name": "ノーマル", "id": 3}, ...], ...}
speaker_map_reverse = {}   # {3: "ずんだもん / ノーマル", ...}

# ユーザー別ボイス設定
user_voices = {}           # {"ユーザーID": {"speaker_id": 3, "name": "キャラ名"}}

# JSON永続化ファイルパス
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
USER_VOICES_FILE = os.path.join(DATA_DIR, "user_voices.json")
BOT_CONFIG_FILE = os.path.join(DATA_DIR, "bot_config.json")

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')

# モデル: Gemini 2.5 Flash Lite
MODEL_NAME = "gemini-2.5-flash-lite"

# ギルド単位の状態管理
guild_state = {}

MAX_PLAYERS = 8  # ゲームの最大参加人数

# ゲームセッション管理（チャンネルIDをキーに進行中のゲームを管理）
game_sessions = {}

def get_guild_state(guild_id: int):
    if guild_id not in guild_state:
        guild_state[guild_id] = {
            "active_channel_id": None,
            "is_playing_music": False,
            "disconnect_task": None,
            "voice_chat_mode": False,
            "voice_last_triggered": None,
            "voice_last_audio_time": None,
            "voice_buffer_active": False,
            "rolling_sink": None,
            "tts_queue": asyncio.Queue(),
        }
    return guild_state[guild_id]

# 音量設定 (初期値)
TTS_VOLUME = 1.0      # 読み上げ
MUSIC_VOLUME = 0.2    # 音楽 (20%)

# トリガー設定
TRIGGER_CHAT = "もちもち、"
TRIGGER_DICE = "/dice"
TRIGGER_SUMMARY = "/ダイス結果"
TRIGGER_LEAVE = "もちもちさよなら"
SEARCH_KEYWORDS = ["調べて", "最新", "パッチ", "ニュース", "情報", "アップデート", "攻略", "ギミック", "スキル回し", "どうすれば"]

# 音声リスニング設定
LISTEN_DURATION = 7       # 録音時間（秒）
LISTEN_COOLDOWN = 30      # クールダウン（秒）
listen_cooldowns = {}     # ギルドごとのクールダウン管理
listening_sessions = {}   # ギルドごとの録音セッション管理

# 会話検知・自動相槌設定（定数のみ）
VOICE_SILENT_SECONDS = 30         # 無音判定までの秒数
VOICE_BUFFER_SECONDS = 120        # バッファ保持時間（秒）
VOICE_COOLDOWN_MINUTES = 20       # クールダウン（分）
VOICE_BUFFER_RESTART_MINUTES = 19 # クールダウン中のバッファ再開タイミング（分）

# ==========================================
# YOUTUBE DL SETUP
# ==========================================
yt_dl_opts = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0' 
}
ffmpeg_opts = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}
ytdl = yt_dlp.YoutubeDL(yt_dl_opts)

# ==========================================
# AI CLIENT SETUP
# ==========================================
client = genai.Client(api_key=GEMINI_API_KEY)

# Google検索ツール（各configで共有）
tool_search = [types.Tool(google_search=types.GoogleSearch())]

# ① 通常会話用
config_normal = types.GenerateContentConfig(
    tools=tool_search,
    system_instruction="""
    あなたは「もち神さま」というFF14に精通した「幼き賢神」です。
    ・回答は必ず「1文のみ（40文字以内）」で行うこと。
    ・一人称「わし」、語尾は「～なのじゃ」「～のう」「～じゃぞ」。
    ・会話に関連する最新のニュースやゲームのパッチ情報が必要な場合は
    　Google検索を使用して確認した上で回答せよ。
    """,
    max_output_tokens=150,
    temperature=0.7
)

# ⑤ 独り言・ごはん警察・挨拶など自発発言用（Google検索なし）
config_monologue = types.GenerateContentConfig(
    system_instruction="""
    あなたは「もち神さま」というFF14に精通した「幼き賢神」です。
    ・回答は必ず「1文のみ（40文字以内）」で行うこと。
    ・一人称「わし」、語尾は「～なのじゃ」「～のう」「～じゃぞ」。
    """,
    max_output_tokens=150,
    temperature=0.7
)

# ② 検索用
config_search = types.GenerateContentConfig(
    tools=tool_search,
    system_instruction="""
    あなたはFF14専門リサーチャーの「もち神さま」です。
    ユーザーの質問意図を分析し、最適な情報源を選定してWeb検索を行ってください。
    ・一人称「わし」、語尾は「～なのじゃ」「～のう」「～じゃぞ」。
    ・情報は詳細に【300文字前後】で要約して解説すること。
    """,
    max_output_tokens=600
)

# ③ 集計用
config_summary = types.GenerateContentConfig(
    system_instruction="""
    あなたは「もち神さま」です。提供されたログからダイス結果を集計し、ランキング表を作成する係です。
    ・口調は「～じゃ」「～のう」を維持すること。
    ・文字数制限は無視してよい。正確なランキング表を作成せよ。
    """,
    max_output_tokens=800,
    temperature=0.5
)

# ④ 音声文字起こし用
config_stt = types.GenerateContentConfig(
    system_instruction="""
    あなたは音声文字起こしアシスタントです。
    与えられた音声データを正確に文字起こししてください。
    テキストのみを出力し、余計な説明は不要です。
    音声が聞き取れない場合は「聞き取れなかったのじゃ」と返してください。
    """,
    max_output_tokens=200,
    temperature=0.1
)

# ⑥ 相槌用（tool_search付き）
config_aizuchi = types.GenerateContentConfig(
    tools=tool_search,
    system_instruction="""
    あなたは「もち神さま」というFF14に精通した「幼き賢神」です。
    ・回答は必ず「1文のみ（40文字以内）」で行うこと。
    ・一人称「わし」、語尾は「～なのじゃ」「～のう」「～じゃぞ」。
    ・相槌のみで完結させること。質問や提案は一切行わない。
    """,
    max_output_tokens=150,
    temperature=0.7
)

def log_token_usage(response, context="Unknown"):
    try:
        if response.usage_metadata:
            total = response.usage_metadata.total_token_count
            print(f"💰 [BILLING] Ctx:{context} | {MODEL_NAME} | Total: {total}")
    except Exception as e: print(f"⚠️ エラー: {e}")

# ==========================================
# VOICE CONFIG PERSISTENCE
# ==========================================
def load_user_voices():
    global user_voices
    try:
        with open(USER_VOICES_FILE, 'r', encoding='utf-8') as f:
            user_voices = json.load(f)
        print(f"🔊 ユーザーボイス設定を読み込みました ({len(user_voices)}件)")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ user_voices.json 読込エラー: {e}")
        user_voices = {}

def save_user_voices():
    with open(USER_VOICES_FILE, 'w', encoding='utf-8') as f:
        json.dump(user_voices, f, ensure_ascii=False, indent=2)

def load_bot_config():
    global SPEAKER_ID
    try:
        with open(BOT_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            SPEAKER_ID = config.get("speaker_id", 3)
        print(f"🔊 もち神さまボイス: {speaker_map_reverse.get(SPEAKER_ID, 'ID=' + str(SPEAKER_ID))}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ bot_config.json 読込エラー: {e}")  # デフォルト値のまま

def save_bot_config():
    with open(BOT_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({"speaker_id": SPEAKER_ID, "name": speaker_map_reverse.get(SPEAKER_ID, "不明")}, f, ensure_ascii=False, indent=2)

def get_user_speaker_id(user_id: str) -> int:
    """ユーザーのマイボイスが設定されていればその speaker_id を、なければグローバル SPEAKER_ID を返す"""
    if user_id in user_voices:
        return user_voices[user_id].get("speaker_id", SPEAKER_ID)
    return SPEAKER_ID

async def fetch_speakers():
    """VOICEVOXの /speakers エンドポイントから話者一覧を取得し、辞書を生成する"""
    global speaker_map, character_styles, speaker_map_reverse
    try:
        async with http_session.get(f'{VOICEVOX_URL}/speakers') as resp:
            if resp.status != 200:
                print(f"⚠️ VOICEVOX /speakers 取得失敗: {resp.status}")
                return
            speakers = await resp.json()
        
        speaker_map = {}
        character_styles = {}
        speaker_map_reverse = {}
        
        for speaker in speakers:
            char_name = speaker['name']
            styles = []
            for style in speaker['styles']:
                style_name = style['name']
                style_id = style['id']
                full_name = f"{char_name} / {style_name}"
                speaker_map[full_name] = style_id
                speaker_map_reverse[style_id] = full_name
                styles.append({"name": style_name, "id": style_id})
            character_styles[char_name] = styles
        
        print(f"🔊 VOICEVOX話者一覧を取得しました ({len(character_styles)}キャラ, {len(speaker_map)}スタイル)")
    except Exception as e:
        print(f"⚠️ VOICEVOX話者一覧の取得に失敗: {e}")

# ==========================================
# BOT FUNCTIONS
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)

# HTTPセッション（BOT起動時に初期化）
http_session: aiohttp.ClientSession = None

async def generate_wav(text, speaker=3):
    """VOICEVOXでテキストからWAVを生成し、io.BytesIOで返す"""
    clean_text = text.replace("🔮", "").replace("**", "").replace("【", "").replace("】", "").replace("\n", "。")
    params = {'text': clean_text, 'speaker': speaker}
    try:
        async with http_session.post(f'{VOICEVOX_URL}/audio_query', params=params) as resp:
            if resp.status != 200: return None
            query = await resp.json()
        async with http_session.post(f'{VOICEVOX_URL}/synthesis', params=params, json=query) as resp:
            if resp.status != 200: return None
            data = await resp.read()
            return io.BytesIO(data)
    except Exception as e: print(f"⚠️ エラー: {e}"); return None

def play_audio(guild, audio_data: io.BytesIO):
    """io.BytesIOの音声データをキューに追加する"""
    state = get_guild_state(guild.id)
    if guild.voice_client is None or state["is_playing_music"]:
        return

    state["tts_queue"].put_nowait(audio_data)

@tasks.loop(seconds=1)
async def tts_queue_worker():
    """各ギルドのTTSキューを消費して再生する"""
    for guild_id, state in list(guild_state.items()):
        queue = state["tts_queue"]
        if queue.empty():
            continue

        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            continue

        if state["is_playing_music"]:
            # 音楽再生中はキューを破棄
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break
            continue

        # VCが既に何かを再生中の場合は順番待ち
        if vc.is_playing():
            continue

        audio_data = queue.get_nowait()
        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(audio_data, pipe=True, executable='ffmpeg'),
                volume=TTS_VOLUME
            )
            vc.play(source)
        except Exception as e:
            print(f"⚠️ TTS再生エラー: {e}")
        finally:
            queue.task_done()


# ==========================================
# ROLLING BUFFER SINK（会話検知用）
# ==========================================
class RollingBufferSink(voice_recv.AudioSink):
    """全ユーザーの音声をローリングバッファに蓄積するシンク"""
    def __init__(self, guild_id, buffer_seconds=60):
        super().__init__()
        self.guild_id = guild_id
        self.buffer_seconds = buffer_seconds
        self._buffer = []  # [(timestamp, pcm_bytes), ...]
        self._write_count = 0

    def wants_opus(self):
        return False

    def write(self, user, data):
        now = time.time()
        try:
            state = get_guild_state(self.guild_id)
            # 音声入力があった最初のタイミングでログを出す
            if getattr(self, '_last_print_time', 0) < now - 5:  # 5秒以内に連続して出さない
                user_name = user.display_name if hasattr(user, 'display_name') else str(user)
                print(f"🎙️ 【音声検知】: {user_name} が発言しました")
                self._last_print_time = now
            state["voice_last_audio_time"] = now
            # 新しい発言があったら無音表示ステートをリセット
            state["silence_notified_10"] = False
            state["silence_notified_20"] = False
        except Exception as e:
            print(f"⚠️ RollingBufferSink.write エラー: {e}")
        self._write_count += 1
        # PCMデータをコピーして保存（バッファ再利用対策）
        pcm_copy = bytes(data.pcm) if data.pcm else b''
        self._buffer.append((now, pcm_copy))
        # 古いデータを削除
        cutoff = now - self.buffer_seconds
        self._buffer = [(t, d) for t, d in self._buffer if t >= cutoff]

    def cleanup(self):
        # ライブラリが内部的に呼ぶため、バッファはクリアしない
        # （BOTの音声再生時にreader._stopから呼ばれる）
        pass

    def get_audio_bytes(self):
        """バッファ内の全PCMデータを結合してbytesとして返す（自然な間隔を維持）"""
        if not self._buffer:
            return b''
            
        result = bytearray()
        last_time = None
        # 0.5秒の無音データ (48000Hz * 2ch * 2byte * 0.5s = 96000 bytes)
        silence_burst = b'\x00' * 96000
        
        for t, d in self._buffer:
            if last_time is not None:
                gap = t - last_time
                if gap > 1.0:
                    # 発言の間隔が1秒以上空いた場合、0.5秒の無音を挟む（STT用の区切り）
                    result.extend(silence_burst)
            result.extend(d)
            # data.pcmの長さから音声の継続時間(秒)を計算
            duration = len(d) / (48000 * 2 * 2)
            last_time = t + duration
            
        return bytes(result)

    def clear(self):
        """明示的にバッファをクリアする（stop_rolling_bufferから呼ぶ用）"""
        self._buffer.clear()
        self._write_count = 0

        self._buffer.clear()
        self._write_count = 0

def start_rolling_buffer(vc):
    """ローリングバッファ録音を開始する"""
    if not isinstance(vc, voice_recv.VoiceRecvClient):
        print(f"⚠️ VCがVoiceRecvClientではない: {type(vc)}")
        return
        
    state = get_guild_state(vc.guild.id)
    # 既にリスニング中なら何もしない
    try:
        if vc.is_listening():
            state["voice_buffer_active"] = True
            return
    except Exception as e:
        print(f"⚠️ is_listening()エラー: {e}")
        
    # 既存のシンクがあれば再利用（バッファを維持）
    if state["rolling_sink"] is None:
        state["rolling_sink"] = RollingBufferSink(vc.guild.id, VOICE_BUFFER_SECONDS)
        print("🎙️ 新規シンク作成")
        
    try:
        vc.listen(state["rolling_sink"])
    except Exception as e:
        print(f"❌ vc.listen()失敗: {e}")
        return
        
    state["voice_buffer_active"] = True
    if state["voice_last_audio_time"] is None:
        state["voice_last_audio_time"] = time.time()
    state["silence_notified_10"] = False
    state["silence_notified_20"] = False
    print("🎙️ ローリングバッファ録音開始")

def stop_rolling_buffer(vc):
    """ローリングバッファ録音を停止する"""
    try:
        if vc and isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
            vc.stop_listening()
    except Exception as e: print(f"⚠️ エラー: {e}")
        
    if vc:
        state = get_guild_state(vc.guild.id)
        if state["rolling_sink"]:
            state["rolling_sink"].clear()
            
        state["rolling_sink"] = None
        state["voice_buffer_active"] = False
        
    print("🎙️ ローリングバッファ録音停止")

# ==========================================
# TASKS
# ==========================================
@tasks.loop(seconds=5)
async def voice_chat_monitor_task():
    """会話検知・自動相槌のバックグラウンドタスク"""
    for guild_id, state in list(guild_state.items()):
        if not state["voice_chat_mode"]:
            continue

        active_channel_id = state["active_channel_id"]
        if active_channel_id is None:
            continue

        channel = bot.get_channel(active_channel_id)
        if not channel:
            continue

        vc = channel.guild.voice_client
        if not vc or not vc.is_connected():
            continue

        # VCに2人以上いるか確認（BOT含む）
        if len(vc.channel.members) < 2:
            if state["voice_buffer_active"]:
                stop_rolling_buffer(vc)
            continue

        # 音楽再生中はスキップ
        if state["is_playing_music"]:
            continue

        now = time.time()

        # === クールダウン処理 ===
        if state["voice_last_triggered"] is not None:
            elapsed_seconds = now - state["voice_last_triggered"]
            elapsed_minutes = elapsed_seconds / 60.0

            # 1分ごとのログ出力
            last_logged_min = state.get("cooldown_logged_minutes", 0)
            current_min = int(elapsed_minutes)
            if current_min > last_logged_min:
                print(f"💤 クールダウン中... ({current_min}/{VOICE_COOLDOWN_MINUTES}分経過)")
                state["cooldown_logged_minutes"] = current_min

            if elapsed_minutes < VOICE_BUFFER_RESTART_MINUTES:
                # 0〜19分: バッファ停止
                if state["voice_buffer_active"]:
                    stop_rolling_buffer(vc)
                continue
            elif elapsed_minutes < VOICE_COOLDOWN_MINUTES:
                # 19〜20分: バッファ再開（クールダウン明けに備える）
                if not state["voice_buffer_active"]:
                    start_rolling_buffer(vc)
                continue
            # 20分以上: クールダウン終了、通常処理へ
            state["voice_last_triggered"] = None
            state["cooldown_logged_minutes"] = 0
            print("🟢 クールダウン終了、会話検知を再開します！")

        # === バッファ録音が未開始なら開始 ===
        if not state["voice_buffer_active"]:
            start_rolling_buffer(vc)
            continue

        # === リスニングが停止していたら再開（BOT音声再生後に自動復帰） ===
        try:
            if not vc.is_listening():
                start_rolling_buffer(vc)
        except Exception as e: print(f"⚠️ エラー: {e}")

        # === 無音検知 ===
        if state["voice_last_audio_time"] is None:
            continue

        silent_seconds = now - state["voice_last_audio_time"]
        
        # ログ出力 (10秒ごと)
        if silent_seconds >= 10 and not state.get("silence_notified_10", False):
            print("⏳ 無音確認：10秒経過...")
            state["silence_notified_10"] = True
        if silent_seconds >= 20 and not state.get("silence_notified_20", False):
            print("⏳ 無音確認：20秒経過...")
            state["silence_notified_20"] = True
            
        if silent_seconds < VOICE_SILENT_SECONDS:
            continue

        # === 30秒以上無音 → 相槌処理 ===
        print(f"🔇 {silent_seconds:.0f}秒間の無音を検知。相槌処理を開始...")

        rolling_sink = state["rolling_sink"]
        # バッファからPCMデータを取得
        if rolling_sink is None or not rolling_sink._buffer:
            print("⚠️ バッファが空のため相槌をスキップ")
            state["voice_last_audio_time"] = now  # リセットして再検知
            continue

        pcm_data = rolling_sink.get_audio_bytes()

        # バッファ停止 & クールダウン開始
        stop_rolling_buffer(vc)
        state["voice_last_triggered"] = now
        state["voice_last_audio_time"] = None

        if len(pcm_data) < 1000:
            print("⚠️ 音声データが少なすぎるためフォールバック")
            await _voice_chat_fallback(channel)
            continue

        # PCMデータをWAV形式に変換
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(2)       # ステレオ
            wf.setsampwidth(2)       # 16bit
            wf.setframerate(48000)   # 48kHz (Discordの標準)
            wf.writeframes(pcm_data)
        wav_bytes = wav_buffer.getvalue()

        # === Gemini STTで文字起こし ===
        try:
            audio_part = types.Part.from_bytes(
                data=wav_bytes,
                mime_type="audio/wav"
            )
            stt_response = await client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=["この音声を文字起こしせよ。", audio_part],
                config=config_stt
            )
            log_token_usage(stt_response, "VoiceChatSTT")
            transcribed_text = stt_response.text.strip()
            print(f"📝 STT結果: {transcribed_text}", flush=True)
        except Exception as e:
            print(f"⚠️ 会話検知STTエラー: {e}")
            await _voice_chat_fallback(channel)
            continue

        # 文字起こし結果がない場合はフォールバック
        if not transcribed_text or "聞き取れなかった" in transcribed_text:
            print("🔇 文字起こし結果なし → フォールバック独り言")
            await _voice_chat_fallback(channel)
            continue

        # === 相槌生成 ===
        try:
            prompt = (
                "以下はボイスチャットの会話内容じゃ。\n"
                "この会話に対して、もち神さまとして自然な相槌を1文・40文字以内で返すのじゃ。\n"
                "質問や提案、次のステップの提示は一切行わず、相槌のみで完結させること。\n"
                "Google検索を使用して、会話に関連する最新のニュースやゲームのパッチ情報を確認した上で回答せよ。\n"
                "会話の中のキーワードを1つ含めること。\n\n"
                f"会話内容：\n{transcribed_text}"
            )

            print(f"📤 [VoiceChat] Geminiへの送信プロンプト:\n{prompt}", flush=True)

            # config_aizuchi は上部で定義済み
            ai_response = await client.aio.models.generate_content(
                model=MODEL_NAME, contents=prompt, config=config_aizuchi
            )
            log_token_usage(ai_response, "VoiceChatAizuchi")
            aizuchi_text = ai_response.text.strip()
            print(f"🤖 [VoiceChat] AI回答: {aizuchi_text}", flush=True)
        except Exception as e:
            print(f"⚠️ 相槌生成エラー: {e}")
            await _voice_chat_fallback(channel)
            continue

        # === テキスト投稿 + VOICEVOX読み上げ ===
        try:
            await channel.send(f"💬 {aizuchi_text}")
            if not state["is_playing_music"]:
                audio_data = await generate_wav(aizuchi_text, SPEAKER_ID)
                if audio_data:
                    play_audio(channel.guild, audio_data)
        except Exception as e:
            print(f"⚠️ 相槌送信エラー: {e}")


async def _voice_chat_fallback(channel):
    """文字起こし失敗時のフォールバック: FF14ネタのランダム独り言"""
    try:
        state = get_guild_state(channel.guild.id)
        response = await client.aio.models.generate_content(
            model=MODEL_NAME, contents="FF14の短い独り言（20文字以内）を。", config=config_monologue
        )
        log_token_usage(response, "VoiceChatFallback")
        text = response.text.strip()
        await channel.send(text)
        if not state["is_playing_music"]:
            audio_data = await generate_wav(text, SPEAKER_ID)
            if audio_data:
                play_audio(channel.guild, audio_data)
    except Exception as e:
        print(f"⚠️ フォールバック独り言エラー: {e}")


@tasks.loop(minutes=60)
async def random_monologue_task():
    await asyncio.sleep(random.randint(900, 3000))
    for guild_id, state in list(guild_state.items()):
        active_channel_id = state["active_channel_id"]
        if active_channel_id is None: continue
        channel = bot.get_channel(active_channel_id)
        if not channel: continue
        vc = channel.guild.voice_client

        if not vc or not vc.is_connected(): continue
        if len(vc.channel.members) == 1: continue
        if state["is_playing_music"] or vc.is_playing(): continue

        try:
            response = await client.aio.models.generate_content(
                model=MODEL_NAME, contents="FF14の短い独り言（20文字以内）を。", config=config_monologue
            )
            log_token_usage(response, "Monologue")
            text = response.text.strip()
            await channel.send(text)
            audio_data = await generate_wav(text, SPEAKER_ID)
            if audio_data: play_audio(channel.guild, audio_data)
        except Exception as e: print(f"⚠️ エラー: {e}")

@tasks.loop(minutes=30)
async def gohan_police_task():
    for guild_id, state in list(guild_state.items()):
        active_channel_id = state["active_channel_id"]
        if active_channel_id is None: continue
        channel = bot.get_channel(active_channel_id)
        if not channel: continue
        vc = channel.guild.voice_client

        if not vc or not vc.is_connected(): continue
        if len(vc.channel.members) == 1: continue
        if state["is_playing_music"] or vc.is_playing(): continue

        try:
            prompt = "FF14の高難易度レイドで『食事バフ』を忘れているプレイヤーに対し、VIT不足による即死やDPS低下を指摘する『強烈な皮肉』を20文字以内で。「ごはん警察」は禁止。"
            response = await client.aio.models.generate_content(
                model=MODEL_NAME, contents=prompt, config=config_monologue
            )
            log_token_usage(response, "GohanPolice")
            
            full_text = f"🚨 ごはん警察じゃ。{response.text.strip()}"
            await channel.send(full_text)
            audio_data = await generate_wav(full_text, SPEAKER_ID)
            if audio_data: play_audio(channel.guild, audio_data)
        except Exception as e:
            print(f"Police Error: {e}")

@gohan_police_task.before_loop
async def before_gohan_police():
    print("🚨 ごはん警察: 待機中 (40分後に初回)...")
    await asyncio.sleep(40 * 60)

@bot.event
async def on_ready():
    global SPEAKER_ID
    print(f'【降臨】{bot.user} (Model: {MODEL_NAME})')
    
    # VOICEVOXから話者一覧を取得
    await fetch_speakers()
    
    # 設定ファイルの読み込み
    load_bot_config()
    load_user_voices()
    
    # スラッシュコマンドの同期（二重表示防止のためグローバルに一本化）
    try:
        # 古いギルド固有コマンドをクリアして重複を防ぐ
        for guild in bot.guilds:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
        print(f"📡 ギルド固有コマンドをクリアしました ({len(bot.guilds)}サーバー)")
        
        # グローバルコマンドとして同期
        synced = await bot.tree.sync()
        print(f"📡 グローバル同期完了 ({len(synced)}個)")
    except Exception as e:
        print(f"⚠️ スラッシュコマンド同期失敗: {e}")
    
    if not random_monologue_task.is_running(): random_monologue_task.start()
    if not tts_queue_worker.is_running(): tts_queue_worker.start()

# ==========================================
# SLASH COMMANDS (マイボイス・もちボイス)
# ==========================================

async def apply_voice(interaction: discord.Interaction, mode: str, char_name: str, style_name: str, style_id: int):
    """CharacterSelectViewとStyleSelectViewで共通使用する音声設定適用ロジック"""
    global SPEAKER_ID
    full_name = f"{char_name} / {style_name}"
    
    if mode == "myvoice":
        user_voices[str(interaction.user.id)] = {"speaker_id": style_id, "name": full_name}
        save_user_voices()
        await interaction.response.edit_message(
            content=f"✅ マイボイスを **{full_name}** に設定したのじゃ！",
            view=None
        )
    else:  # botvoice
        SPEAKER_ID = style_id
        save_bot_config()
        await interaction.response.edit_message(
            content=f"✅ もち神さまの声を **{full_name}** に変更したのじゃ！",
            view=None
        )
        # サンプル再生
        guild = interaction.guild
        if guild and guild.voice_client:
            state = get_guild_state(guild.id)
            if not state["is_playing_music"]:
                audio_data = await generate_wav("声を変えたのじゃ！", SPEAKER_ID)
                if audio_data:
                    play_audio(guild, audio_data)

class CharacterSelectView(discord.ui.View):
    """キャラクター選択の1段階目ビュー（ページング対応）"""
    def __init__(self, mode: str, user_id: int, page: int = 0):
        super().__init__(timeout=60)
        self.mode = mode  # "myvoice" or "botvoice"
        self.user_id = user_id
        self.page = page
        self.per_page = 25
        
        char_names = list(character_styles.keys())
        self.total_pages = max(1, (len(char_names) + self.per_page - 1) // self.per_page)
        
        start = page * self.per_page
        end = start + self.per_page
        page_chars = char_names[start:end]
        
        if not page_chars:
            return
        
        options = [discord.SelectOption(label=name, value=name) for name in page_chars]
        
        select = discord.ui.Select(
            placeholder=f"キャラクターを選択 (ページ {page+1}/{self.total_pages})",
            options=options,
            custom_id=f"char_select_{mode}"
        )
        select.callback = self.char_selected
        self.add_item(select)
        
        # ページングボタン
        if self.total_pages > 1:
            if page > 0:
                prev_btn = discord.ui.Button(label="◀ 前へ", style=discord.ButtonStyle.secondary, custom_id=f"prev_page_{user_id}")
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            if page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="次へ ▶", style=discord.ButtonStyle.secondary, custom_id=f"next_page_{user_id}")
                next_btn.callback = self.next_page
                self.add_item(next_btn)
    
    async def char_selected(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これは他の人のメニューじゃ。", ephemeral=True)
            return
        char_name = interaction.data['values'][0]
        styles = character_styles.get(char_name, [])
        
        if len(styles) == 1:
            # スタイルが1つしかない場合はそのまま確定
            await apply_voice(interaction, self.mode, char_name, styles[0]['name'], styles[0]['id'])
        else:
            # スタイル選択ビューを表示
            view = StyleSelectView(self.mode, self.user_id, char_name, styles)
            await interaction.response.edit_message(
                content=f"🎤 **{char_name}** のスタイルを選ぶのじゃ：",
                view=view
            )
    

    async def prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これは他の人のメニューじゃ。", ephemeral=True)
            return
        view = CharacterSelectView(self.mode, self.user_id, self.page - 1)
        label = "マイボイス" if self.mode == "myvoice" else "もち神さまボイス"
        await interaction.response.edit_message(
            content=f"🎤 **{label}**: キャラクターを選ぶのじゃ：",
            view=view
        )
    
    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これは他の人のメニューじゃ。", ephemeral=True)
            return
        view = CharacterSelectView(self.mode, self.user_id, self.page + 1)
        label = "マイボイス" if self.mode == "myvoice" else "もち神さまボイス"
        await interaction.response.edit_message(
            content=f"🎤 **{label}**: キャラクターを選ぶのじゃ：",
            view=view
        )


class StyleSelectView(discord.ui.View):
    """スタイル選択の2段階目ビュー"""
    def __init__(self, mode: str, user_id: int, char_name: str, styles: list):
        super().__init__(timeout=60)
        self.mode = mode
        self.user_id = user_id
        self.char_name = char_name
        self.styles = styles
        
        options = [
            discord.SelectOption(label=s['name'], value=str(s['id']), description=f"ID: {s['id']}")
            for s in styles[:25]
        ]
        
        select = discord.ui.Select(
            placeholder="スタイルを選択",
            options=options,
            custom_id=f"style_select_{mode}"
        )
        select.callback = self.style_selected
        self.add_item(select)
        
        # 戻るボタン
        back_btn = discord.ui.Button(label="◀ キャラ選択に戻る", style=discord.ButtonStyle.secondary)
        back_btn.callback = self.go_back
        self.add_item(back_btn)
    
    async def style_selected(self, interaction: discord.Interaction):
        global SPEAKER_ID
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これは他の人のメニューじゃ。", ephemeral=True)
            return
        
        style_id = int(interaction.data['values'][0])
        style_name = next((s['name'] for s in self.styles if s['id'] == style_id), "不明")
        
        await apply_voice(interaction, self.mode, self.char_name, style_name, style_id)
    
    async def go_back(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("これは他の人のメニューじゃ。", ephemeral=True)
            return
        view = CharacterSelectView(self.mode, self.user_id)
        label = "マイボイス" if self.mode == "myvoice" else "もち神さまボイス"
        await interaction.response.edit_message(
            content=f"🎤 **{label}**: キャラクターを選ぶのじゃ：",
            view=view
        )


@bot.tree.command(name="myvoice", description="自分のチャット読み上げ声を設定するのじゃ")
async def my_voice(interaction: discord.Interaction):
    if not character_styles:
        await interaction.response.send_message("⚠️ 話者一覧がまだ取得できておらぬ。少し待つのじゃ。", ephemeral=True)
        return
    
    # 現在の設定を表示
    user_id = str(interaction.user.id)
    current = user_voices.get(user_id)
    if current:
        status = f"現在の設定: **{current['name']}**\n"
    else:
        status = "現在未設定（もち神さまの声で読み上げ中）\n"
    
    view = CharacterSelectView("myvoice", interaction.user.id)
    await interaction.response.send_message(
        f"🎤 **マイボイス設定**\n{status}キャラクターを選ぶのじゃ：",
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="botvoice", description="もち神さまの声を変更するのじゃ")
async def bot_voice(interaction: discord.Interaction):
    if not character_styles:
        await interaction.response.send_message("⚠️ 話者一覧がまだ取得できておらぬ。少し待つのじゃ。", ephemeral=True)
        return
    
    current_name = speaker_map_reverse.get(SPEAKER_ID, f"ID={SPEAKER_ID}")
    
    view = CharacterSelectView("botvoice", interaction.user.id)
    await interaction.response.send_message(
        f"🎤 **もち神さまボイス設定**\n現在の声: **{current_name}**\nキャラクターを選ぶのじゃ：",
        view=view,
        ephemeral=True
    )


@bot.tree.command(name="album", description="デザートのアルバムを表示するのじゃ")
async def desert_album(interaction: discord.Interaction):
    msg = (
        "🎵 デザートのアルバムじゃ。聴くがよい。\n\n"
        "🏜️ **DESERT MEMBER SONG 2024**\n"
        "https://soundcloud.com/shouyu-mochi/sets/desert-theme-song/s-0y6FdI6ccI3?si=9a004c595feb46e7b67547a3ca0a1638&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing\n\n"
        "🎤 **DESERT MEMBER SONG 2025**\n"
        "https://soundcloud.com/shouyu-mochi/sets/desert-member-song-2025-test/s-klf6JFeRYpP?si=276edc9d114643028d7c334f07d9c1a7&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing"
    )
    await interaction.response.send_message(msg)


@bot.tree.command(name="socho", description="ソーチョーの幻想盤のURLを表示するのじゃ")
async def fauxhollows(interaction: discord.Interaction):
    await interaction.response.send_message("🦊 **ソーチョーの幻想盤**\nhttps://knt-a.com/fauxhollows/")

# ==========================================
# SLASH COMMANDS (会話検知)
# ==========================================

@bot.tree.command(name="vchat_on", description="会話検知モードをオンにするのじゃ")
async def voice_chat_on(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_connected():
        await interaction.response.send_message("先に `!mjoin` でわしを呼ぶのじゃ。", ephemeral=True)
        return
        
    state = get_guild_state(interaction.guild_id)
    state["voice_chat_mode"] = True
    # バッファ録音を開始
    start_rolling_buffer(vc)
    await interaction.response.send_message(
        "👂 会話を聞き始めるのじゃ。\n"
        "※会話が30秒途切れると、もち神さまが相槌を打つのじゃ。"
    )
    # モニタータスクを開始
    if not voice_chat_monitor_task.is_running():
        voice_chat_monitor_task.start()


@bot.tree.command(name="vchat_off", description="会話検知モードをオフにするのじゃ")
async def voice_chat_off(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    state["voice_chat_mode"] = False
    state["voice_last_triggered"] = None
    state["voice_last_audio_time"] = None
    vc = interaction.guild.voice_client
    
    # バッファ録音を停止
    if vc:
        stop_rolling_buffer(vc)
        
    await interaction.response.send_message("🔇 会話検知を止めるのじゃ。")

# ==========================================
# MINI GAMES
# ==========================================

class DiceBattleLobbyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="🎲 参加する", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        session = game_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("セッションが見つからぬ。", ephemeral=True)
            return
        if interaction.user in session["players"]:
            await interaction.response.send_message("すでに参加しておるぞ。", ephemeral=True)
            return
        if len(session["players"]) >= MAX_PLAYERS:
            await interaction.response.send_message(f"参加者が上限（{MAX_PLAYERS}人）に達しておる。", ephemeral=True)
            return
        session["players"].append(interaction.user)
        embed = build_dice_lobby_embed(session)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(label="🔒 締め切る", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        session = game_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("セッションが見つからぬ。", ephemeral=True)
            return
        if interaction.user != session["host"]:
            await interaction.response.send_message("主催者のみ締め切れるのじゃ。", ephemeral=True)
            return
        if len(session["players"]) < 2:
            await interaction.response.send_message("参加者が2人以上必要じゃ。", ephemeral=True)
            return
        self.stop()
        await interaction.response.defer()
        await run_dice_battle(interaction, session)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        session = game_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("セッションが見つからぬ。", ephemeral=True)
            return
        if interaction.user != session["host"]:
            await interaction.response.send_message("主催者のみキャンセルできるのじゃ。", ephemeral=True)
            return
        game_sessions.pop(channel_id, None)
        self.stop()
        await interaction.response.edit_message(content="❌ ダイスバトルをキャンセルしたぞ。", embed=None, view=None)


def build_dice_lobby_embed(session: dict) -> discord.Embed:
    embed = discord.Embed(title="🎲 ダイスバトル　参加受付中！", color=discord.Color.blue())
    players = session["players"]
    if players:
        player_list = "\n".join(f"🟢 {p.display_name}" for p in players)
    else:
        player_list = "まだいない"
    embed.add_field(
        name=f"参加者（{len(players)}人 / 最大{MAX_PLAYERS}人）",
        value=player_list,
        inline=False
    )
    embed.set_footer(text="主催者が「締め切る」を押すとゲームスタートじゃ")
    return embed


async def run_dice_battle(interaction: discord.Interaction, session: dict):
    players = session["players"]
    results = []
    for player in players:
        roll = random.randint(1, 100)
        results.append((player, roll))
    results.sort(key=lambda x: x[1], reverse=True)

    # 同点対応の順位計算
    ranked = []
    current_rank = 1
    for i, (player, roll) in enumerate(results):
        if i > 0 and roll < results[i - 1][1]:
            current_rank = i + 1
        ranked.append((current_rank, player, roll))

    embed = discord.Embed(title="🎲✨ ダイスバトル結果 ✨🎲", color=discord.Color.gold())
    for rank, player, roll in ranked:
        if rank == 1:
            medal = "🥇"
        elif rank == 2:
            medal = "🥈"
        elif rank == 3:
            medal = "🥉"
        else:
            medal = f"{rank}位"
        value = f"🎲 **{roll}**"
        if rank == 1:
            value += " 👑"
        embed.add_field(name=f"{medal} {player.display_name}", value=value, inline=False)

    winner = ranked[0][1]
    embed.set_footer(text=f"参加者 {len(players)}名　｜　🏆 優勝：{winner.display_name}！おめでとうじゃ！🎉")

    await interaction.followup.send(embed=embed)
    game_sessions.pop(interaction.channel_id, None)


class JankenLobbyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="✋ 参加する", style=discord.ButtonStyle.success)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        session = game_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("セッションが見つからぬ。", ephemeral=True)
            return
        if interaction.user in session["players"]:
            await interaction.response.send_message("すでに参加しておるぞ。", ephemeral=True)
            return
        if len(session["players"]) >= MAX_PLAYERS:
            await interaction.response.send_message(f"参加者が上限（{MAX_PLAYERS}人）に達しておる。", ephemeral=True)
            return
        session["players"].append(interaction.user)
        embed = build_janken_lobby_embed(session)
        await interaction.response.edit_message(embed=embed)

    @discord.ui.button(label="🔒 締め切る", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        session = game_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("セッションが見つからぬ。", ephemeral=True)
            return
        if interaction.user != session["host"]:
            await interaction.response.send_message("主催者のみ締め切れるのじゃ。", ephemeral=True)
            return
        if len(session["players"]) < 2:
            await interaction.response.send_message("参加者が2人以上必要じゃ。", ephemeral=True)
            return
        self.stop()
        await interaction.response.defer()
        await start_janken_round(interaction, session, round_num=1)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        session = game_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("セッションが見つからぬ。", ephemeral=True)
            return
        if interaction.user != session["host"]:
            await interaction.response.send_message("主催者のみキャンセルできるのじゃ。", ephemeral=True)
            return
        game_sessions.pop(channel_id, None)
        self.stop()
        await interaction.response.edit_message(content="❌ じゃんけん大会をキャンセルしたぞ。", embed=None, view=None)


def build_janken_lobby_embed(session: dict) -> discord.Embed:
    embed = discord.Embed(title="✊ じゃんけん大会　参加受付中！", color=discord.Color.purple())
    players = session["players"]
    if players:
        player_list = "\n".join(f"🟢 {p.display_name}" for p in players)
    else:
        player_list = "まだいない"
    embed.add_field(
        name=f"参加者（{len(players)}人 / 最大{MAX_PLAYERS}人）",
        value=player_list,
        inline=False
    )
    embed.set_footer(text="主催者が「締め切る」を押すとゲームスタートじゃ")
    return embed


class JankenHandView(discord.ui.View):
    def __init__(self, session: dict, round_num: int):
        super().__init__(timeout=60)
        self.session = session
        self.round_num = round_num

    async def handle_choice(self, interaction: discord.Interaction, hand: str):
        channel_id = interaction.channel_id
        session = game_sessions.get(channel_id)
        if session is None:
            await interaction.response.send_message("セッションが見つからぬ。", ephemeral=True)
            return
        if interaction.user not in session["players"]:
            await interaction.response.send_message("参加者のみ選択できるのじゃ。", ephemeral=True)
            return
        if interaction.user.id in session["choices"]:
            await interaction.response.send_message("すでに選択済みじゃ。", ephemeral=True)
            return
        session["choices"][interaction.user.id] = hand
        await interaction.response.send_message(f"✅ **{hand}** を選んだのじゃ！（他の人には見えないぞ）", ephemeral=True)
        if len(session["choices"]) == len(session["players"]):
            self.stop()
            await show_janken_result(interaction, session, self.round_num)

    @discord.ui.button(label="✊ グー", style=discord.ButtonStyle.secondary)
    async def rock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "グー")

    @discord.ui.button(label="✌️ チョキ", style=discord.ButtonStyle.secondary)
    async def scissors_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "チョキ")

    @discord.ui.button(label="🖐️ パー", style=discord.ButtonStyle.secondary)
    async def paper_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "パー")


async def start_janken_round(interaction: discord.Interaction, session: dict, round_num: int):
    session["choices"] = {}
    view = JankenHandView(session, round_num)
    player_list = "\n".join(f"⏳ {p.display_name}" for p in session["players"])
    embed = discord.Embed(
        title=f"✊ 第{round_num}回戦　手を選んでください！",
        color=discord.Color.orange()
    )
    embed.add_field(name="参加者", value=player_list, inline=False)
    embed.set_footer(text="ボタンを押して手を選ぶのじゃ（他の人には見えないぞ）")
    await interaction.followup.send(embed=embed, view=view)


def judge_janken(choices: dict) -> str:
    hands = set(choices.values())
    if len(hands) == 1 or len(hands) == 3:
        return "あいこ"
    if hands == {"グー", "チョキ"}:
        return "グー"
    if hands == {"チョキ", "パー"}:
        return "チョキ"
    if hands == {"パー", "グー"}:
        return "パー"
    return "あいこ"


async def show_janken_result(interaction: discord.Interaction, session: dict, round_num: int):
    hand_emoji = {"グー": "✊", "チョキ": "✌️", "パー": "🖐️"}
    choices = session["choices"]
    players = session["players"]
    result = judge_janken(choices)

    if result == "あいこ":
        embed = discord.Embed(title="✊ じゃんけん結果", color=discord.Color.yellow())
        lines = []
        for p in players:
            hand = choices[p.id]
            emoji = hand_emoji[hand]
            lines.append(f"{emoji} {p.display_name}　{hand}")
        embed.add_field(name="🤝 あいこ！もう一度じゃ！", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"第{round_num}回戦　あいこ")
        await interaction.followup.send(embed=embed)
        await asyncio.sleep(2)
        await start_janken_round(interaction, session, round_num + 1)
    else:
        winners = [p for p in players if choices[p.id] == result]
        losers = [p for p in players if choices[p.id] != result]
        embed = discord.Embed(title="✊ じゃんけん結果", color=discord.Color.green())
        winner_lines = []
        for p in winners:
            hand = choices[p.id]
            emoji = hand_emoji[hand]
            winner_lines.append(f"{emoji} {p.display_name}　{hand}　👑")
        embed.add_field(name=f"🏆 {result}の勝ち！", value="\n".join(winner_lines), inline=False)
        loser_lines = []
        for p in losers:
            hand = choices[p.id]
            emoji = hand_emoji[hand]
            loser_lines.append(f"{emoji} {p.display_name}　{hand}")
        embed.add_field(name="💨 敗者", value="\n".join(loser_lines), inline=False)
        winner_names = "、".join(p.display_name for p in winners)
        embed.set_footer(text=f"第{round_num}回戦終了　🏆 優勝：{winner_names}！")
        await interaction.followup.send(embed=embed)
        game_sessions.pop(interaction.channel_id, None)


async def start_dice_battle(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id in game_sessions:
        await interaction.response.send_message("すでにゲームが進行中じゃ。", ephemeral=True)
        return
    session = {"host": interaction.user, "players": [interaction.user], "type": "dice"}
    game_sessions[channel_id] = session
    view = DiceBattleLobbyView()
    embed = build_dice_lobby_embed(session)
    await interaction.response.send_message(embed=embed, view=view)


async def start_janken(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id in game_sessions:
        await interaction.response.send_message("すでにゲームが進行中じゃ。", ephemeral=True)
        return
    session = {"host": interaction.user, "players": [interaction.user], "type": "janken", "choices": {}}
    game_sessions[channel_id] = session
    view = JankenLobbyView()
    embed = build_janken_lobby_embed(session)
    await interaction.response.send_message(embed=embed, view=view)


# ==========================================
# SLASH COMMANDS (UI Dashboard / menu)
# ==========================================

class MusicSelectView(discord.ui.View):
    def __init__(self, entries: list[dict], interaction_user_id: int):
        super().__init__(timeout=60)
        self.add_item(MusicSelectMenu(entries, interaction_user_id))

class MusicSelectMenu(discord.ui.Select):
    def __init__(self, entries: list[dict], interaction_user_id: int):
        self.entries = entries
        self.interaction_user_id = interaction_user_id
        options = [
            discord.SelectOption(
                label=entry["title"][:100],
                value=str(i),
                description=entry.get("duration_string", "")
            )
            for i, entry in enumerate(entries)
        ]
        super().__init__(placeholder="再生する曲を選ぶのじゃ", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.interaction_user_id:
            await interaction.response.send_message("これは他の人の選択じゃ。", ephemeral=True)
            return

        guild = interaction.guild
        state = get_guild_state(guild.id)
        vc = guild.voice_client

        if vc is None:
            await interaction.response.send_message("ボイスチャンネルに入るのじゃ。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)

        entry = self.entries[int(self.values[0])]
        url = entry["url"]
        title = entry.get("title", "不明な曲")

        try:
            if vc.is_playing():
                vc.stop()

            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(url, **ffmpeg_opts), volume=MUSIC_VOLUME)

            def after_playing(error):
                state["is_playing_music"] = False

            vc.play(source, after=after_playing)
            state["is_playing_music"] = True
            await interaction.followup.send(f"🎵 **再生中**: {title} (音量: {int(MUSIC_VOLUME*100)}%)")
        except Exception as e:
            print(f"Play Error: {e}")
            await interaction.followup.send("見つからなんだ、または再生できぬ。")
            state["is_playing_music"] = False


class MusicPlayModal(discord.ui.Modal, title="音楽を再生する"):
    url = discord.ui.TextInput(
        label="URL または 検索キーワード",
        style=discord.TextStyle.short,
        placeholder="例: https://youtube.com/... または FF14 BGM",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        query = self.url.value.strip()
        is_url = query.startswith("http")
        
        # すぐにdeferしてDiscordの3秒タイムアウトを防ぐ
        await interaction.response.defer(ephemeral=not is_url)

        guild = interaction.guild
        state = get_guild_state(guild.id)
        vc = guild.voice_client

        if vc is None:
            if interaction.user.voice:
                try:
                    vc = await interaction.user.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
                    state["active_channel_id"] = interaction.channel.id
                except Exception as e:
                    print(f"Voice Connect Error: {e}")
                    await interaction.followup.send("ボイスチャンネルに接続できなかったのじゃ。", ephemeral=True)
                    return
            else:
                await interaction.followup.send("ボイスチャンネルに入るのじゃ。", ephemeral=True)
                return

        # URLの場合はそのまま再生
        if is_url:
            msg = await interaction.followup.send(f"「{query}」のレコードを探しておる...")
            try:
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
                if 'entries' in data:
                    data = data['entries'][0]
                url = data['url']
                title = data.get('title', '不明な曲')
                if vc.is_playing(): vc.stop()
                source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(url, **ffmpeg_opts), volume=MUSIC_VOLUME)
                def after_playing(error):
                    state["is_playing_music"] = False
                vc.play(source, after=after_playing)
                state["is_playing_music"] = True
                await msg.edit(content=f"🎵 **再生中**: {title} (音量: {int(MUSIC_VOLUME*100)}%)")
            except Exception as e:
                print(f"Play Error: {e}")
                await msg.edit(content="見つからなんだ、または再生できぬ。")
                state["is_playing_music"] = False
            return

        # キーワードの場合は5件取得してセレクトメニューを表示
        search_query = f"ytsearch5:{query} bgm"
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
            entries = data.get("entries", [])
            if not entries:
                await interaction.followup.send("見つからなんだ。", ephemeral=True)
                return
            view = MusicSelectView(entries, interaction.user.id)
            await interaction.followup.send("🎵 再生する曲を選ぶのじゃ：", view=view, ephemeral=True)
        except Exception as e:
            print(f"Search Error: {e}")
            await interaction.followup.send("検索に失敗したのう。", ephemeral=True)

class VolumeModal(discord.ui.Modal, title="音量変更"):
    volume = discord.ui.TextInput(
        label="音量 (0 〜 80)",
        style=discord.TextStyle.short,
        placeholder="20",
        required=True,
        max_length=2
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            vol_val = int(self.volume.value)
            if not 0 <= vol_val <= 80:
                raise ValueError
        except Exception as e:
            print(f"⚠️ エラー: {e}")
            await interaction.response.send_message("❌ 0～80の整数を指定するのじゃ。", ephemeral=True)
            return
            
        global MUSIC_VOLUME
        MUSIC_VOLUME = vol_val / 100.0
        state = get_guild_state(interaction.guild_id)
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and vc.source and state["is_playing_music"]:
            update_source_volume(vc.source, MUSIC_VOLUME)
            
        await interaction.response.send_message("操作を受け付けたぞ。", ephemeral=True)
        await interaction.channel.send(f"🔊 音量を **{vol_val}%** に変更したぞ。")

class MochimochiModal(discord.ui.Modal, title="もちもちに話しかける"):
    question = discord.ui.TextInput(
        label="キーワード・質問",
        placeholder="例：今日のおすすめのジョブは？",
        max_length=50
    )

    async def on_submit(self, interaction: discord.Interaction):
        user_question = self.question.value.strip()
        if not user_question:
            await interaction.response.send_message("何も入力されておらぬ。", ephemeral=True)
            return

        state = get_guild_state(interaction.guild_id)
        channel = interaction.channel
        
        await interaction.response.send_message("もち神さまが考えておるぞ...", ephemeral=True)

        async with channel.typing():
            try:
                use_search = any(k in user_question for k in SEARCH_KEYWORDS) or "教えて" in user_question
                target_config = config_search if use_search else config_normal
                history = [f"{msg.author.display_name}: {msg.content}" async for msg in channel.history(limit=15)]
                full_prompt = f"履歴：\n" + "\n".join(reversed(history)) + f"\n\n質問：{user_question}"
                response = await client.aio.models.generate_content(
                    model=MODEL_NAME, contents=full_prompt, config=target_config
                )
                log_token_usage(response, "Chat(Modal)")
                ai_text = response.text
                
                # 自分以外のみんなに見えるように、channel.send()を使用する
                await channel.send(f"💬 **{interaction.user.display_name}**：{user_question}\n\n{ai_text}")
                
                if not use_search and not state["is_playing_music"]:
                    audio_data = await generate_wav(ai_text, SPEAKER_ID)
                    if audio_data: play_audio(interaction.guild, audio_data)
                    
                await interaction.edit_original_response(content="✅ 送信したのじゃ。")
            except Exception as e:
                print(f"Error: {e}")
                await interaction.edit_original_response(content="❌ 天界の網が乱れておるのう。")

class MainMenuSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="音楽を再生する", value="play", emoji="🎵"),
            discord.SelectOption(label="音楽の音量変更", value="volume", emoji="🔊"),
            discord.SelectOption(label="音楽を停止する", value="stop", emoji="🛑"),
            discord.SelectOption(label="マイボイスの変更", value="myvoice", emoji="🎤"),
            discord.SelectOption(label="ボットボイスの変更", value="botvoice", emoji="🗣️"),
            discord.SelectOption(label="会話検知 (オン/オフ)", value="voice_chat", emoji="💬"),
            discord.SelectOption(label="もちもちに話しかける", value="mochimochi_chat", emoji="🤖"),
            discord.SelectOption(label="ダイスバトル", value="dice_battle", emoji="🎲"),
            discord.SelectOption(label="じゃんけん", value="janken_game", emoji="✊"),
        ]

        # menu_links.json から動的にリンク項目を追加
        existing_values = {o.value for o in options} | {"disconnect", "cancel"}
        for item in load_menu_links():
            if item["value"] in existing_values:
                continue  # value重複はスキップ
            if len(options) >= 23:
                break  # Discordのセレクトメニュー上限 (キャンセルの余裕を持たせる)
            options.append(discord.SelectOption(
                label=item["label"],
                value=item["value"],
                emoji=item.get("emoji", "🔗")
            ))

        # disconnect を常に最後に追加
        options.append(discord.SelectOption(label="もち神さまとお別れする", value="disconnect", emoji="👋"))
        options.append(discord.SelectOption(label="キャンセル", value="cancel", emoji="❌"))

        super().__init__(placeholder="メニューを選ぶのじゃ", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        val = self.values[0]
        guild = interaction.guild
        vc = guild.voice_client if guild else None
        state = get_guild_state(interaction.guild_id)
        
        if val == "play":
            await interaction.response.send_modal(MusicPlayModal())
        elif val == "volume":
            await interaction.response.send_modal(VolumeModal())
        elif val == "stop":
            if vc and vc.is_playing():
                vc.stop()
                state["is_playing_music"] = False
                await interaction.response.send_message("操作を受け付けたぞ。", ephemeral=True)
                await interaction.channel.send("🛑 音楽を止めたぞ。")
            else:
                await interaction.response.send_message("何も流れておらぬ。", ephemeral=True)
        elif val == "myvoice":
            view = CharacterSelectView("myvoice", interaction.user.id)
            current = user_voices.get(str(interaction.user.id))
            status = f"現在の設定: **{current['name']}**\n" if current else "現在未設定\n"
            await interaction.response.send_message(f"🎤 **マイボイス設定**\n{status}キャラクターを選ぶのじゃ：", view=view, ephemeral=True)
        elif val == "botvoice":
            view = CharacterSelectView("botvoice", interaction.user.id)
            current_name = speaker_map_reverse.get(SPEAKER_ID, f"ID={SPEAKER_ID}")
            await interaction.response.send_message(f"🎤 **もち神さまボイス設定**\n現在の声: **{current_name}**\nキャラクターを選ぶのじゃ：", view=view, ephemeral=True)
        elif val == "voice_chat":
            if not vc:
                await interaction.response.send_message("先に `!mjoin` でわしを呼ぶのじゃ。", ephemeral=True)
                return
            if state["voice_chat_mode"]:
                state["voice_chat_mode"] = False
                state["voice_last_triggered"] = None
                state["voice_last_audio_time"] = None
                stop_rolling_buffer(vc)
                await interaction.response.send_message("🔇 会話検知をオフにしたぞ。", ephemeral=True)
            else:
                state["voice_chat_mode"] = True
                start_rolling_buffer(vc)
                await interaction.response.send_message("👂 会話検知をオンにしたぞ。", ephemeral=True)
                if not voice_chat_monitor_task.is_running():
                    voice_chat_monitor_task.start()
        elif val == "mochimochi_chat":
            await interaction.response.send_modal(MochimochiModal())
        elif val == "dice_battle":
            await start_dice_battle(interaction)
        elif val == "janken_game":
            await start_janken(interaction)
        elif val == "disconnect":
            if vc:
                await interaction.response.send_message("操作を受け付けたぞ。", ephemeral=True)
                await interaction.channel.send("👋 さらばじゃ。")
                if state["voice_chat_mode"]:
                    stop_rolling_buffer(vc)
                
                # 状態を確実にリセット
                state["voice_chat_mode"] = False
                state["voice_last_triggered"] = None
                state["voice_last_audio_time"] = None
                state["active_channel_id"] = None
                state["is_playing_music"] = False
                state["voice_buffer_active"] = False
                if state["rolling_sink"]:
                    state["rolling_sink"].clear()
                    state["rolling_sink"] = None
                    
                await vc.disconnect()
            else:
                await interaction.response.send_message("わしはまだおらんぞ。", ephemeral=True)
        elif val == "cancel":
            try:
                await interaction.message.delete()
            except Exception:
                await interaction.response.send_message("キャンセルしたのじゃ。", ephemeral=True)
        else:
            # menu_links.json 由来のリンク項目を処理
            for item in load_menu_links():
                if val == item["value"]:
                    emoji = item.get("emoji", "🔗")
                    if "links" in item and isinstance(item["links"], list):
                        view = discord.ui.View()
                        for link in item["links"]:
                            view.add_item(discord.ui.Button(
                                style=discord.ButtonStyle.link,
                                label=link.get("title", "リンク"),
                                url=link.get("url", "")
                            ))
                        await interaction.response.send_message(
                            f"{emoji} **{item['label']}**",
                            view=view
                        )
                    else:
                        await interaction.response.send_message(
                            f"{emoji} **{item['label']}**\n{item.get('url', '')}"
                        )
                    return
            await interaction.response.send_message("不明な操作じゃ。", ephemeral=True)

class MainMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(MainMenuSelect())

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(content="⏱️ タイムアウトしたのじゃ。", view=self)
        except Exception:
            pass

@bot.tree.command(name="menu", description="もち神さまの操作パネルを開くのじゃ")
async def slash_menu(interaction: discord.Interaction):
    view = MainMenuView()
    await interaction.response.send_message("⚙️ **もち神さま ダッシュボード**\n操作を選ぶのじゃ：", view=view, ephemeral=True)
    view.message = await interaction.original_response()



# ==========================================
# SLASH COMMANDS (もちもち)
# ==========================================


@bot.tree.command(name="janken", description="じゃんけん大会を開催するのじゃ")
async def slash_janken(interaction: discord.Interaction):
    await start_janken(interaction)


@bot.tree.command(name="listen", description="声で質問するのじゃ")
async def slash_mochimochi_listen(interaction: discord.Interaction):
    """ユーザーの音声を録音し、Gemini APIで文字起こし→AI応答する"""
    guild_id = interaction.guild_id
    state = get_guild_state(guild_id)

    # === 前提条件チェック ===
    if not interaction.user.voice:
        await interaction.response.send_message("ボイスチャンネルに入るのじゃ。", ephemeral=True)
        return

    vc = interaction.guild.voice_client
    if vc is None:
        await interaction.response.send_message("先に `!mjoin` でわしを呼ぶのじゃ。", ephemeral=True)
        return

    # VoiceRecvClient かどうかチェック
    if not isinstance(vc, voice_recv.VoiceRecvClient):
        await interaction.response.send_message("音声受信に対応しておらぬ。`!mjoin` でわしを呼び直すのじゃ。", ephemeral=True)
        return

    # === クールダウンチェック ===
    now = time.time()
    last_used = listen_cooldowns.get(guild_id, 0)
    remaining = LISTEN_COOLDOWN - (now - last_used)
    if remaining > 0:
        await interaction.response.send_message(f"⏳ まだ耳が休まっておらぬ。あと **{int(remaining)}秒** 待つのじゃ。", ephemeral=True)
        return

    # === 同時録音セッション制限 ===
    if listening_sessions.get(guild_id, False):
        await interaction.response.send_message("🔴 今はすでに聞いておるぞ。少し待つのじゃ。", ephemeral=True)
        return

    # === 音楽再生中チェック ===
    if state["is_playing_music"]:
        await interaction.response.send_message("🎵 音楽が流れておるから聞き取れぬ。`/stop` してから試すのじゃ。", ephemeral=True)
        return

    # 3秒以内にdeferで応答
    await interaction.response.defer()

    # 会話検知バッファとの競合を防ぐため一時停止
    was_buffer_active = state["voice_buffer_active"]
    if was_buffer_active and vc:
        stop_rolling_buffer(vc)

    listening_sessions[guild_id] = True
    listen_cooldowns[guild_id] = now

    target_user = interaction.user
    await interaction.followup.send(f"👂 **{target_user.display_name}**、{LISTEN_DURATION}秒間聞いておるぞ。話すのじゃ！")

    # === 録音処理 ===
    wav_filename = f'listen_{uuid.uuid4()}.wav'
    try:
        # WaveSink + UserFilter で特定ユーザーのみ録音
        sink = voice_recv.WaveSink(wav_filename)
        filtered_sink = voice_recv.UserFilter(sink, target_user)

        vc.listen(filtered_sink)

        # 指定時間待機
        await asyncio.sleep(LISTEN_DURATION)

        # 録音停止
        vc.stop_listening()

        # ファイルが存在し、中身があるか確認
        if not os.path.exists(wav_filename) or os.path.getsize(wav_filename) < 1000:
            await interaction.followup.send("🔇 何も聞こえなかったのじゃ。マイクを確認せよ。")
            return

        # === Gemini APIで文字起こし ===
        try:
            # 音声ファイルをバイナリで読み込み
            with open(wav_filename, 'rb') as f:
                audio_data = f.read()

            # Gemini APIに音声を送信して文字起こし
            audio_part = types.Part.from_bytes(
                data=audio_data,
                mime_type="audio/wav"
            )

            stt_response = await client.aio.models.generate_content(
                model=MODEL_NAME,
                contents=["この音声を文字起こしせよ。", audio_part],
                config=config_stt
            )
            log_token_usage(stt_response, "STT")

            transcribed_text = stt_response.text.strip()
            print(f"📝 STT結果: {transcribed_text}", flush=True)

            if not transcribed_text or "聞き取れなかった" in transcribed_text:
                await interaction.followup.send("🔇 聞き取れなかったのじゃ。もう少しはっきり話すのじゃ。")
                return

            # 文字起こし結果を表示
            await interaction.followup.send(f"📝 **聞き取り結果**: {transcribed_text}")

            # === 文字起こし結果をGemini通常会話に送信 ===
            # 入力制限チェック
            if len(transcribed_text) > 100:
                transcribed_text = transcribed_text[:100]

            use_search = any(k in transcribed_text for k in SEARCH_KEYWORDS) or "教えて" in transcribed_text
            target_config = config_search if use_search else config_normal

            channel = interaction.channel
            history = [f"{msg.author.display_name}: {msg.content}" async for msg in channel.history(limit=15)]
            full_prompt = f"履歴：\n" + "\n".join(reversed(history)) + f"\n\n質問：{transcribed_text}"
            
            print(f"📤 [/もちもち] Geminiへの送信プロンプト:\n{full_prompt}", flush=True)

            ai_response = await client.aio.models.generate_content(
                model=MODEL_NAME, contents=full_prompt, config=target_config
            )
            log_token_usage(ai_response, "ListenChat")

            ai_text = ai_response.text
            print(f"🤖 [/もちもち] AI回答: {ai_text}", flush=True)
            await interaction.followup.send(ai_text)

            # 読み上げ（検索結果でなければ）
            if not use_search and not state["is_playing_music"]:
                audio_data = await generate_wav(ai_text, SPEAKER_ID)
                if audio_data: play_audio(interaction.guild, audio_data)

        except Exception as e:
            print(f"Listen STT/Chat Error: {e}")
            await interaction.followup.send("天界の耳が乱れておるのう。もう一度試すのじゃ。")

    except Exception as e:
        print(f"Listen Error: {e}")
        await interaction.followup.send("録音に失敗したのじゃ。")
        # 安全に録音を停止
        try:
            if vc.is_listening():
                vc.stop_listening()
        except Exception as e: print(f"⚠️ エラー: {e}")

    finally:
        # 一時ファイルのクリーンアップ（録音用ファイルはディスクI/O必須）
        try:
            if os.path.exists(wav_filename):
                os.remove(wav_filename)
        except Exception as e: print(f"⚠️ エラー: {e}")
        listening_sessions[guild_id] = False
        
        # 会話検知バッファを復帰
        if was_buffer_active and state["voice_chat_mode"]:
            vc = interaction.guild.voice_client
            if vc and vc.is_connected():
                start_rolling_buffer(vc)


# ==========================================
# NEW SLASH COMMANDS
# ==========================================

@bot.tree.command(name="play", description="音楽を再生するのじゃ")
@app_commands.describe(query="YouTubeのURLまたは検索キーワード")
async def slash_play(interaction: discord.Interaction, query: str):
    query = query.strip()
    is_url = query.startswith("http")
    await interaction.response.defer(ephemeral=not is_url)

    guild = interaction.guild
    state = get_guild_state(guild.id)
    vc = guild.voice_client

    if vc is None:
        if interaction.user.voice:
            try:
                vc = await interaction.user.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
                state["active_channel_id"] = interaction.channel.id
            except Exception as e:
                print(f"Voice Connect Error: {e}")
                await interaction.followup.send("ボイスチャンネルに接続できなかったのじゃ。", ephemeral=True)
                return
        else:
            await interaction.followup.send("ボイスチャンネルに入るのじゃ。", ephemeral=True)
            return

    if is_url:
        msg = await interaction.followup.send(f"「{query}」のレコードを探しておる...")
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            if 'entries' in data:
                data = data['entries'][0]
            url = data['url']
            title = data.get('title', '不明な曲')
            if vc.is_playing(): vc.stop()
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(url, **ffmpeg_opts), volume=MUSIC_VOLUME)
            def after_playing(error):
                state["is_playing_music"] = False
            vc.play(source, after=after_playing)
            state["is_playing_music"] = True
            await msg.edit(content=f"🎵 **再生中**: {title} (音量: {int(MUSIC_VOLUME*100)}%)")
        except Exception as e:
            print(f"Play Error: {e}")
            await msg.edit(content="見つからなんだ、または再生できぬ。")
            state["is_playing_music"] = False
        return

    search_query = f"ytsearch5:{query} bgm"
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
        entries = data.get("entries", [])
        if not entries:
            await interaction.followup.send("見つからなんだ。", ephemeral=True)
            return
        view = MusicSelectView(entries, interaction.user.id)
        await interaction.followup.send("🎵 再生する曲を選ぶのじゃ：", view=view, ephemeral=True)
    except Exception as e:
        print(f"Search Error: {e}")
        await interaction.followup.send("検索に失敗したのう。", ephemeral=True)

@bot.tree.command(name="stop", description="音楽を停止するのじゃ")
async def slash_stop(interaction: discord.Interaction):
    state = get_guild_state(interaction.guild_id)
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.stop()
        state["is_playing_music"] = False
        await interaction.response.send_message("止めたぞ。", ephemeral=True)
        await interaction.channel.send("🛑 音楽を止めたぞ。")
    else:
        await interaction.response.send_message("何も流れておらぬ。", ephemeral=True)

@bot.tree.command(name="volume", description="音楽の音量を変更するのじゃ")
@app_commands.describe(volume="音量（0〜80）")
async def slash_volume(interaction: discord.Interaction, volume: int):
    if not 0 <= volume <= 80:
        await interaction.response.send_message("❌ 0～80の整数を指定するのじゃ。", ephemeral=True)
        return
        
    global MUSIC_VOLUME
    MUSIC_VOLUME = volume / 100.0
    state = get_guild_state(interaction.guild_id)
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.source and state["is_playing_music"]:
        update_source_volume(vc.source, MUSIC_VOLUME)
        
    await interaction.response.send_message("操作を受け付けたぞ。", ephemeral=True)
    await interaction.channel.send(f"🔊 音量を **{volume}%** に変更したぞ。")

@bot.tree.command(name="dicebattle", description="ダイスバトルを開催するのじゃ")
async def slash_dicebattle(interaction: discord.Interaction):
    await start_dice_battle(interaction)

@bot.tree.command(name="leave", description="もち神さまをVCから退出させるのじゃ")
async def slash_leave(interaction: discord.Interaction):
    guild = interaction.guild
    vc = guild.voice_client if guild else None
    state = get_guild_state(interaction.guild_id)
    
    if vc:
        await interaction.response.send_message("操作を受け付けたぞ。", ephemeral=True)
        await interaction.channel.send("👋 さらばじゃ。")
        if state["voice_chat_mode"]:
            stop_rolling_buffer(vc)
        
        state["voice_chat_mode"] = False
        state["voice_last_triggered"] = None
        state["voice_last_audio_time"] = None
        state["active_channel_id"] = None
        state["is_playing_music"] = False
        state["voice_buffer_active"] = False
        if state["rolling_sink"]:
            state["rolling_sink"].clear()
            state["rolling_sink"] = None
            
        await vc.disconnect()
    else:
        await interaction.response.send_message("わしはまだおらんぞ。", ephemeral=True)

def roll_dice(num: int) -> tuple[int, str]:
    res = random.randint(1, num)
    low_words = ["床ペロ", "雑魚よのう", "寄生か？", "無能じゃ", "ゴミじゃの", "非力すぎ", "出直せ雑魚"]
    mid_words = ["普通じゃ", "及第点じゃ", "凡夫じゃの", "無難じゃ", "まあまあ", "安泰じゃ", "悪くない"]
    high_words = ["良いぞ", "高めじゃ", "期待大", "さすが", "運が良い", "追い風", "上出来"]
    super_words = ["天才じゃ", "凄まじい", "豪運のう", "驚きじゃ", "最高じゃ", "神引き", "震える"]

    if res <= 35: reaction = random.choice(low_words)
    elif 36 <= res <= 70: reaction = random.choice(mid_words)
    elif 71 <= res <= 90: reaction = random.choice(high_words)
    else: reaction = random.choice(super_words)
    return res, reaction

async def summarize_dice(channel) -> str | None:
    limit_time = discord.utils.utcnow() - timedelta(minutes=10)
    history_list = [f"{msg.author.display_name}: {msg.content}" async for msg in channel.history(limit=100, after=limit_time)]
    if not history_list:
        return None
    history_newest_first = list(reversed(history_list))
    prompt = (
        "以下のチャット履歴（上が最新、下が過去）から、各ユーザーの最新のダイス結果（一番上にある『🔮 ... 【 数字 】』）を1つだけ特定せよ。\n"
        "それらの数字を集計し、降順（大きい順）でランキングを作成せよ。\n\n"
        "【重要：出力形式について】\n"
        "・Discordでズレるため、表組み（| や -）は絶対に使用するな。\n"
        "・以下のシンプルな箇条書き形式のみを使用せよ。\n"
        "  🥇 1位: [名前] 【 [数字] 】\n"
        "  🥈 2位: ...\n\n"
        "最後に優勝者を称え、最下位には軽い皮肉の言葉を述べよ。\n\n"
        + "\n".join(history_newest_first)
    )
    response = await client.aio.models.generate_content(
        model=MODEL_NAME, contents=prompt, config=config_summary
    )
    log_token_usage(response, "Summary")
    return response.text

@bot.tree.command(name="dice", description="ダイスを振るのじゃ")
@app_commands.describe(num="ダイスの最大値（デフォルト100）")
async def slash_dice(interaction: discord.Interaction, num: int = 100):
    res, reaction = roll_dice(num)
    text = f"🔮 **{interaction.user.display_name}** の目は **【 {res} 】** じゃ！ 「{reaction}」"
    await interaction.response.send_message(text)
    
    state = get_guild_state(interaction.guild_id)
    if not state["is_playing_music"]:
        audio_data = await generate_wav(f"{res}。{reaction}。", SPEAKER_ID)
        if audio_data: play_audio(interaction.guild, audio_data)

@bot.tree.command(name="diceresult", description="直近10分のダイス結果を集計するのじゃ")
async def slash_diceresult(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        result_text = await summarize_dice(interaction.channel)
        if not result_text:
            await interaction.followup.send("直近10分間にダイスの記録はないのう。")
            return
        await interaction.followup.send(result_text)
        
        state = get_guild_state(interaction.guild_id)
        if not state["is_playing_music"]:
            lines = result_text.strip().splitlines()
            last_line = lines[-1] if lines else "集計完了じゃ。"
            audio_data = await generate_wav(last_line, SPEAKER_ID)
            if audio_data: play_audio(interaction.guild, audio_data)
    except Exception as e:
        print(e)
        await interaction.followup.send("帳簿が開けぬ。")

# ==========================================
# PREFIX COMMANDS (play / stop / vol / mjoin / pause)
# ==========================================
@bot.command()
async def vol(ctx, volume: int):
    global MUSIC_VOLUME
    if not 0 <= volume <= 80:
        await ctx.send("❌ 0～80%の範囲で指定せよ。")
        return
    MUSIC_VOLUME = volume / 100.0
    state = get_guild_state(ctx.guild.id)
    if ctx.voice_client and ctx.voice_client.source and state["is_playing_music"]:
        update_source_volume(ctx.voice_client.source, MUSIC_VOLUME)
    await ctx.send(f"🔊 音楽の音量を **{volume}%** に変更したぞ。")

@bot.command()
async def play(ctx, *, query: str):
    state = get_guild_state(ctx.guild.id)
    if ctx.voice_client is None:
        if ctx.author.voice: await ctx.author.voice.channel.connect()
        else: return await ctx.send("ボイスチャンネルに入るのじゃ。")
    
    msg = await ctx.send(f"「{query}」のレコードを探しておる...")

    if query.startswith("http"):
        search_query = query
    else:
        search_query = f"ytsearch:{query} bgm"

    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
        
        if 'entries' in data:
            data = data['entries'][0]
        
        url = data['url']
        title = data.get('title', '不明な曲')
        
        if ctx.voice_client.is_playing(): ctx.voice_client.stop()
        
        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(url, **ffmpeg_opts), volume=MUSIC_VOLUME)
        
        def after_playing(error):
            state["is_playing_music"] = False
            
        ctx.voice_client.play(source, after=after_playing)
        state["is_playing_music"] = True
        
        await msg.edit(content=f"🎵 **再生中**: {title} (音量: {int(MUSIC_VOLUME*100)}%)")
    except Exception as e:
        print(f"Play Error: {e}")
        await msg.edit(content="見つからなんだ、または再生できぬ。")
        state["is_playing_music"] = False

@bot.command()
async def stop(ctx):
    state = get_guild_state(ctx.guild.id)
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        state["is_playing_music"] = False
        await ctx.send("止めたぞ。")
    else:
        await ctx.send("何も流れておらぬ。")

@bot.command()
async def mjoin(ctx):
    global MUSIC_VOLUME
    if ctx.author.voice:
        await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        state = get_guild_state(ctx.guild.id)
        state["active_channel_id"] = ctx.channel.id
        
        # ★追加: 接続時に音量を必ず20%にリセット
        MUSIC_VOLUME = 0.2
        
        # 会話モード初期化
        state["voice_chat_mode"] = False
        state["voice_last_triggered"] = None
        
        if gohan_police_task.is_running():
            gohan_police_task.cancel()
        gohan_police_task.start()
        
        async with ctx.typing():
            try:
                response = await client.aio.models.generate_content(
                    model=MODEL_NAME, contents="参加時の短い挨拶（一言、20文字以内）を1つだけ生成せよ。", config=config_monologue
                )
                log_token_usage(response, "Join")
                greet = response.text.strip()
            except Exception as e:
                print(f"⚠️ エラー: {e}")
                greet = "わしが来てやったぞ。"
        
        info_msg = (
            "\n\n"
            "/menu メニュー表示\n"
            "/play [URL or キーワード]\n"
            "/stop\n"
            "/volume [0-80]\n"
            "/dice [最大値]\n"
            "/diceresult\n"
            "/dicebattle\n"
            "/leave\n"
        )
        
        await ctx.send(greet + info_msg)
        audio_data = await generate_wav(greet, SPEAKER_ID)
        if audio_data: play_audio(ctx.guild, audio_data)

@bot.command()
async def pause(ctx):
    if ctx.voice_client:
        if ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("一時停止したのじゃ。")
        elif ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("再開するぞ。")

# ==========================================
# EVENTS (Voice)
# ==========================================
@bot.event
async def on_voice_state_update(member, before, after):
    # BOT自身がVCから切断された場合のクリーンアップ
    if member == member.guild.me and before.channel is not None and after.channel is None:
        state = get_guild_state(member.guild.id)
        vc = member.guild.voice_client
        if state["voice_chat_mode"]:
            if vc:
                stop_rolling_buffer(vc)
            else:
                # vcがすでにNoneの場合は状態だけリセット
                state["rolling_sink"] = None
                state["voice_buffer_active"] = False
        state["voice_chat_mode"] = False
        state["voice_last_triggered"] = None
        state["voice_last_audio_time"] = None
        state["active_channel_id"] = None
        state["is_playing_music"] = False
        if voice_chat_monitor_task.is_running():
            voice_chat_monitor_task.stop()
        return

    if member.bot: return
    if member.guild.voice_client is None: return
    bot_vc = member.guild.voice_client
    
    state = get_guild_state(member.guild.id)

    if after.channel == bot_vc.channel and before.channel != after.channel:
        if state["disconnect_task"] and not state["disconnect_task"].done():
            state["disconnect_task"].cancel()
        
        if state["active_channel_id"]:
            text_ch = bot.get_channel(state["active_channel_id"])
            if text_ch:
                greet_text = f"{member.display_name}、いらっしゃいなのじゃ。"
                await text_ch.send(greet_text)
                if not state["is_playing_music"]:
                    audio_data = await generate_wav(greet_text, SPEAKER_ID)
                    if audio_data: play_audio(member.guild, audio_data)

    if len(bot_vc.channel.members) == 1:
        if not state["disconnect_task"] or state["disconnect_task"].done():
            state["disconnect_task"] = bot.loop.create_task(delayed_disconnect(bot_vc))

async def delayed_disconnect(voice_client):
    try:
        await asyncio.sleep(60) 
        if len(voice_client.channel.members) == 1:
            state = get_guild_state(voice_client.guild.id)
            if state["voice_chat_mode"]:
                stop_rolling_buffer(voice_client)
                
            state["active_channel_id"] = None
            state["is_playing_music"] = False
            # 会話モード停止
            state["voice_chat_mode"] = False
            state["voice_last_triggered"] = None
            state["voice_last_audio_time"] = None
            if state["rolling_sink"]:
                state["rolling_sink"].clear()
            state["rolling_sink"] = None
            state["voice_buffer_active"] = False
            
            await voice_client.disconnect()
            print(f"👋 {voice_client.guild.name} から自動退出しました")
    except asyncio.CancelledError:
        pass

# ==========================================
# EVENTS (Message)
# ==========================================
@bot.event
async def on_message(message):
    # Bot自身の発言は最初に無視
    if message.author.bot: return
    
    await bot.process_commands(message)

    if message.guild is None: return
    
    state = get_guild_state(message.guild.id)

    if message.content == TRIGGER_LEAVE:
        if message.guild.voice_client:
            await message.channel.send("さらばじゃ。")
            # 会話モード停止
            if state["voice_chat_mode"]:
                stop_rolling_buffer(message.guild.voice_client)
            
            # 状態を確実にリセット
            state["voice_chat_mode"] = False
            state["voice_last_triggered"] = None
            state["voice_last_audio_time"] = None
            state["active_channel_id"] = None
            state["is_playing_music"] = False
            state["voice_buffer_active"] = False
            if state["rolling_sink"]:
                state["rolling_sink"].clear()
                state["rolling_sink"] = None
                
            await message.guild.voice_client.disconnect()
        return

    if message.guild.voice_client is None: return

    # ■ ダイス処理
    if message.content.startswith(TRIGGER_DICE):
        num_str = message.content.replace(TRIGGER_DICE, "").strip()
        num = int(num_str) if num_str.isdigit() else 100
        
        res, reaction = roll_dice(num)
        text = f"🔮 **{message.author.display_name}** の目は **【 {res} 】** じゃ！ 「{reaction}」"
        await message.channel.send(text)
        
        if not state["is_playing_music"]:
            audio_data = await generate_wav(f"{res}。{reaction}。", SPEAKER_ID)
            if audio_data: play_audio(message.guild, audio_data)
        return

    # ■ ダイス結果集計 (プロンプト更新・インデント修正済み)
    if message.content == TRIGGER_SUMMARY:
        async with message.channel.typing():
            try:
                result_text = await summarize_dice(message.channel)
                if not result_text:
                    await message.channel.send("直近10分間にダイスの記録はないのう。")
                    return
                await message.channel.send(result_text)
                
                if not state["is_playing_music"]:
                    # 最後の1行だけ読み上げ
                    lines = result_text.strip().splitlines()
                    last_line = lines[-1] if lines else "集計完了じゃ。"
                    audio_data = await generate_wav(last_line, SPEAKER_ID)
                    if audio_data: play_audio(message.guild, audio_data)
            except Exception as e:
                print(e)
                await message.channel.send("帳簿が開けぬ。")
        return

    if message.content.startswith(TRIGGER_CHAT):
        user_question = message.content.replace(TRIGGER_CHAT, "").strip()
        if not user_question: return
        
        if user_question == "ソーチョー":
            await message.channel.send("https://knt-a.com/fauxhollows/")
            if not state["is_playing_music"]:
                audio_data = await generate_wav("ソーチョー", SPEAKER_ID)
                if audio_data: play_audio(message.guild, audio_data)
            return

        if len(user_question) > 50:
            await message.channel.send("長い。短くせよ。")
            return
        async with message.channel.typing():
            try:
                use_search = any(k in user_question for k in SEARCH_KEYWORDS) or "教えて" in user_question
                target_config = config_search if use_search else config_normal
                history = [f"{msg.author.display_name}: {msg.content}" async for msg in message.channel.history(limit=15)]
                full_prompt = f"履歴：\n" + "\n".join(reversed(history)) + f"\n\n質問：{user_question}"
                response = await client.aio.models.generate_content(
                    model=MODEL_NAME, contents=full_prompt, config=target_config
                )
                log_token_usage(response, "Chat")
                ai_text = response.text
                await message.channel.send(ai_text)
                if not use_search and not state["is_playing_music"]:
                    audio_data = await generate_wav(ai_text, SPEAKER_ID)
                    if audio_data: play_audio(message.guild, audio_data)
            except Exception as e:
                print(f"Error: {e}")
                await message.channel.send("天界の網が乱れておるのう。")
        return

    if not message.content.startswith('!'):
        if not state["is_playing_music"]:
            user_speaker = get_user_speaker_id(str(message.author.id))
            audio_data = await generate_wav(message.content, user_speaker)
            if audio_data: play_audio(message.guild, audio_data)

# ==========================================
# BOT STARTUP
# ==========================================
async def main():
    global http_session
    http_session = aiohttp.ClientSession()
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await http_session.close()

asyncio.run(main())