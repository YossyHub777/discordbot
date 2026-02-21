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
USER_VOICES_FILE = "user_voices.json"
BOT_CONFIG_FILE = "bot_config.json"

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')

# モデル: Gemini 2.5 Flash Lite
MODEL_NAME = "gemini-2.5-flash-lite"

# 音量設定 (初期値)
TTS_VOLUME = 1.0      # 読み上げ
MUSIC_VOLUME = 0.2    # 音楽 (20%)

current_active_channel_id = None

# HTTPセッション（BOT起動時に初期化）
http_session: aiohttp.ClientSession = None

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

# 会話検知・自動相槌設定
voice_chat_mode = False           # 会話モードのオン・オフ
voice_rolling_buffer = []         # ローリングバッファ（60秒分）
voice_last_triggered = None       # 最後に発動した時刻
voice_last_audio_time = None      # 最後に音声を受信した時刻
voice_buffer_active = False       # バッファ録音中かどうか
VOICE_SILENT_SECONDS = 30         # 無音判定までの秒数
VOICE_BUFFER_SECONDS = 60         # バッファ保持時間（秒）
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

def log_token_usage(response, context="Unknown"):
    try:
        if response.usage_metadata:
            total = response.usage_metadata.total_token_count
            print(f"💰 [BILLING] Ctx:{context} | {MODEL_NAME} | Total: {total}")
    except: pass

# ==========================================
# VOICE CONFIG PERSISTENCE
# ==========================================
def load_user_voices():
    global user_voices
    try:
        with open(USER_VOICES_FILE, 'r', encoding='utf-8') as f:
            user_voices = json.load(f)
        print(f"🔊 ユーザーボイス設定を読み込みました ({len(user_voices)}件)")
    except:
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
    except:
        pass  # デフォルト値のまま

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

is_playing_music = False
disconnect_task = None

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
    except: return None

def play_audio(guild, audio_data: io.BytesIO):
    """io.BytesIOの音声データをVCでpipe再生する"""
    global is_playing_music
    if guild.voice_client is None or is_playing_music:
        return

    if guild.voice_client.is_playing():
        guild.voice_client.stop()

    source = discord.PCMVolumeTransformer(
        discord.FFmpegPCMAudio(audio_data, pipe=True, executable='ffmpeg'),
        volume=TTS_VOLUME
    )
    guild.voice_client.play(source)

# ==========================================
# ROLLING BUFFER SINK（会話検知用）
# ==========================================
class RollingBufferSink(voice_recv.AudioSink):
    """全ユーザーの音声をローリングバッファに蓄積するシンク"""
    def __init__(self, buffer_seconds=60):
        super().__init__()
        self.buffer_seconds = buffer_seconds
        self._buffer = []  # [(timestamp, pcm_bytes), ...]
        self._write_count = 0

    def wants_opus(self):
        return False

    def write(self, user, data):
        global voice_last_audio_time
        now = time.time()
        voice_last_audio_time = now
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
        """バッファ内の全PCMデータを結合してbytesとして返す"""
        if not self._buffer:
            return b''
        return b''.join(d for _, d in self._buffer)

    def clear(self):
        """明示的にバッファをクリアする（stop_rolling_bufferから呼ぶ用）"""
        self._buffer.clear()
        self._write_count = 0

# グローバルシンクインスタンス
rolling_sink = None

def start_rolling_buffer(vc):
    """ローリングバッファ録音を開始する"""
    global rolling_sink, voice_buffer_active, voice_last_audio_time
    if not isinstance(vc, voice_recv.VoiceRecvClient):
        print(f"⚠️ VCがVoiceRecvClientではない: {type(vc)}")
        return
    # 既にリスニング中なら何もしない
    try:
        if vc.is_listening():
            voice_buffer_active = True
            return
    except Exception as e:
        print(f"⚠️ is_listening()エラー: {e}")
    # 既存のシンクがあれば再利用（バッファを維持）
    if rolling_sink is None:
        rolling_sink = RollingBufferSink(VOICE_BUFFER_SECONDS)
        print("🎙️ 新規シンク作成")
    try:
        vc.listen(rolling_sink)
    except Exception as e:
        print(f"❌ vc.listen()失敗: {e}")
        return
    voice_buffer_active = True
    if voice_last_audio_time is None:
        voice_last_audio_time = time.time()
    print("🎙️ ローリングバッファ録音開始")

