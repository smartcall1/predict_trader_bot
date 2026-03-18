"""
Predict.fun 고래 탐지 모듈

고래 감지 방식:
  - REST 폴링: GET /v1/orders/matches?minValueUsdtWei={wei}&first=50
  - 30초 간격, cursor 기반 페이지네이션으로 중복 방지
  - amountFilled >= MIN_WHALE_SIZE_USDT 거래 → 고래 후보 DB 등록

WebSocket은 오더북만 지원 — 거래 이벤트 없음 (미사용)
리더보드 API 없음 (공식 확인) — 폴링 자동 탐지 방식만 사용
"""

import os
import json
import time
import threading
import requests
from datetime import datetime, timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import config

DB_FILE = os.path.join(config.DATA_DIR, "whales_predict.json")
WEI     = 10 ** 18

# 스코어링 기준
MIN_WIN_RATE = config.MIN_WIN_RATE
MIN_TRADES   = config.MIN_TRADES


# ──────────────────────────────────────────────
# DB 유틸리티
# ──────────────────────────────────────────────

def load_whales_db() -> dict:
    bak = DB_FILE + ".bak"
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[WhaleMgr][WARN] DB 손상: {e}. 백업 복구 시도...")
            try:
                os.replace(DB_FILE, DB_FILE + ".corrupted")
            except Exception:
                pass
    if os.path.exists(bak):
        try:
            with open(bak, "r", encoding="utf-8") as f:
                db = json.load(f)
            print(f"[WhaleMgr] .bak 에서 {len(db)}마리 복구")
            return db
        except Exception:
            pass
    return {}


