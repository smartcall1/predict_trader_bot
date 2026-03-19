"""
Predict.fun 고래 스코어링 (GraphQL 기반 - 폴리마켓 봇 동일 로직)

핵심 변경:
- fetch_trader_history: REST /v1/traders/{addr}/activity (404) → GraphQL positions(isResolved=true)
- 카테고리별 승률 추적 (sports/crypto/politics/other)
- 스냅샷 델타 기반 최신 활동 고래 가중치
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

DB_FILE      = os.path.join(config.DATA_DIR, "whales_predict.json")
SNAPSHOT_FILE = os.path.join(config.DATA_DIR, "leaderboard_snapshot.json")
GRAPHQL_URL  = "https://graphql.predict.fun/graphql"

WEIGHT_PROFIT    = 0.40
WEIGHT_WIN_RATE  = 0.40
WEIGHT_FREQUENCY = 0.20

# 카테고리 키워드 매핑
CATEGORY_KEYWORDS = {
    "sports":   ["nba", "nfl", "nhl", "mlb", "soccer", "football", "basketball",
                 "tennis", "cricket", "rugby", "golf", "f1", "racing", "ufc", "mma",
                 "win", "champion", "league", "cup", "series", "match", "game",
                 "lakers", "celtics", "yankees", "warriors", "heat"],
    "crypto":   ["btc", "eth", "bitcoin", "ethereum", "crypto", "token", "defi",
                 "price", "market cap", "fdv", "launch", "airdrop", "blockchain",
                 "sol", "bnb", "usdt", "stablecoin", "exchange", "coinbase"],
    "politics": ["election", "president", "senate", "congress", "vote", "political",
                 "democrat", "republican", "biden", "trump", "governor", "primary"],
}


def classify_market(question: str) -> str:
    """마켓 질문 → 카테고리 분류"""
    q = question.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(k in q for k in keywords):
            return cat
    return "other"


class WhaleScorer:
    def __init__(self):
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504, 520, 524])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "PredictBot/1.0",
            "Accept": "application/json",
            "x-api-key": config.PREDICT_API_KEY or "",
        })
        self.db_file = DB_FILE

    # ──────────────────────────────────────────────
    # DB 관리
    # ──────────────────────────────────────────────

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

    # ──────────────────────────────────────────────
    # GraphQL 데이터 조회
    # ──────────────────────────────────────────────

    def fetch_resolved_positions(self, address: str) -> list:
        """
        GraphQL positions(isResolved=true) → 정산 완료 포지션 조회
        폴리마켓 activity?user={addr} + gamma outcomePrices 완전 대체

        반환 필드:
          action: "WIN" | "LOSS"
          pnl: float (pnlUsd)
          size_usdt: float (entry_price * shares)
          entry_price: float (averageBuyPriceUsd)
          question: str
          market_id: str
          category: str ("sports"/"crypto"/"politics"/"other")
          timestamp: int (정확한 ts 없음 → 현재 시각 근사)
        """
        query = """
        query($addr: Address!, $cursor: String) {
          account(address: $addr) {
            positions(filter: {isResolved: true}, pagination: {first: 100, after: $cursor}) {
              edges {
                node {
                  market { id question }
                  outcome { name }
                  shares
                  averageBuyPriceUsd
                  pnlUsd
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          }
        }
        """
        all_positions = []
        cursor = None

        for _ in range(5):  # 최대 5페이지 = 500건
            variables = {"addr": address}
            if cursor:
                variables["cursor"] = cursor
            try:
                r = self.session.post(
                    GRAPHQL_URL,
                    json={"query": query, "variables": variables},
                    timeout=15,
                )
                r.raise_for_status()
                body = r.json()
                if "errors" in body:
                    print(f"[Scorer][WARN] GraphQL 오류 ({address[:8]}): {body['errors'][0].get('message','')}")
                    break
                acc_data = (body.get("data") or {}).get("account")
                if not acc_data:
                    break
                pos_data  = acc_data.get("positions") or {}
                edges     = pos_data.get("edges", [])
                page_info = pos_data.get("pageInfo", {})

                for e in edges:
                    n = e["node"]
                    pnl_usd     = float(n.get("pnlUsd") or 0)
                    entry_price = float(n.get("averageBuyPriceUsd") or 0)
                    shares      = float(n.get("shares") or 0) / 1e18  # BigIntString → float
                    question    = (n.get("market") or {}).get("question") or ""

                    all_positions.append({
                        "market_id":    str((n.get("market") or {}).get("id", "")),
                        "question":     question,
                        "outcome_name": (n.get("outcome") or {}).get("name", ""),
                        "entry_price":  entry_price,
                        "shares":       shares,
                        "size_usdt":    entry_price * shares,
                        "pnl":          pnl_usd,
                        "action":       "WIN" if pnl_usd > 0 else "LOSS",
                        "category":     classify_market(question),
                        "timestamp":    int(time.time()),
                    })

                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
                time.sleep(0.3)

            except Exception as e:
                print(f"[Scorer][ERR] fetch_resolved_positions({address[:8]}): {e}")
                break

        return all_positions

    def fetch_account_stats(self, address: str) -> dict | None:
        """GraphQL account.statistics 조회 (전체 PnL/볼륨)"""
        query = """
        query($addr: String!) {
          account(address: $addr) {
            statistics { pnlUsd volumeUsd marketsCount }
          }
        }
        """
        try:
            r = self.session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": {"addr": address}},
                timeout=10,
            )
            r.raise_for_status()
            body = r.json()
            if "errors" in body or not body["data"]["account"]:
                return None
            return body["data"]["account"]["statistics"]
        except Exception:
            return None

    # ──────────────────────────────────────────────
    # 스코어 계산
    # ──────────────────────────────────────────────

    def score_whale(self, address: str, trades: list, whale_info: dict = None) -> dict | None:
        """
        정산 완료 포지션 목록 → 고래 점수 계산
        폴리마켓 whale_scorer.py calculate_score() 동일 로직

        trades: fetch_resolved_positions() 반환값
          - action: "WIN" | "LOSS"
          - pnl: float
          - size_usdt: float
          - category: str
          - timestamp: int
        """
        if len(trades) < config.MIN_TRADES:
            return None

        now_ts = time.time()
        cutoff_30 = now_ts - 30 * 86400

        # 30일 이내 거래만 (timestamp 근사 — 정확한 ts 없으므로 전체 사용)
        # 정산 완료 포지션이 누적이므로 전체 기준 사용 (폴리마켓 30일 대비 더 보수적)
        wins   = [t for t in trades if t.get("action") == "WIN"]
        losses = [t for t in trades if t.get("action") == "LOSS"]
        total  = len(wins) + len(losses)
        if total == 0:
            return None

        win_rate    = len(wins) / total * 100
        total_pnl   = sum(t.get("pnl", 0) for t in trades)
        total_inv   = sum(t.get("size_usdt", 0) for t in trades)
        roi         = (total_pnl / total_inv * 100) if total_inv > 0 else 0
        if roi > 5000:
            return None

        # 빈도 (최근 30일 = 전체 trades로 근사)
        freq_score = min(len(trades) / 50.0, 1.0)

        # 신뢰도 보정 (20건 미만 → 비례 할인)
        confidence = min(total / 20.0, 1.0)

        # 승률 점수 (65% 이하=0, 90% 이상=만점)
        win_score = max(0.0, min((win_rate - 65.0) / 25.0, 1.0))

        # ROI 로그 스케일 (200% = 만점)
        if roi <= 0:
            roi_score = 0.0
        else:
            roi_score = max(0.0, min(math.log1p(roi / 100.0) / math.log(3.0), 1.0))

        # recency_penalty (최근 20건 WR 기준)
        recent_20 = trades[-20:]
        if len(recent_20) >= 10:
            recent_wr = sum(1 for t in recent_20 if t.get("action") == "WIN") / len(recent_20) * 100
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

        # activity_penalty (last_seen 기반)
        last_seen = (whale_info or {}).get("last_seen", 0) or 0
        if last_seen:
            days_inactive = (now_ts - last_seen) / 86400
            if days_inactive <= 7:
                activity_penalty = 1.0
            elif days_inactive <= 14:
                activity_penalty = 0.8
            elif days_inactive <= 21:
                activity_penalty = 0.65
            else:
                activity_penalty = 0.5
        else:
            days_inactive    = None
            activity_penalty = 0.5

        # 스냅샷 델타 보정 (최근 6시간 PnL 상승 = 활발히 수익내는 고래)
        delta_bonus = self._compute_delta_bonus(address, whale_info)

        raw_score = (
            WEIGHT_PROFIT    * roi_score +
            WEIGHT_WIN_RATE  * win_score +
            WEIGHT_FREQUENCY * freq_score
        )
        composite = raw_score * confidence * recency_penalty * activity_penalty * (1.0 + delta_bonus)

        # 헷징/MM 필터 (고승률 + 저ROI → 방향성 없는 베팅)
        if win_rate >= 60.0 and roi < 5.0 and total >= 20:
            print(f"[Scorer] 헷징/MM: WR={win_rate:.1f}% ROI={roi:.1f}% → score=0")
            composite = 0.0

        # ── 카테고리별 승률 집계 ──────────────────────────────
        category_stats = {}
        for cat in ("sports", "crypto", "politics", "other"):
            cat_trades = [t for t in trades if t.get("category") == cat]
            if cat_trades:
                cat_wins = sum(1 for t in cat_trades if t.get("action") == "WIN")
                cat_pnl  = sum(t.get("pnl", 0) for t in cat_trades)
                cat_inv  = sum(t.get("size_usdt", 0) for t in cat_trades)
                category_stats[cat] = {
                    "trades":   len(cat_trades),
                    "wins":     cat_wins,
                    "win_rate": round(cat_wins / len(cat_trades) * 100, 1),
                    "roi":      round(cat_pnl / cat_inv * 100, 1) if cat_inv > 0 else 0,
                }

        return {
            "address":          address,
            "score":            round(composite, 4),
            "win_rate":         round(win_rate, 1),
            "roi":              round(roi, 1),
            "total_trades":     len(trades),
            "resolved_trades":  total,
            "wins":             len(wins),
            "losses":           len(losses),
            "total_pnl":        round(total_pnl, 2),
            "recent_wr":        round(recent_wr, 1) if recent_wr is not None else None,
            "confidence":       round(confidence, 2),
            "recency_penalty":  round(recency_penalty, 2),
            "activity_penalty": round(activity_penalty, 2),
            "delta_bonus":      round(delta_bonus, 3),
            "category_stats":   category_stats,
            "updated_at":       int(now_ts),
        }

    def _compute_delta_bonus(self, address: str, whale_info: dict) -> float:
        """
        스냅샷 델타 보너스: 최근 6시간 PnL 증가량 기반 활동 가중치
        최대 +0.3 (30% 보너스) — 최근 수익내는 고래 우선 추적
        """
        try:
            if not os.path.exists(SNAPSHOT_FILE):
                return 0.0
            with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
                snap = json.load(f)
            prev = snap.get(address)
            if not prev:
                return 0.0
            prev_pnl = prev.get("pnlUsd", 0)
            curr_pnl = (whale_info or {}).get("leaderboard_pnl", 0)
            snap_age = time.time() - prev.get("ts", 0)
            if snap_age <= 0 or snap_age > 24 * 3600:
                return 0.0
            delta_per_hour = (curr_pnl - prev_pnl) / (snap_age / 3600)
            # 시간당 $100 이상 수익 시 보너스
            if delta_per_hour >= 500:
                return 0.30
            elif delta_per_hour >= 200:
                return 0.20
            elif delta_per_hour >= 100:
                return 0.10
            elif delta_per_hour >= 0:
                return 0.0
            else:
                return -0.10  # 최근 손실 중 → 소폭 감점
        except Exception:
            return 0.0

    # ──────────────────────────────────────────────
    # 전체 갱신
    # ──────────────────────────────────────────────

    def update_all(self):
        """DB 전체 고래 점수 갱신 + Pruning + status 판정"""
        db = self.load_db()
        if not db:
            print("[Scorer] 고래 DB 비어있음 — 탐지 대기 중")
            return

        now_ts = int(time.time())

        # Pruning: score < 0.3 AND 14일+ 미활동
        prune_targets = [
            addr for addr, w in db.items()
            if (now_ts - (w.get("last_seen") or 0)) > 14 * 86400
            and (w.get("score") or 0) < 0.3
        ]
        for addr in prune_targets:
            del db[addr]
        if prune_targets:
            print(f"[Scorer] Pruning: {len(prune_targets)}마리 제거 → 잔여 {len(db)}마리")

        # 점수 갱신
        updated = 0
        for addr, whale in db.items():
            # GraphQL resolved positions 조회
            trades = self.fetch_resolved_positions(addr)
            if not trades:
                # fallback: DB 저장 trades
                trades = whale.get("trades", [])
            result = self.score_whale(addr, trades, whale_info=whale)
            if result:
                whale.update(result)
                updated += 1
                # status 판정
                score = result["score"]
                if score >= 0.6:
                    whale["status"] = "active"
                elif score < 0.45:
                    whale["status"] = "inactive"
                # 카테고리 통계 업데이트
                if result.get("category_stats"):
                    whale["category_stats"] = result["category_stats"]

            time.sleep(0.5)  # API rate limit

        self.save_db(db)
        active = sum(1 for w in db.values() if w.get("status") == "active")
        print(f"[Scorer] {updated}/{len(db)} 고래 점수 갱신 | active={active}마리")

    def get_active_whales(self, min_score: float = 0.2) -> list:
        """점수 기준 이상 활성 고래 목록"""
        db = self.load_db()
        return sorted(
            [w for w in db.values() if w.get("score", 0) >= min_score],
            key=lambda x: x.get("score", 0),
            reverse=True,
        )
