"""
Predict.fun 고래 탐지 모듈

고래 감지 방식:
  - REST 폴링: GET /v1/orders/matches?minValueUsdtWei={wei}&first=50
  - 30초 간격, cursor 기반 페이지네이션으로 중복 방지
  - amountFilled >= MIN_WHALE_SIZE_USDT 거래 → 고래 후보 DB 등록

GraphQL 리더보드 발굴:
  - 6시간마다 pnlUsd 기준 상위 트레이더 자동 시드
  - 상위 20명 현재 포지션 조회 → 마켓별 중복 포지션 맵 생성
  - overlap_positions.json → 봇 베팅금 배율 조정에 활용
"""

import os
import json
import math
import time
import threading
import requests
from datetime import datetime, timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import config

DB_FILE       = os.path.join(config.DATA_DIR, "whales_predict.json")
SNAPSHOT_FILE = os.path.join(config.DATA_DIR, "leaderboard_snapshot.json")
OVERLAP_FILE  = os.path.join(config.DATA_DIR, "overlap_positions.json")
WEI           = 10 ** 18
GRAPHQL_URL   = "https://graphql.predict.fun/graphql"

# 스코어링 기준
MIN_WIN_RATE = config.MIN_WIN_RATE
MIN_TRADES   = config.MIN_TRADES

# ──────────────────────────────────────────────
# GraphQL 쿼리
# ──────────────────────────────────────────────