def save_whales_db(db: dict):
    bak = DB_FILE + ".bak"
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                json.load(f)
            os.replace(DB_FILE, bak)
        except Exception:
            pass
    tmp = DB_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DB_FILE)
    except Exception as e:
        print(f"[WhaleMgr][ERR] DB 저장 실패: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


# ──────────────────────────────────────────────
# REST 폴링 기반 고래 탐지
# ──────────────────────────────────────────────

class WhaleWatcher:
    """
    GET /v1/orders/matches 30초 폴링으로 대형 거래 탐지
    amountFilled >= MIN_WHALE_SIZE_USDT → 고래 후보 DB 등록 + 메인 봇에 전달
    """

    def __init__(self, on_whale_trade_callback=None):
        self.on_whale_trade  = on_whale_trade_callback
        self._running        = False
        self._db_lock        = threading.Lock()
        self._recent_trades: list = []
        self._recent_lock    = threading.Lock()
        self._last_cursor    = None   # 페이지네이션 커서 (중복 방지)
        self._seen_order_ids: set = set()  # 추가 중복 방어
        self._warmup         = True   # 첫 폴링: dedup 등록만, 거래 처리 없음

        self._match_debug_count = 0  # maker/taker 방향 검증용 (처음 30건 저장)

        self._session = requests.Session()
        retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.headers.update({
            "User-Agent": "PredictBot/1.0",
            "Accept": "application/json",
            "x-api-key": config.PREDICT_API_KEY or "",
        })

    def start(self):
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        print(f"[WhaleMgr] 거래 폴링 시작 (최소 ${config.MIN_WHALE_SIZE_USDT:.0f} USDT)")

    def stop(self):
        self._running = False

    def pop_recent_trades(self) -> list:
        """메인 봇이 주기적으로 호출 — 최근 감지 거래 가져감"""
        with self._recent_lock:
            trades = self._recent_trades.copy()
            self._recent_trades.clear()
        return trades

    # ──────────────────────────────────────────────
    # 폴링 루프
    # ──────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                self._fetch_matches()
            except Exception as e:
                print(f"[WhaleMgr][ERR] 폴링 오류: {e}")
            time.sleep(30)

    def _fetch_matches(self):
        """
        GET /v1/orders/matches
          - minValueUsdtWei: MIN_WHALE_SIZE_USDT × 1e18
          - first: 50 (최근 50건)
          - after: 이전 cursor (없으면 최신부터)
        응답: {"cursor": "...", "data": [...]}
        """
        min_wei = str(int(config.MIN_WHALE_SIZE_USDT * WEI))
        params  = {"first": 50, "minValueUsdtWei": min_wei}
        # [Fix3] cursor 실제 전달 — 이미 본 거래 재처리 방지
        if self._last_cursor:
            params["after"] = self._last_cursor

        try:
            r = self._session.get(
                f"{config.PREDICT_API_BASE}/v1/orders/matches",
                params=params,
                timeout=15,
            )
            if r.status_code != 200:
                print(f"[WhaleMgr][WARN] /orders/matches {r.status_code}: {r.text[:100]}")
                return

            body   = r.json()
            items  = body.get("data", [])
            cursor = body.get("cursor")

            # 첫 폴링: dedup ID만 등록하고 처리는 스킵 (시작 직전 거래 복사 방지)
            if self._warmup:
                for item in reversed(items):
                    self._process_match(item, dry_run=True)
                self._warmup = False
                self._last_cursor = None  # cursor 미사용 — 매번 최신 50건 조회
                print(f"[WhaleMgr] 워밍업 완료 ({len(items)}건 스킵) — 다음 폴링부터 처리")
                return

            new_count = 0
            for item in reversed(items):  # 오래된 것부터 처리
                self._process_match(item)
                new_count += 1

            if new_count:
                print(f"[WhaleMgr] 대형 거래 {new_count}건 처리")

            # cursor 미사용 — 매번 최신 50건 조회 (_seen_order_ids dedup으로 중복 방지)
            # after=cursor 는 시간 역방향 페이지네이션이므로 새 거래를 놓침

        except Exception as e:
            print(f"[WhaleMgr][ERR] _fetch_matches: {e}")

    def _process_match(self, item: dict, dry_run: bool = False):
        """
        체결 데이터 파싱 → 고래 등록 + 메인 봇 전달
        응답 구조:
          amountFilled: str(wei)
          executedAt: ISO datetime
          makers: [{signer, price(wei), quoteType(Bid/Ask), outcome{name, onChainId}, amount(wei)}]
          market: {id, conditionId, feeRateBps, decimalPrecision, question, ...}
        """
        # 거래 크기 (wei → USDT)
        amount_wei  = int(item.get("amountFilled") or 0)
        size_usdt   = amount_wei / WEI
        if size_usdt < config.MIN_WHALE_SIZE_USDT:
            return

        # [디버그] 처음 30건 raw 응답 저장 — maker/taker 방향(quoteType) 검증용
        # logs/match_debug.jsonl 확인 후 side 로직 이상 없으면 이 블록 제거 가능
        if not dry_run and self._match_debug_count < 30:
            try:
                _dp = os.path.join(config.LOGS_DIR, "match_debug.jsonl")
                with open(_dp, "a", encoding="utf-8") as _f:
                    _f.write(json.dumps({"n": self._match_debug_count, "item": item},
                                        ensure_ascii=False, default=str) + "\n")
                self._match_debug_count += 1
            except Exception:
                pass

        makers = item.get("makers", [])
        if not makers:
            return

        maker       = makers[0]
        addr        = (maker.get("signer") or "").lower()
        price_wei   = int(maker.get("price") or 0)
        price       = price_wei / WEI
        quote_type  = maker.get("quoteType", "")   # "Bid"=BUY, "Ask"=SELL
        outcome_obj = maker.get("outcome", {})
        outcome_nm  = outcome_obj.get("name", "")
        token_id    = outcome_obj.get("onChainId", "")

        market      = item.get("market", {})
        market_id   = str(market.get("id", ""))
        question    = market.get("question") or market.get("title") or market_id

        executed_at = item.get("executedAt", "")
        try:
            ts = int(datetime.fromisoformat(executed_at.replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = int(time.time())

        # 중복 방어 — executedAt + addr + market_id 조합
        dedup_key = f"{executed_at}:{addr}:{market_id}"
        if dedup_key in self._seen_order_ids:
            return
        self._seen_order_ids.add(dedup_key)
        if len(self._seen_order_ids) > 10000:
            self._seen_order_ids = set(list(self._seen_order_ids)[-5000:])

        if dry_run:
            return  # 워밍업: 중복 등록만, 거래 처리/콜백 없음

        if not addr or not market_id:
            return

        side = "BUY" if quote_type == "Bid" else "SELL"

        trade_info = {
            "address":          addr,
            "marketId":         market_id,
            "side":             side,
            "price":            price,
            "size_usdt":        size_usdt,
            "outcome_name":     outcome_nm,
            "token_id":         token_id,
            "question":         question,
            "timestamp":        ts,
            "market":           market,
        }

        print(f"[WhaleMgr] Whale {addr[:8]}... | {side} {outcome_nm} | ${size_usdt:.0f} @ {price:.3f} | {question[:30]}")

        # 고래 DB 업데이트
        self._update_whale_db(addr, trade_info)

        # 메인 봇 버퍼
        with self._recent_lock:
            self._recent_trades.append(trade_info)
            if len(self._recent_trades) > 200:
                self._recent_trades = self._recent_trades[-200:]

        # 직접 콜백
        if self.on_whale_trade:
            try:
                self.on_whale_trade(trade_info)
            except Exception as e:
                print(f"[WhaleMgr][ERR] 콜백 실패: {e}")

    def _update_whale_db(self, address: str, trade: dict):
        with self._db_lock:
            db = load_whales_db()
            if address not in db:
                db[address] = {
                    "address":      address,
                    "name":         f"whale_{address[:6]}",
                    "total_trades": 0,
                    "wins":         0,
                    "losses":       0,
                    "total_pnl":    0.0,
                    "total_volume": 0.0,
                    "last_seen":    0,
                    "score":        0.0,
                    "trades":       [],
                }
            w = db[address]
            w["total_volume"] = w.get("total_volume", 0) + trade["size_usdt"]
            w["last_seen"]    = trade["timestamp"]
            w["total_trades"] = w.get("total_trades", 0) + 1
            # 최근 50건만 보관
            w.setdefault("trades", []).append(trade)
            w["trades"] = w["trades"][-50:]
            save_whales_db(db)


# ──────────────────────────────────────────────
# 포지션 조회 유틸리티 (공개 API)
# ──────────────────────────────────────────────

def get_whale_positions(address: str, session: requests.Session) -> list:
    """
    GET /v1/positions/{address} — 공개, 누구나 조회 가능
    특정 고래의 현재 포지션 확인용
    """
    try:
        r = session.get(
            f"{config.PREDICT_API_BASE}/v1/positions/{address}",
            params={"first": 50},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("data", data) if isinstance(data, dict) else data
    except Exception as e:
        print(f"[WhaleMgr][ERR] get_whale_positions({address[:8]}...): {e}")
    return []


# ──────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────

def run_manager(client) -> WhaleWatcher:
    """메인 봇에서 호출 — WhaleWatcher 시작"""
    print("[WhaleMgr] 고래 탐지 초기화 (REST 폴링 방식)")
    watcher = WhaleWatcher()
    watcher.start()
    return watcher
