import discord
from discord.ext import commands, tasks, voice_recv
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

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')

# モデル: Gemini 2.5 Flash Lite
MODEL_NAME = "gemini-2.5-flash-lite"

# 音量設定 (初期値)
TTS_VOLUME = 1.0      # 読み上げ
MUSIC_VOLUME = 0.2    # 音楽 (20%)

current_active_channel_id = None

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

# ① 通常会話用
config_normal = types.GenerateContentConfig(
    system_instruction="""
    あなたは「もち神さま」というFF14に精通した「幼き賢神」です。
    ・回答は必ず「1文のみ（40文字以内）」で行うこと。
    ・一人称「わし」、語尾は「～なのじゃ」「～のう」「～じゃぞ」。
    """,
    max_output_tokens=150, 
    temperature=0.7
)

# ② 検索用
tool_search = [types.Tool(google_search=types.GoogleSearch())]
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
    clean_text = text.replace("🔮", "").replace("**", "").replace("【", "").replace("】", "").replace("\n", "。")
    params = {'text': clean_text, 'speaker': speaker}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f'{VOICEVOX_URL}/audio_query', params=params) as resp:
                if resp.status != 200: return None
                query = await resp.json()
            async with session.post(f'{VOICEVOX_URL}/synthesis', params=params, json=query) as resp:
                if resp.status != 200: return None
                data = await resp.read()
                filename = f'voice_{uuid.uuid4()}.wav'
                with open(filename, mode='wb') as f: f.write(data)
                return filename
    except: return None

def play_audio(guild, filename):
    global is_playing_music
    if guild.voice_client is None or is_playing_music:
        try: os.remove(filename)
        except: pass
        return

    if guild.voice_client.is_playing():
        guild.voice_client.stop()

    source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(filename, executable='ffmpeg'), volume=TTS_VOLUME)
    
    def after_playing(error):
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except: pass

    guild.voice_client.play(source, after=after_playing)

# ==========================================
# TASKS
# ==========================================
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
            model=MODEL_NAME, contents="FF14の短い独り言（20文字以内）を。", config=config_normal
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
            model=MODEL_NAME, contents=prompt, config=config_normal
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
    print(f'【降臨】{bot.user} (Model: {MODEL_NAME})')
    if not random_monologue_task.is_running(): random_monologue_task.start()

# ==========================================
# COMMANDS
# ==========================================
@bot.command()
async def mjoin(ctx):
    global current_active_channel_id, MUSIC_VOLUME
    if ctx.author.voice:
        await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        current_active_channel_id = ctx.channel.id
        
        # ★追加: 接続時に音量を必ず20%にリセット
        MUSIC_VOLUME = 0.2
        
        if gohan_police_task.is_running():
            gohan_police_task.cancel()
        gohan_police_task.start()
        
        async with ctx.typing():
            try:
                response = await client.aio.models.generate_content(
                    model=MODEL_NAME, contents="参加時の短い挨拶（一言、20文字以内）を1つだけ生成せよ。", config=config_normal
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
            f"!play [URLまたはキーワード ]\n"
            f"!stop\n"
            f"!vol [音量0-80]\n"
            f"!もちもち (声で質問)"
        )
        
        await ctx.send(greet + info_msg)
        fn = await generate_wav(greet, SPEAKER_ID)
        if fn: play_audio(ctx.guild, fn)

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
        # Python 3.10+ 推奨 (get_running_loop)
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
        
        if 'entries' in data:
            data = data['entries'][0]
        
        filename = data['url']
        title = data.get('title', '不明な曲')
        
        if ctx.voice_client.is_playing(): ctx.voice_client.stop()
        
        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(filename, **ffmpeg_opts), volume=MUSIC_VOLUME)
        
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
async def pause(ctx):
    if ctx.voice_client:
        if ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("一時停止したのじゃ。")
        elif ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("再開するぞ。")