_LEADERBOARD_QUERY = """
query Leaderboard($cursor: String) {
  leaderboard(pagination: {first: 50, after: $cursor}) {
    edges {
      node {
        rank
        account {
          address
          name
          statistics {
            pnlUsd
            volumeUsd
            marketsCount
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

_MARKET_STATUS_QUERY = """
query($id: ID!) {
  market(id: $id) {
    id
    status
    resolution { name }
  }
}
"""

_POSITIONS_QUERY = """
query AccountPositions($address: Address!, $cursor: String) {
  account(address: $address) {
    positions(pagination: {first: 50, after: $cursor}, filter: {isResolved: false}) {
      edges {
        node {
          market {
            id
            question
          }
          outcome {
            name
            onChainId
          }
          shares
          averageBuyPriceUsd
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


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
# GraphQL 리더보드 발굴
# ──────────────────────────────────────────────

def fetch_graphql_leaderboard(max_pages: int = 20, min_pnl: float = 1000.0) -> list:
    """
    GraphQL leaderboard에서 pnlUsd 기준 상위 트레이더 목록 반환
    반환: [{"address", "name", "pnl", "vol", "markets"}, ...]  pnl 내림차순
    """
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json", "User-Agent": "PredictBot/1.0"})

    rows = []
    cursor = None
    page = 0

    while page < max_pages:
        variables = {"cursor": cursor} if cursor else {}
        try:
            resp = session.post(
                GRAPHQL_URL,
                json={"query": _LEADERBOARD_QUERY, "variables": variables},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            print(f"[WhaleMgr] GraphQL 리더보드 오류 (page {page}): {e}")
            break

        if "errors" in body:
            print(f"[WhaleMgr] GraphQL 오류: {body['errors'][0]['message']}")
            break

        lb = body.get("data", {}).get("leaderboard", {})
        edges = lb.get("edges", [])
        page_info = lb.get("pageInfo", {})

        for e in edges:
            n = e["node"]
            acc = n["account"]
            st = acc.get("statistics") or {}
            pnl = float(st.get("pnlUsd") or 0)
            vol = float(st.get("volumeUsd") or 0)
            if pnl < min_pnl:
                continue
            rows.append({
                "address": acc["address"].lower(),
                "name":    acc.get("name") or acc["address"][:10],
                "pnl":     pnl,
                "vol":     vol,
                "markets": int(st.get("marketsCount") or 0),
            })

        page += 1
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        time.sleep(0.3)

    rows.sort(key=lambda x: x["pnl"], reverse=True)
    print(f"[WhaleMgr] GraphQL 리더보드: {len(rows)}명 수집 (pnl >= ${min_pnl:.0f})")
    return rows


def save_leaderboard_snapshot(rows: list):
    """리더보드 스냅샷 저장 — whale_scorer delta 계산용 (6시간 주기)"""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    data = {"timestamp": int(time.time()), "rows": rows}
    tmp = SNAPSHOT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_FILE)
    except Exception as e:
        print(f"[WhaleMgr] 스냅샷 저장 실패: {e}")


_CATEGORY_KEYWORDS = {
    "sports":   ["nba", "nfl", "nhl", "mlb", "soccer", "football", "basketball",
                 "tennis", "cricket", "rugby", "golf", "f1", "racing", "ufc", "mma",
                 "champion", "league", "cup", "series", "match", "lakers", "celtics"],
    "crypto":   ["btc", "eth", "bitcoin", "ethereum", "crypto", "token", "defi",
                 "price", "fdv", "launch", "airdrop", "sol", "bnb", "usdt",
                 "exchange", "coinbase", "ipo"],
    "politics": ["election", "president", "senate", "congress", "vote", "political",
                 "democrat", "republican", "biden", "trump", "governor", "primary"],
}


def _classify_market(question: str) -> str:
    q = question.lower()
    for cat, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in q for k in keywords):
            return cat
    return "other"


def load_leaderboard_snapshot() -> dict:
    """이전 리더보드 스냅샷 로드"""
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def settle_pending_trades():
    """
    action=None인 BUY 거래들의 마켓 정산 결과를 GraphQL로 확인하여
    action(WIN/LOSS), pnl, category 갱신 + wins/losses/total_pnl 누적.
    leaderboard refresh와 동일한 6시간 주기로 호출.
    """
    db = load_whales_db()

    # market_id → [(addr, trade_idx), ...] 맵핑
    pending_map: dict = {}
    for addr, whale in db.items():
        for i, t in enumerate(whale.get("trades", [])):
            if t.get("side") == "BUY" and t.get("action") not in ("WIN", "LOSS"):
                mid = str(t.get("marketId") or t.get("market_id") or "")
                if mid:
                    pending_map.setdefault(mid, []).append((addr, i))

    if not pending_map:
        return

    total_pending = sum(len(v) for v in pending_map.values())
    print(f"[WhaleMgr] 정산 확인: {len(pending_map)}개 마켓 / {total_pending}건 대기")

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json", "User-Agent": "PredictBot/1.0"})

    settled = 0
    for market_id, entries in pending_map.items():
        try:
            r = session.post(
                GRAPHQL_URL,
                json={"query": _MARKET_STATUS_QUERY, "variables": {"id": market_id}},
                timeout=10,
            )
            market = (r.json().get("data") or {}).get("market") or {}
            if market.get("status") != "RESOLVED":
                continue
            resolution = market.get("resolution") or {}
            winning_outcome = (resolution.get("name") or "").strip().lower()
            if not winning_outcome:
                continue

            for addr, trade_idx in entries:
                try:
                    t = db[addr]["trades"][trade_idx]
                    outcome_name = (t.get("outcome_name") or "").strip().lower()
                    entry_price  = float(t.get("price") or t.get("entry_price") or 0)
                    size_usdt    = float(t.get("size_usdt") or 0)
                    if entry_price <= 0 or size_usdt <= 0:
                        continue

                    is_win = outcome_name == winning_outcome
                    pnl = round(
                        size_usdt * (1.0 / entry_price - 1.0) if is_win else -size_usdt, 4
                    )

                    t["action"]      = "WIN" if is_win else "LOSS"
                    t["pnl"]         = pnl
                    t["entry_price"] = entry_price
                    t["category"]    = _classify_market(t.get("question", ""))

                    w = db[addr]
                    if is_win:
                        w["wins"] = w.get("wins", 0) + 1
                    else:
                        w["losses"] = w.get("losses", 0) + 1
                    w["total_pnl"] = round(w.get("total_pnl", 0) + pnl, 4)
                    settled += 1
                except Exception as e:
                    print(f"[WhaleMgr][WARN] 정산 처리 실패 ({addr[:8]}, market={market_id}): {e}")

        except Exception as e:
            print(f"[WhaleMgr][WARN] 마켓 정산 조회 실패 (market={market_id}): {e}")
        time.sleep(0.2)

    if settled:
        save_whales_db(db)
    print(f"[WhaleMgr] 정산 완료: {settled}/{total_pending}건 처리")


def get_overlap_positions(top_addrs: list, session: requests.Session, top_n: int = 20) -> dict:
    """
    상위 N명 고래의 현재 포지션을 GraphQL로 조회 → 동일 마켓 보유 맵 반환
    반환: {market_id: [addr1, addr2, ...]}  (2명 이상 겹친 마켓만)
    """
    market_map: dict = {}  # market_id → [addrs]

    for addr in top_addrs[:top_n]:
        cursor = None
        while True:
            variables = {"address": addr}
            if cursor:
                variables["cursor"] = cursor
            try:
                resp = session.post(
                    GRAPHQL_URL,
                    json={"query": _POSITIONS_QUERY, "variables": variables},
                    timeout=15,
                )
                if resp.status_code != 200:
                    break
                body = resp.json()
                acc_data = (body.get("data") or {}).get("account") or {}
                pos_data = acc_data.get("positions") or {}
                edges = pos_data.get("edges", [])
                page_info = pos_data.get("pageInfo", {})

                for e in edges:
                    node = e.get("node", {})
                    mkt = node.get("market") or {}
                    market_id = str(mkt.get("id", ""))
                    if market_id:
                        market_map.setdefault(market_id, [])
                        if addr not in market_map[market_id]:
                            market_map[market_id].append(addr)

                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
            except Exception as e:
                print(f"[WhaleMgr] overlap 포지션 조회 실패 ({addr[:8]}...): {e}")
                break
        time.sleep(0.2)

    overlap = {mid: addrs for mid, addrs in market_map.items() if len(addrs) >= 2}
    print(f"[WhaleMgr] Overlap 감지: {len(overlap)}개 마켓에서 다중 고래 포지션")
    return overlap


def save_overlap_map(overlap: dict):
    """중복 포지션 맵 저장 — 봇 베팅금 배율 계산용"""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    data = {"timestamp": int(time.time()), "overlap": overlap}
    tmp = OVERLAP_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, OVERLAP_FILE)
    except Exception as e:
        print(f"[WhaleMgr] overlap 저장 실패: {e}")


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
                pass

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
            # 최근 50건만 보관 (BUY는 action=None으로 태깅 → 정산 후 WIN/LOSS 갱신)
            # [Fix] market 전체 객체 제외 — 파일 크기 폭증(23MB) 및 DB 손상 방지
            trade_entry = {
                "side":         trade.get("side"),
                "price":        trade.get("price"),
                "size_usdt":    trade.get("size_usdt"),
                "outcome_name": trade.get("outcome_name"),
                "token_id":     trade.get("token_id"),
                "question":     trade.get("question"),
                "marketId":     trade.get("marketId"),
                "timestamp":    trade.get("timestamp"),
            }
            if trade.get("side") == "BUY":
                trade_entry["action"] = None
            w.setdefault("trades", []).append(trade_entry)
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
    """메인 봇에서 호출 — WhaleWatcher 시작 + GraphQL 리더보드 주기 갱신"""
    print("[WhaleMgr] 고래 탐지 초기화 (REST 폴링 + GraphQL 리더보드)")

    def _leaderboard_refresh_loop():
        gql_session = requests.Session()
        gql_session.headers.update({"Content-Type": "application/json", "User-Agent": "PredictBot/1.0"})
        first_run = True
        while True:
            try:
                # 첫 실행은 즉시, 이후 6시간마다
                if not first_run:
                    time.sleep(6 * 3600)
                first_run = False

                rows = fetch_graphql_leaderboard(max_pages=20, min_pnl=2000.0)
                if not rows:
                    continue

                # 고래 DB 자동 시드 (상위 50명)
                db = load_whales_db()
                added = 0
                for r in rows[:50]:
                    addr = r["address"]
                    if addr not in db:
                        _pnl = round(r["pnl"], 2)
                        _vol = round(r["vol"], 2)
                        # Bootstrap 점수: leaderboard PNL 규모 기반 초기 점수
                        # (full 스코어링 전 cold-start 방지 — whale_scorer._bootstrap_score 동일 공식)
                        _impl_roi = (_pnl / _vol * 100) if _vol > 0 else 100.0
                        # [Fix] 상한 0.15 — 실데이터 스코어와 역전 방지 (whale_scorer._bootstrap_score 동일)
                        if _pnl >= 50000:   _bs = 0.15
                        elif _pnl >= 20000: _bs = 0.13
                        elif _pnl >= 10000: _bs = 0.11
                        elif _pnl >= 5000:  _bs = 0.09
                        else:               _bs = 0.07
                        if _vol > 0 and _impl_roi < 10.0:
                            _bs = round(_bs * 0.8, 4)

                        db[addr] = {
                            "address":         addr,
                            "name":            r["name"],
                            "total_trades":    r["markets"],
                            "wins":            0,
                            "losses":          0,
                            "total_pnl":       _pnl,
                            "total_volume":    _vol,
                            "score":           _bs,
                            "status":          "seeded",
                            "leaderboard_pnl": _pnl,
                            "last_seen":       int(time.time()),
                            "trades":          [],
                            "bootstrap":       True,
                        }
                        added += 1
                    else:
                        db[addr]["leaderboard_pnl"] = round(r["pnl"], 2)
                        db[addr]["total_volume"] = max(
                            db[addr].get("total_volume", 0), round(r["vol"], 2)
                        )
                if added:
                    save_whales_db(db)
                    print(f"[WhaleMgr] 리더보드 시드: {added}명 신규 추가")

                # 스냅샷 저장 (scorer delta 계산용)
                save_leaderboard_snapshot(rows)

                # pending BUY 거래 정산 확인 (RESOLVED 마켓 WIN/LOSS 갱신)
                settle_pending_trades()

                # 상위 20명 현재 포지션 → overlap 맵 갱신
                top_addrs = [r["address"] for r in rows[:20]]
                overlap = get_overlap_positions(top_addrs, gql_session)
                if overlap:
                    save_overlap_map(overlap)

            except Exception as e:
                print(f"[WhaleMgr] 리더보드 갱신 오류: {e}")

    threading.Thread(target=_leaderboard_refresh_loop, daemon=True).start()

    watcher = WhaleWatcher()
    watcher.start()
    return watcher
