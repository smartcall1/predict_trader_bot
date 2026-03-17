"""
Predict.fun 고래 스코어링

Polymarket whale_scorer.py에서 이식 — 로직 동일, API URL만 변경
"""

import json
import math
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from config import config

DB_FILE = os.path.join(config.DATA_DIR, "whales_predict.json")

WEIGHT_PROFIT   = 0.40
WEIGHT_WIN_RATE = 0.40
WEIGHT_FREQUENCY = 0.20


class WhaleScorer:
    def __init__(self):
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504, 520, 524])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Authorization": f"Bearer {config.PREDICT_API_KEY}",
        })
        self.db_file = DB_FILE

    def load_db(self) -> dict:
        bak = self.db_file + ".bak"
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"[Scorer][WARN] DB 손상: {e}")
                try:
                    os.replace(self.db_file, self.db_file + ".corrupted")
                except Exception:
                    pass
        if os.path.exists(bak):
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    db = json.load(f)
                print(f"[Scorer] .bak에서 {len(db)}마리 복구")
                return db
            except Exception:
                pass
        return {}

    def save_db(self, db: dict):
        bak = self.db_file + ".bak"
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    json.load(f)
                os.replace(self.db_file, bak)
            except Exception:
                pass
        tmp = self.db_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(db, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.db_file)
        except Exception as e:
            print(f"[Scorer][ERR] DB 저장 실패: {e}")

    def fetch_trader_history(self, address: str, days: int = 90) -> list:
        """
        특정 트레이더 거래 내역 조회 (최근 N일)
        TODO: 실제 엔드포인트/필드명 확인 필요
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            r = self.session.get(
                f"{config.PREDICT_API_BASE}/v1/traders/{address}/activity",
                params={"limit": 500},
                timeout=15,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            trades = data if isinstance(data, list) else data.get("activity", data.get("data", []))
            result = []
            for tx in trades:
                # 타임스탬프 파싱 (Polymarket과 동일 방식)
                ts_val = tx.get("timestamp") or tx.get("createdAt") or 0
                try:
                    if isinstance(ts_val, (int, float)):
                        ts = int(ts_val)
                        if ts > 1_000_000_000_000:
                            ts //= 1000
                        tx_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    else:
                        tx_dt = datetime.fromisoformat(str(ts_val).replace("Z", "+00:00"))
                except Exception:
                    continue
                if tx_dt >= cutoff:
                    result.append(tx)
            return result
        except Exception as e:
            print(f"[Scorer][ERR] fetch_trader_history({address[:8]}...) 실패: {e}")
            return []

    def score_whale(self, address: str, trades: list) -> dict | None:
        """
        거래 내역으로 고래 점수 계산
        Polymarket scorer와 동일 알고리즘
        """
        if len(trades) < config.MIN_TRADES:
            return None

        resolved = [t for t in trades if t.get("action") in ("WIN", "LOSS", "STOP_LOSS")]
        if len(resolved) < config.MIN_TRADES:
            # WebSocket으로 수집한 데이터는 action 필드 없을 수 있음 → 대안 로직
            resolved = trades

        wins   = sum(1 for t in resolved if t.get("pnl", 0) > 0 or t.get("action") == "WIN")
        losses = sum(1 for t in resolved if t.get("pnl", 0) < 0 or t.get("action") == "LOSS")
        total  = wins + losses
        if total == 0:
            return None

        win_rate = wins / total * 100
        if win_rate < config.MIN_WIN_RATE:
            return None

        total_pnl = sum(float(t.get("pnl", 0)) for t in resolved)
        total_invested = sum(float(t.get("size_usdt", t.get("size", 0))) for t in resolved)
        roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        if roi > 5000:  # 이상치 제거
            return None

        # 30일 내 거래 빈도
        cutoff_30 = time.time() - 30 * 86400
        recent_20 = [
            t for t in trades
            if (t.get("timestamp", 0) or 0) >= cutoff_30
        ]
        freq_score = min(len(recent_20) / 20, 1.0)

        # recency_penalty (Polymarket BUG FIX21과 동일)
        recency_penalty = 1.0 if len(recent_20) >= 20 else 0.8

        # 최종 점수
        profit_score  = min(max(roi / 100, 0), 1.0)
        wr_score      = (win_rate - 50) / 50 if win_rate > 50 else 0
        composite = (
            WEIGHT_PROFIT   * profit_score +
            WEIGHT_WIN_RATE * wr_score +
            WEIGHT_FREQUENCY * freq_score
        ) * recency_penalty

        return {
            "address": address,
            "score": round(composite, 4),
            "win_rate": round(win_rate, 1),
            "roi": round(roi, 1),
            "total_trades": len(trades),
            "resolved_trades": total,
            "wins": wins,
            "losses": losses,
            "total_pnl": round(total_pnl, 2),
            "recent_20_count": len(recent_20),
            "updated_at": int(time.time()),
        }

    def update_all(self):
        """DB 전체 고래 점수 갱신"""
        db = self.load_db()
        if not db:
            print("[Scorer] 고래 DB 비어있음 — WebSocket 탐지 대기 중")
            return

        updated = 0
        for addr, whale in db.items():
            trades = self.fetch_trader_history(addr)
            if not trades:
                # WebSocket으로 수집한 내부 trades 사용
                trades = whale.get("trades", [])
            result = self.score_whale(addr, trades)
            if result:
                whale.update(result)
                updated += 1

        self.save_db(db)
        print(f"[Scorer] {updated}/{len(db)} 고래 점수 갱신 완료")

    def get_active_whales(self, min_score: float = 0.3) -> list:
        """점수 기준 이상 활성 고래 목록 반환"""
        db = self.load_db()
        whales = [
            w for w in db.values()
            if w.get("score", 0) >= min_score
        ]
        return sorted(whales, key=lambda x: x.get("score", 0), reverse=True)