# ==========================================
# LISTEN COMMAND (音声認識)
# ==========================================
@bot.command(name='もちもち')
async def mochimochi_listen(ctx):
    """ユーザーの音声を録音し、Gemini APIで文字起こし→AI応答する"""
    global is_playing_music
    guild_id = ctx.guild.id

    # === 前提条件チェック ===
    if not ctx.author.voice:
        await ctx.send("ボイスチャンネルに入るのじゃ。")
        return

    vc = ctx.voice_client
    if vc is None:
        await ctx.send("先に `!mjoin` でわしを呼ぶのじゃ。")
        return

    # VoiceRecvClient かどうかチェック
    if not isinstance(vc, voice_recv.VoiceRecvClient):
        await ctx.send("音声受信に対応しておらぬ。`!mjoin` でわしを呼び直すのじゃ。")
        return

    # === クールダウンチェック ===
    now = time.time()
    last_used = listen_cooldowns.get(guild_id, 0)
    remaining = LISTEN_COOLDOWN - (now - last_used)
    if remaining > 0:
        await ctx.send(f"⏳ まだ耳が休まっておらぬ。あと **{int(remaining)}秒** 待つのじゃ。")
        return

    # === 同時録音セッション制限 ===
    if listening_sessions.get(guild_id, False):
        await ctx.send("🔴 今はすでに聞いておるぞ。少し待つのじゃ。")
        return

    # === 音楽再生中チェック ===
    if is_playing_music:
        await ctx.send("🎵 音楽が流れておるから聞き取れぬ。`!stop` してから試すのじゃ。")
        return

    listening_sessions[guild_id] = True
    listen_cooldowns[guild_id] = now

    target_user = ctx.author
    await ctx.send(f"👂 **{target_user.display_name}**、{LISTEN_DURATION}秒間聞いておるぞ。話すのじゃ！")

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
            await ctx.send("🔇 何も聞こえなかったのじゃ。マイクを確認せよ。")
            return

        # === Gemini APIで文字起こし ===
        async with ctx.typing():
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
                    await ctx.send("🔇 聞き取れなかったのじゃ。もう少しはっきり話すのじゃ。")
                    return

                # 文字起こし結果を表示
                await ctx.send(f"📝 **聞き取り結果**: {transcribed_text}")

                # === 文字起こし結果をGemini通常会話に送信 ===
                # 入力制限チェック
                if len(transcribed_text) > 100:
                    transcribed_text = transcribed_text[:100]

                use_search = any(k in transcribed_text for k in SEARCH_KEYWORDS) or "教えて" in transcribed_text
                target_config = config_search if use_search else config_normal

                history = [f"{msg.author.display_name}: {msg.content}" async for msg in ctx.channel.history(limit=15)]
                full_prompt = f"履歴：\n" + "\n".join(reversed(history)) + f"\n\n質問：{transcribed_text}"

                ai_response = await client.aio.models.generate_content(
                    model=MODEL_NAME, contents=full_prompt, config=target_config
                )
                log_token_usage(ai_response, "ListenChat")

                ai_text = ai_response.text
                await ctx.send(ai_text)

                # 読み上げ（検索結果でなければ）
                if not use_search and not is_playing_music:
                    fn = await generate_wav(ai_text, SPEAKER_ID)
                    if fn: play_audio(ctx.guild, fn)

            except Exception as e:
                print(f"Listen STT/Chat Error: {e}")
                await ctx.send("天界の耳が乱れておるのう。もう一度試すのじゃ。")

    except Exception as e:
        print(f"Listen Error: {e}")
        await ctx.send("録音に失敗したのじゃ。")
        # 安全に録音を停止
        try:
            if vc.is_listening():
                vc.stop_listening()
        except: pass

    finally:
        # 一時ファイルのクリーンアップ
        try:
            if os.path.exists(wav_filename):
                os.remove(wav_filename)
        except: pass
        listening_sessions[guild_id] = False

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
    except asyncio.CancelledError:
        pass

# ==========================================
# EVENTS (Message)
# ==========================================
@bot.event
async def on_message(message):
    global current_active_channel_id, is_playing_music
    
    # Bot自身の発言は最初に無視
    if message.author.bot: return
    
    await bot.process_commands(message)

    if message.content == TRIGGER_LEAVE:
        if message.guild.voice_client:
            await message.channel.send("さらばじゃ。")
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
                fn = await generate_wav("ソーチョーの答え合わせじゃな。", SPEAKER_ID)
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
            fn = await generate_wav(message.content, SPEAKER_ID)
            if fn: play_audio(message.guild, fn)

bot.run(DISCORD_TOKEN)