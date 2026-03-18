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

    def score_whale(self, address: str, trades: list, whale_info: dict = None) -> dict | None:
        """
        거래 내역으로 고래 점수 계산 (Polymarket 고급 로직 적용)
        - confidence 보정 (소샘플 비례 할인)
        - win_score 하한 65% 상향 (50%→65%)
        - ROI 로그 스케일 (200% 만점)
        - recency_penalty 4단계
        - activity_penalty (비활동 기간 기반)
        - 헷징/MM 필터 (고승률+저ROI 차단)
        """
        if len(trades) < config.MIN_TRADES:
            return None

        resolved = [t for t in trades if t.get("action") in ("WIN", "LOSS", "STOP_LOSS")]
        if len(resolved) < config.MIN_TRADES:
            resolved = trades

        wins   = sum(1 for t in resolved if t.get("pnl", 0) > 0 or t.get("action") == "WIN")
        losses = sum(1 for t in resolved if t.get("pnl", 0) < 0 or t.get("action") == "LOSS")
        total  = wins + losses
        if total == 0:
            return None

        win_rate = wins / total * 100

        total_pnl      = sum(float(t.get("pnl", 0)) for t in resolved)
        total_invested = sum(float(t.get("size_usdt", t.get("size", 0))) for t in resolved)
        roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        if roi > 5000:
            return None

        # 30일 내 거래 빈도 (월 50회 기준)
        cutoff_30     = time.time() - 30 * 86400
        recent_trades = [t for t in trades if (t.get("timestamp", 0) or 0) >= cutoff_30]
        freq_score    = min(len(recent_trades) / 50.0, 1.0)

        # confidence 보정: resolved 20건 미만 → 비례 할인 (소샘플 고래 점수 과대 방지)
        confidence = min(total / 20.0, 1.0)

        # win_score: 65% 이하 0점, 90% 이상 만점 (저승률 고래 차별화)
        win_score = max(0.0, min((win_rate - 65.0) / 25.0, 1.0))

        # ROI 로그 스케일 (200% 이상 만점, 소ROI 과대평가 방지)
        if roi <= 0:
            roi_score = 0.0
        else:
            roi_score = max(0.0, min(math.log1p(roi / 100.0) / math.log(3.0), 1.0))

        # recency_penalty 4단계: 최근 정산 거래 WIN율 기준
        recent_resolved = [
            t for t in resolved
            if (t.get("timestamp", 0) or 0) >= cutoff_30
        ]
        if len(recent_resolved) >= 10:
            recent_wr = (
                sum(1 for t in recent_resolved
                    if t.get("pnl", 0) > 0 or t.get("action") == "WIN")
                / len(recent_resolved) * 100
            )
            if recent_wr >= 85:
                recency_penalty = 1.0
            elif recent_wr >= 75:
                recency_penalty = 0.75
            elif recent_wr >= 65:
                recency_penalty = 0.5
            else:
                recency_penalty = 0.25
        else:
            recent_wr       = None
            recency_penalty = 0.8

        # activity_penalty: last_seen 기반 비활동 패널티
        last_seen = (whale_info or {}).get("last_seen", 0) or 0
        if last_seen:
            days_inactive = (time.time() - last_seen) / 86400
            if days_inactive <= 3:
                activity_penalty = 1.0
            elif days_inactive <= 7:
                activity_penalty = 0.9
            elif days_inactive <= 14:
                activity_penalty = 0.7
            else:
                activity_penalty = 0.5
        else:
            days_inactive    = None
            activity_penalty = 0.5

        raw_score = (
            WEIGHT_PROFIT    * roi_score +
            WEIGHT_WIN_RATE  * win_score +
            WEIGHT_FREQUENCY * freq_score
        )
        composite = raw_score * confidence * recency_penalty * activity_penalty

        # 헷징/MM 필터: 고승률 + 저ROI = 방향성 없는 베팅 → 차단
        if win_rate >= 60.0 and roi < 5.0 and total >= 20:
            print(f"[Scorer] 헷징/MM: WR={win_rate:.1f}% 고승률 ROI={roi:.1f}% 저수익 → score=0")
            composite = 0.0

        return {
            "address":          address,
            "score":            round(composite, 4),
            "win_rate":         round(win_rate, 1),
            "roi":              round(roi, 1),
            "total_trades":     len(trades),
            "resolved_trades":  total,
            "wins":             wins,
            "losses":           losses,
            "total_pnl":        round(total_pnl, 2),
            "recent_count":     len(recent_trades),
            "recent_wr":        round(recent_wr, 1) if recent_wr is not None else None,
            "confidence":       round(confidence, 2),
            "recency_penalty":  round(recency_penalty, 2),
            "activity_penalty": round(activity_penalty, 2),
            "updated_at":       int(time.time()),
        }

    def update_all(self):
        """DB 전체 고래 점수 갱신 + Pruning + status 자동판정"""
        db = self.load_db()
        if not db:
            print("[Scorer] 고래 DB 비어있음 — 탐지 대기 중")
            return

        now_ts = int(time.time())

        # ── Pruning: score<0.3 + 14일 이상 미활동 → DB 제거 ──────────────
        PRUNE_SCORE = 0.3
        PRUNE_AGE   = 14 * 86400
        prune_targets = [
            addr for addr, w in db.items()
            if (now_ts - (w.get("last_seen") or 0)) > PRUNE_AGE
            and (w.get("score") or 0) < PRUNE_SCORE
        ]
        for addr in prune_targets:
            del db[addr]
        if prune_targets:
            print(f"[Scorer] Pruning: {len(prune_targets)}마리 부진 고래 제거 → 잔여 {len(db)}마리")

        # ── 점수 갱신 ──────────────────────────────────────────────────────
        updated = 0
        for addr, whale in db.items():
            trades = self.fetch_trader_history(addr)
            if not trades:
                trades = whale.get("trades", [])
            result = self.score_whale(addr, trades, whale_info=whale)
            if result:
                whale.update(result)
                updated += 1
                # status 자동판정: score≥0.6→active, <0.45→inactive, 그레이존 유지
                score = result["score"]
                if score >= 0.6:
                    whale["status"] = "active"
                elif score < 0.45:
                    whale["status"] = "inactive"

        self.save_db(db)
        active = sum(1 for w in db.values() if w.get("status") == "active")
        print(f"[Scorer] {updated}/{len(db)} 고래 점수 갱신 | active={active}마리")

    def get_active_whales(self, min_score: float = 0.3) -> list:
        """점수 기준 이상 활성 고래 목록 반환"""
        db = self.load_db()
        whales = [
            w for w in db.values()
            if w.get("score", 0) >= min_score
        ]
        return sorted(whales, key=lambda x: x.get("score", 0), reverse=True)