def stop_rolling_buffer(vc):
    """ローリングバッファ録音を停止する"""
    global rolling_sink, voice_buffer_active
    try:
        if vc and isinstance(vc, voice_recv.VoiceRecvClient) and vc.is_listening():
            vc.stop_listening()
    except:
        pass
    if rolling_sink:
        rolling_sink.clear()
    rolling_sink = None
    voice_buffer_active = False
    print("🎙️ ローリングバッファ録音停止")

# ==========================================
# TASKS
# ==========================================
@tasks.loop(seconds=5)
async def voice_chat_monitor_task():
    """会話検知・自動相槌のバックグラウンドタスク"""
    global voice_chat_mode, voice_rolling_buffer, voice_last_triggered
    global voice_last_audio_time, voice_buffer_active, rolling_sink

    if not voice_chat_mode:
        return

    if current_active_channel_id is None:
        return

    channel = bot.get_channel(current_active_channel_id)
    if not channel:
        return

    vc = channel.guild.voice_client
    if not vc or not vc.is_connected():
        return

    # VCに2人以上いるか確認（BOT含む）
    if len(vc.channel.members) < 2:
        if voice_buffer_active:
            stop_rolling_buffer(vc)
        return

    # 音楽再生中はスキップ
    if is_playing_music:
        return

    now = time.time()

    # === クールダウン処理 ===
    if voice_last_triggered is not None:
        elapsed_minutes = (now - voice_last_triggered) / 60.0

        if elapsed_minutes < VOICE_BUFFER_RESTART_MINUTES:
            # 0〜19分: バッファ停止
            if voice_buffer_active:
                stop_rolling_buffer(vc)
            return
        elif elapsed_minutes < VOICE_COOLDOWN_MINUTES:
            # 19〜20分: バッファ再開（クールダウン明けに備える）
            if not voice_buffer_active:
                start_rolling_buffer(vc)
            return
        # 20分以上: クールダウン終了、通常処理へ

    # === バッファ録音が未開始なら開始 ===
    if not voice_buffer_active:
        start_rolling_buffer(vc)
        return

    # === リスニングが停止していたら再開（BOT音声再生後に自動復帰） ===
    try:
        if not vc.is_listening():
            start_rolling_buffer(vc)
    except:
        pass

    # === 無音検知 ===
    if voice_last_audio_time is None:
        return

    silent_seconds = now - voice_last_audio_time
    if silent_seconds < VOICE_SILENT_SECONDS:
        return

    # === 30秒以上無音 → 相槌処理 ===
    print(f"🔇 {silent_seconds:.0f}秒間の無音を検知。相槌処理を開始...")

    # バッファからPCMデータを取得
    if rolling_sink is None or not rolling_sink._buffer:
        print("⚠️ バッファが空のため相槌をスキップ")
        voice_last_audio_time = now  # リセットして再検知
        return

    pcm_data = rolling_sink.get_audio_bytes()

    # バッファ停止 & クールダウン開始
    stop_rolling_buffer(vc)
    voice_last_triggered = now
    voice_last_audio_time = None

    if len(pcm_data) < 1000:
        print("⚠️ 音声データが少なすぎるためフォールバック")
        await _voice_chat_fallback(channel)
        return

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
    except Exception as e:
        print(f"⚠️ 会話検知STTエラー: {e}")
        await _voice_chat_fallback(channel)
        return

    # 文字起こし結果がない場合はフォールバック
    if not transcribed_text or "聞き取れなかった" in transcribed_text:
        print("🔇 文字起こし結果なし → フォールバック独り言")
        await _voice_chat_fallback(channel)
        return

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

        # 相槌用config（tool_search付き）
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

        ai_response = await client.aio.models.generate_content(
            model=MODEL_NAME, contents=prompt, config=config_aizuchi
        )
        log_token_usage(ai_response, "VoiceChatAizuchi")
        aizuchi_text = ai_response.text.strip()
    except Exception as e:
        print(f"⚠️ 相槌生成エラー: {e}")
        await _voice_chat_fallback(channel)
        return

    # === テキスト投稿 + VOICEVOX読み上げ ===
    try:
        await channel.send(f"💬 {aizuchi_text}")
        if not is_playing_music:
            fn = await generate_wav(aizuchi_text, SPEAKER_ID)
            if fn:
                play_audio(channel.guild, fn)
    except Exception as e:
        print(f"⚠️ 相槌送信エラー: {e}")


