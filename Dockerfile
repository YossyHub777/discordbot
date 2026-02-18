FROM python:3.11-slim

# ログをバッファリングせずに即時出力する設定（超重要）
ENV PYTHONUNBUFFERED=1

# 音声処理とコンパイルに必要なシステムライブラリ
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopus-dev \
    libffi-dev \
    build-essential \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ライブラリのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコードのコピー
COPY . .

CMD ["python", "mochigami.py"]