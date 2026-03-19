"""
Predict.fun 봇 설정

핵심 차이 (Polymarket 대비):
- 체인: BNB Chain (chain_id=56)
- 토큰: USDT (wei, 1e18)
- SDK: predict-sdk
"""

import os
from dotenv import load_dotenv

# core/ 안에서 실행해도 프로젝트 루트의 .env 를 찾도록 명시
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"))


class Config:
    # === Predict.fun API ===
    PREDICT_API_KEY = os.getenv("PREDICT_API_KEY")
    PREDICT_API_BASE = os.getenv("PREDICT_API_BASE", "https://api.predict.fun")
    PREDICT_WS_URL = os.getenv("PREDICT_WS_URL", "wss://ws.predict.fun/ws")

    # === BNB Chain 지갑 ===
    PRIVATE_KEY = os.getenv("PRIVATE_KEY")        # 개인키 (0x...)
    WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")  # 지갑 주소 (0x...)

    # BNB Chain RPC (여러 개 fallback)
    BSC_RPC_URLS = [
        os.getenv("BSC_RPC_URL", "https://bsc-dataseed1.binance.org"),
        "https://bsc-dataseed2.binance.org",
        "https://bsc-dataseed3.binance.org",
        "https://bsc.publicnode.com",
    ]

    # === 기본 운영 설정 ===
    INITIAL_BANKROLL = float(os.getenv("INITIAL_BANKROLL", "1000.0"))  # USDT 기준

    # === 고래봇 설정 ===
    MAX_POSITIONS       = int(os.getenv("MAX_POSITIONS", "30"))        # [Fix4] 20→30
    BET_DIVISOR         = int(os.getenv("BET_DIVISOR", "40"))         # [Fix4] 20→40 (포트폴리오 2.5% 베팅)
    STOP_LOSS_PCT       = float(os.getenv("STOP_LOSS_PCT", "0.40"))   # 40% 손절
    MIN_WHALE_SIZE_USDT = float(os.getenv("MIN_WHALE_SIZE_USDT", "200.0"))  # 고래 최소 거래액 (PAPER: 200, LIVE: 500 권장)
    MIN_PRICE           = float(os.getenv("MIN_PRICE", "0.10"))        # 극초저확률 차단
    MAX_PRICE           = float(os.getenv("MAX_PRICE", "0.85"))        # 정산 직전 차단 (0.85 초과 = 이미 한쪽 확정 가능성)
    DEFAULT_SLIPPAGE_BPS = int(os.getenv("DEFAULT_SLIPPAGE_BPS", "300"))  # 슬리피지 bps (300 = 3%)

    # 고래 스코어링 최소 기준
    MIN_WIN_RATE = float(os.getenv("MIN_WIN_RATE", "55.0"))
    MIN_TRADES   = int(os.getenv("MIN_TRADES", "5"))

    # === 시스템 ===
    PAPER_TRADING = os.getenv("PAPER_TRADING", "True").lower() == "true"
    DEBUG_MODE    = os.getenv("DEBUG_MODE", "True").lower() == "true"

    # === Telegram 알림 ===
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

    # === 경로 설정 ===
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    LOGS_DIR = os.path.join(BASE_DIR, "logs")

    @classmethod
    def ensure_dirs(cls):
        for d in [cls.DATA_DIR, cls.LOGS_DIR]:
            os.makedirs(d, exist_ok=True)


config = Config()