async def _voice_chat_fallback(channel):
    """文字起こし失敗時のフォールバック: FF14ネタのランダム独り言"""
    global is_playing_music
    try:
        response = await client.aio.models.generate_content(
            model=MODEL_NAME, contents="FF14の短い独り言（20文字以内）を。", config=config_monologue
        )
        log_token_usage(response, "VoiceChatFallback")
        text = response.text.strip()
        await channel.send(text)
        if not is_playing_music:
            fn = await generate_wav(text, SPEAKER_ID)
            if fn:
                play_audio(channel.guild, fn)
    except Exception as e:
        print(f"⚠️ フォールバック独り言エラー: {e}")


@tasks.loop(minutes=60)
async def random_monologue_task():
    global current_active_channel_id, is_playing_music
    await asyncio.sleep(random.randint(900, 3000))
    if current_active_channel_id is None: return
    channel = bot.get_channel(current_active_channel_id)
    if not channel: return
    vc = channel.guild.voice_client

    if not vc or not vc.is_connected(): return
    if len(vc.channel.members) == 1: return
    if is_playing_music or vc.is_playing(): return

    try:
        response = await client.aio.models.generate_content(
            model=MODEL_NAME, contents="FF14の短い独り言（20文字以内）を。", config=config_monologue
        )
        log_token_usage(response, "Monologue")
        text = response.text.strip()
        await channel.send(text)
        fn = await generate_wav(text, SPEAKER_ID)
        if fn: play_audio(channel.guild, fn)
    except: pass

@tasks.loop(minutes=30)
async def gohan_police_task():
    global current_active_channel_id, is_playing_music
    if current_active_channel_id is None: return
    channel = bot.get_channel(current_active_channel_id)
    if not channel: return
    vc = channel.guild.voice_client

    if not vc or not vc.is_connected(): return
    if len(vc.channel.members) == 1: return
    if is_playing_music or vc.is_playing(): return

    try:
        prompt = "FF14の高難易度レイドで『食事バフ』を忘れているプレイヤーに対し、VIT不足による即死やDPS低下を指摘する『強烈な皮肉』を20文字以内で。「ごはん警察」は禁止。"
        response = await client.aio.models.generate_content(
            model=MODEL_NAME, contents=prompt, config=config_monologue
        )
        log_token_usage(response, "GohanPolice")
        
        full_text = f"🚨 ごはん警察じゃ。{response.text.strip()}"
        await channel.send(full_text)
        fn = await generate_wav(full_text, SPEAKER_ID)
        if fn: play_audio(channel.guild, fn)
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
    
    # スラッシュコマンドの同期（グローバル＋ギルド即時反映）
    try:
        synced = await bot.tree.sync()
        print(f"📡 グローバル同期完了 ({len(synced)}個)")
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        print(f"📡 ギルド即時同期完了 ({len(bot.guilds)}サーバー)")
    except Exception as e:
        print(f"⚠️ スラッシュコマンド同期失敗: {e}")
    
    if not random_monologue_task.is_running(): random_monologue_task.start()

# ==========================================
# SLASH COMMANDS (マイボイス・もちボイス)
# ==========================================

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
                prev_btn = discord.ui.Button(label="◀ 前へ", style=discord.ButtonStyle.secondary, custom_id="prev_page")
                prev_btn.callback = self.prev_page
                self.add_item(prev_btn)
            if page < self.total_pages - 1:
                next_btn = discord.ui.Button(label="次へ ▶", style=discord.ButtonStyle.secondary, custom_id="next_page")
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
            await self._apply_voice(interaction, char_name, styles[0]['name'], styles[0]['id'])
        else:
            # スタイル選択ビューを表示
            view = StyleSelectView(self.mode, self.user_id, char_name, styles)
            await interaction.response.edit_message(
                content=f"🎤 **{char_name}** のスタイルを選ぶのじゃ：",
                view=view
            )
    
    async def _apply_voice(self, interaction: discord.Interaction, char_name: str, style_name: str, style_id: int):
        global SPEAKER_ID
        full_name = f"{char_name} / {style_name}"
        
        if self.mode == "myvoice":
            user_voices[str(self.user_id)] = {"speaker_id": style_id, "name": full_name}
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
            if guild and guild.voice_client and not is_playing_music:
                fn = await generate_wav("声を変えたのじゃ！", SPEAKER_ID)
                if fn:
                    play_audio(guild, fn)
    
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
        full_name = f"{self.char_name} / {style_name}"
        
        if self.mode == "myvoice":
            user_voices[str(self.user_id)] = {"speaker_id": style_id, "name": full_name}
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
            if guild and guild.voice_client and not is_playing_music:
                fn = await generate_wav("声を変えたのじゃ！", SPEAKER_ID)
                if fn:
                    play_audio(guild, fn)
    
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


