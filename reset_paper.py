"""
PAPER 트레이딩 전체 초기화 스크립트

초기화 항목:
  1. state_Predict_PAPER.json — 뱅크롤/포지션/통계 리셋
  2. trade_history_PAPER.jsonl — 거래 기록 삭제
  3. whales_predict.json — 고래 스코어 0 초기화 (주소/거래량 이력은 보존)
  4. logs/market_debug.jsonl — 디버그 로그 삭제
"""

import os
import json
import sys

# 프로젝트 루트 기준 경로
ROOT     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")

STATE_FILE  = os.path.join(DATA_DIR, "state_Predict_PAPER.json")
TRADE_LOG   = os.path.join(DATA_DIR, "trade_history_PAPER.jsonl")
WHALES_FILE = os.path.join(DATA_DIR, "whales_predict.json")
DEBUG_LOG   = os.path.join(LOGS_DIR, "market_debug.jsonl")

# 초기 뱅크롤 (.env 우선, 없으면 3000)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass
INITIAL_BANKROLL = float(os.getenv("INITIAL_BANKROLL", "3000.0"))


def confirm(msg: str) -> bool:
    ans = input(f"{msg} [y/N] ").strip().lower()
    return ans == "y"


def reset_state():
    fresh = {
        "bankroll":      INITIAL_BANKROLL,
        "peak_bankroll": INITIAL_BANKROLL,
        "stats": {"total_bets": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "max_drawdown": 0.0},
        "positions": {},
        "seen_txs": [],
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(fresh, f, ensure_ascii=False, indent=2)
    print(f"  ✅ state 리셋 — 뱅크롤 ${INITIAL_BANKROLL:.2f}")


def reset_trade_log():
    open(TRADE_LOG, "w").close()
    print(f"  ✅ 거래 기록 삭제 ({TRADE_LOG})")


def reset_whale_scores():
    if not os.path.exists(WHALES_FILE):
        print("  ⚠️  whales_predict.json 없음 — 스킵")
        return
    with open(WHALES_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)

    reset_count = 0
    for addr, w in db.items():
        w["score"]        = 0.0
        w["wins"]         = 0
        w["losses"]       = 0
        w["total_pnl"]    = 0.0
        w["total_trades"] = 0
        w["trades"]       = []   # 거래 이력 초기화 (거래량/주소는 보존)
        reset_count += 1

    with open(WHALES_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 고래 스코어 초기화 — {reset_count}마리")


def reset_debug_log():
    if os.path.exists(DEBUG_LOG):
        open(DEBUG_LOG, "w").close()
        print(f"  ✅ 디버그 로그 삭제 ({DEBUG_LOG})")


if __name__ == "__main__":
    print("=" * 50)
    print("  PAPER 트레이딩 전체 초기화")
    print(f"  초기 뱅크롤: ${INITIAL_BANKROLL:.2f}")
    print("=" * 50)

    if not confirm("\n정말 초기화하겠습니까?"):
        print("취소.")
        sys.exit(0)

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

    reset_state()
    reset_trade_log()
    reset_whale_scores()
    reset_debug_log()

    print("\n🎉 초기화 완료. 봇을 재시작하시오.")