@bot.tree.command(name="マイボイス", description="自分のチャット読み上げ声を設定するのじゃ")
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


@bot.tree.command(name="もちボイス", description="もち神さまの声を変更するのじゃ")
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


@bot.tree.command(name="デザートアルバム", description="デザートのアルバムを表示するのじゃ")
async def desert_album(interaction: discord.Interaction):
    msg = (
        "🎵 デザートのアルバムじゃ。聴くがよい。\n\n"
        "🏜️ **DESERT MEMBER SONG 2024**\n"
        "https://soundcloud.com/shouyu-mochi/sets/desert-theme-song/s-0y6FdI6ccI3?si=9a004c595feb46e7b67547a3ca0a1638&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing\n\n"
        "🎤 **DESERT MEMBER SONG 2025**\n"
        "https://soundcloud.com/shouyu-mochi/sets/desert-member-song-2025-test/s-klf6JFeRYpP?si=276edc9d114643028d7c334f07d9c1a7&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing"
    )
    await interaction.response.send_message(msg)


# ==========================================
# SLASH COMMANDS (会話検知)
# ==========================================

@bot.tree.command(name="会話オン", description="会話検知モードをオンにするのじゃ")
async def voice_chat_on(interaction: discord.Interaction):
    global voice_chat_mode, voice_last_audio_time
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_connected():
        await interaction.response.send_message("先に `!mjoin` でわしを呼ぶのじゃ。", ephemeral=True)
        return
    voice_chat_mode = True
    # バッファ録音を開始
    start_rolling_buffer(vc)
    await interaction.response.send_message(
        "👂 会話を聞き始めるのじゃ。\n"
        "※会話が30秒途切れると、もち神さまが相槌を打つのじゃ。"
    )
    # モニタータスクを開始
    if not voice_chat_monitor_task.is_running():
        voice_chat_monitor_task.start()


@bot.tree.command(name="会話オフ", description="会話検知モードをオフにするのじゃ")
async def voice_chat_off(interaction: discord.Interaction):
    global voice_chat_mode, voice_rolling_buffer, voice_last_triggered
    global voice_last_audio_time, voice_buffer_active
    vc = interaction.guild.voice_client
    voice_chat_mode = False
    voice_rolling_buffer = []
    voice_last_triggered = None
    voice_last_audio_time = None
    # バッファ録音を停止
    if vc:
        stop_rolling_buffer(vc)
    # モニタータスクを停止
    if voice_chat_monitor_task.is_running():
        voice_chat_monitor_task.cancel()
    await interaction.response.send_message("🔇 会話検知を止めるのじゃ。")


# ==========================================
# SLASH COMMANDS (もちもち)
# ==========================================


@bot.tree.command(name="もちもち", description="声で質問するのじゃ")
async def slash_mochimochi_listen(interaction: discord.Interaction):
    """ユーザーの音声を録音し、Gemini APIで文字起こし→AI応答する"""
    global is_playing_music
    guild_id = interaction.guild_id

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
    if is_playing_music:
        await interaction.response.send_message("🎵 音楽が流れておるから聞き取れぬ。`/stop` してから試すのじゃ。", ephemeral=True)
        return

    # 3秒以内にdeferで応答
    await interaction.response.defer()

    # 会話検知バッファとの競合を防ぐため一時停止
    was_buffer_active = voice_buffer_active
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

            ai_response = await client.aio.models.generate_content(
                model=MODEL_NAME, contents=full_prompt, config=target_config
            )
            log_token_usage(ai_response, "ListenChat")

            ai_text = ai_response.text
            await interaction.followup.send(ai_text)

            # 読み上げ（検索結果でなければ）
            if not use_search and not is_playing_music:
                fn = await generate_wav(ai_text, SPEAKER_ID)
                if fn: play_audio(interaction.guild, fn)

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
        except: pass

    finally:
        # 一時ファイルのクリーンアップ（録音用ファイルはディスクI/O必須）
        try:
            if os.path.exists(wav_filename):
                os.remove(wav_filename)
        except: pass
        listening_sessions[guild_id] = False
        # 会話検知バッファを復帰
        if was_buffer_active and voice_chat_mode:
            vc = interaction.guild.voice_client
            if vc and vc.is_connected():
                start_rolling_buffer(vc)


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
    if ctx.voice_client and ctx.voice_client.source and is_playing_music:
        ctx.voice_client.source.volume = MUSIC_VOLUME
    await ctx.send(f"🔊 音楽の音量を **{volume}%** に変更したぞ。")

@bot.command()
async def play(ctx, *, query: str):
    global is_playing_music
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
            global is_playing_music
            is_playing_music = False
            
        ctx.voice_client.play(source, after=after_playing)
        is_playing_music = True
        
        await msg.edit(content=f"🎵 **再生中**: {title} (音量: {int(MUSIC_VOLUME*100)}%)")
    except Exception as e:
        print(f"Play Error: {e}")
        await msg.edit(content="見つからなんだ、または再生できぬ。")
        is_playing_music = False

@bot.command()
async def stop(ctx):
    global is_playing_music
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        is_playing_music = False
        await ctx.send("止めたぞ。")
    else:
        await ctx.send("何も流れておらぬ。")

@bot.command()
async def mjoin(ctx):
    global current_active_channel_id, MUSIC_VOLUME, voice_chat_mode, voice_rolling_buffer, voice_last_triggered
    if ctx.author.voice:
        await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        current_active_channel_id = ctx.channel.id
        
        # ★追加: 接続時に音量を必ず20%にリセット
        MUSIC_VOLUME = 0.2
        
        # 会話モード初期化
        voice_chat_mode = False
        voice_rolling_buffer = []
        voice_last_triggered = None
        
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
            except: greet = "わしが来てやったぞ。"
        
        info_msg = (
            "\n\n"
            f"もちもち、[キーワード] (Gemini)\n"
            f"もちもち、ソーチョー\n"
            f"/dice [最大値]\n"
            f"/ダイス結果\n"
            f"/play [URLまたはキーワード]\n"
            f"/stop\n"
            f"/vol [音量0-80]\n"
            f"/もちもち (声で質問)\n"
            f"/もちボイス (もち神さまの声を変更)\n"
            f"/マイボイス (自分の読み上げ声を変更)\n"
            f"/デザートアルバム\n"
            f"/会話オン (会話が途切れたらもち神さまが相槌を打つ)\n"
            f"/会話オフ\n"
            f"もちもちさよなら"
        )
        
        await ctx.send(greet + info_msg)
        fn = await generate_wav(greet, SPEAKER_ID)
        if fn: play_audio(ctx.guild, fn)

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
    global current_active_channel_id, is_playing_music, disconnect_task

    if member.bot: return
    if member.guild.voice_client is None: return
    bot_vc = member.guild.voice_client

    if after.channel == bot_vc.channel and before.channel != after.channel:
        if disconnect_task and not disconnect_task.done():
            disconnect_task.cancel()
        
        if current_active_channel_id:
            text_ch = bot.get_channel(current_active_channel_id)
            if text_ch:
                greet_text = f"{member.display_name}、いらっしゃいなのじゃ。"
                await text_ch.send(greet_text)
                if not is_playing_music:
                    fn = await generate_wav(greet_text, SPEAKER_ID)
                    if fn: play_audio(member.guild, fn)

    if len(bot_vc.channel.members) == 1:
        if not disconnect_task or disconnect_task.done():
            disconnect_task = bot.loop.create_task(delayed_disconnect(bot_vc))

async def delayed_disconnect(voice_client):
    # global宣言を追加
    global current_active_channel_id, is_playing_music
    try:
        await asyncio.sleep(60) 
        if len(voice_client.channel.members) == 1:
            await voice_client.disconnect()
            current_active_channel_id = None # Noneを代入
            is_playing_music = False
            if gohan_police_task.is_running():
                gohan_police_task.cancel()
            # 会話モード停止
            if voice_chat_monitor_task.is_running():
                voice_chat_monitor_task.cancel()
    except asyncio.CancelledError:
        pass

# ==========================================
# EVENTS (Message)
# ==========================================
@bot.event
async def on_message(message):
    global current_active_channel_id, is_playing_music, voice_chat_mode, voice_rolling_buffer, voice_last_triggered, voice_last_audio_time
    
    # Bot自身の発言は最初に無視
    if message.author.bot: return
    
    await bot.process_commands(message)

    if message.content == TRIGGER_LEAVE:
        if message.guild.voice_client:
            await message.channel.send("さらばじゃ。")
            # 会話モード停止
            if voice_chat_mode:
                stop_rolling_buffer(message.guild.voice_client)
                voice_chat_mode = False
                voice_rolling_buffer = []
                voice_last_triggered = None
                voice_last_audio_time = None
                if voice_chat_monitor_task.is_running():
                    voice_chat_monitor_task.cancel()
            await message.guild.voice_client.disconnect()
            current_active_channel_id = None
            is_playing_music = False
            if gohan_police_task.is_running():
                gohan_police_task.cancel()
        return

    if message.guild.voice_client is None: return

    # ■ ダイス処理
    if message.content.startswith(TRIGGER_DICE):
        num_str = message.content.replace(TRIGGER_DICE, "").strip()
        num = int(num_str) if num_str.isdigit() else 100
        res = random.randint(1, num)
        
        low_words = ["床ペロ", "雑魚よのう", "寄生か？", "無能じゃ", "ゴミじゃの", "非力すぎ", "出直せ雑魚"]
        mid_words = ["普通じゃ", "及第点じゃ", "凡夫じゃの", "無難じゃ", "まあまあ", "安泰じゃ", "悪くない"]
        high_words = ["良いぞ", "高めじゃ", "期待大", "さすが", "運が良い", "追い風", "上出来"]
        super_words = ["天才じゃ", "凄まじい", "豪運のう", "驚きじゃ", "最高じゃ", "神引き", "震える"]

        if res <= 35: reaction = random.choice(low_words)
        elif 36 <= res <= 70: reaction = random.choice(mid_words)
        elif 71 <= res <= 90: reaction = random.choice(high_words)
        else: reaction = random.choice(super_words)

        text = f"🔮 **{message.author.display_name}** の目は **【 {res} 】** じゃ！ 「{reaction}」"
        await message.channel.send(text)
        
        if not is_playing_music:
            fn = await generate_wav(f"{res}。{reaction}。", SPEAKER_ID)
            if fn: play_audio(message.guild, fn)
        return

    # ■ ダイス結果集計 (プロンプト更新・インデント修正済み)
    if message.content == TRIGGER_SUMMARY:
        async with message.channel.typing():
            try:
                limit_time = discord.utils.utcnow() - timedelta(minutes=30)
                history_list = [f"{msg.author.display_name}: {msg.content}" async for msg in message.channel.history(limit=100, after=limit_time)]
                if not history_list:
                    await message.channel.send("直近30分間にダイスの記録はないのう。")
                    return
                
                history_newest_first = list(reversed(history_list))
                
                # 表組み禁止・皮肉プロンプト・箇条書き指定
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
                await message.channel.send(response.text)
                
                if not is_playing_music:
                    # 最後の1行だけ読み上げ
                    lines = response.text.strip().splitlines()
                    last_line = lines[-1] if lines else "集計完了じゃ。"
                    
                    fn = await generate_wav(last_line, SPEAKER_ID)
                    if fn: play_audio(message.guild, fn)
            except Exception as e:
                print(e)
                await message.channel.send("帳簿が開けぬ。")
        return

    if message.content.startswith(TRIGGER_CHAT):
        user_question = message.content.replace(TRIGGER_CHAT, "").strip()
        if not user_question: return
        
        if user_question == "ソーチョー":
            await message.channel.send("https://knt-a.com/fauxhollows/")
            if not is_playing_music:
                fn = await generate_wav("ソーチョー", SPEAKER_ID)
                if fn: play_audio(message.guild, fn)
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
                if not use_search and not is_playing_music:
                    fn = await generate_wav(ai_text, SPEAKER_ID)
                    if fn: play_audio(message.guild, fn)
            except Exception as e:
                print(f"Error: {e}")
                await message.channel.send("天界の網が乱れておるのう。")
        return

    if not message.content.startswith('!'):
        if not is_playing_music:
            user_speaker = get_user_speaker_id(str(message.author.id))
            fn = await generate_wav(message.content, user_speaker)
            if fn: play_audio(message.guild, fn)

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