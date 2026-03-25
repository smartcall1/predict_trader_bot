"""
Predict.fun 고래 카피 트레이딩 봇

Polymarket whale_copy_bot.py에서 이식
핵심 변경:
1. PolymarketClient → PredictFunClient
2. data-api 폴링 → WebSocket 실시간 감지 (whale_manager.WhaleWatcher)
3. USDC → USDT (wei 단위)
4. Polygon → BNB Chain
"""

import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

import time
import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone

from config import config
from client_wrapper import PredictFunClient
from whale_manager import run_manager, load_whales_db, OVERLAP_FILE
from whale_scorer import WhaleScorer
from telegram_notifier import notifier as tg_notifier


class PredictCopyBot:
    def __init__(self):
        config.ensure_dirs()

        # 상태
        self.seen_txs: OrderedDict = OrderedDict()
        self._seen_market_signals: dict = {}   # 중복 진입 방어 (conditionId+side+addr → ts)
        self.positions: dict = {}
        self._position_lock = threading.Lock()
        self.MAX_POSITIONS = config.MAX_POSITIONS
        self._last_save_time = 0
        self._active_whale_count = 0
        self._pending_confirm = None
        self.pending_orders: list = []          # 지정가 대기 큐 (1분 관찰)
        self._void_price_counter: dict = {}     # VOID 마켓 감지 (가격 고착 카운터)

        # 자산
        self.bankroll = config.INITIAL_BANKROLL
        self.peak_bankroll = self.bankroll
        self.stats = {
            "total_bets": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "max_drawdown": 0.0,
            # 섀도우(반대편) 포트폴리오: 고래와 반대로 배팅했을 때의 가상 성과
            "shadow_wins": 0, "shadow_losses": 0, "shadow_pnl": 0.0,
        }

        suffix = "_PAPER" if config.PAPER_TRADING else "_LIVE"
        self.trade_log_path  = os.path.join(config.DATA_DIR, f"trade_history{suffix}.jsonl")
        self.state_file_path = os.path.join(config.DATA_DIR, f"state_Predict{suffix}.json")

        self._startup_time = int(time.time())
        self._load_state()

        self.client  = PredictFunClient()
        self.scorer  = WhaleScorer()

        # 시작 시 고래 DB 카운트 초기화 (score >= 0.05 기준 — bootstrap 0.07~0.15 포함)
        try:
            db = load_whales_db()
            self._active_whale_count = sum(1 for w in db.values() if w.get("score", 0) >= 0.05)
        except Exception:
            pass

        # WebSocket 고래 탐지 시작
        self.watcher = run_manager(self.client)

        # 백그라운드 스레드
        threading.Thread(target=self._maintenance_loop, daemon=True).start()
        threading.Thread(target=self._telegram_poll_loop, daemon=True).start()

        mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        if config.CONTRARIAN_MODE:
            mode += " CONTRARIAN"
        print(f"[Bot] PredictCopyBot 시작 ({mode}) — 초기 뱅크롤 ${self.bankroll:.2f}")
        tg_notifier.send_message(
            f"🚀 <b>Predict.fun 봇 시작 [{mode}]</b>\n"
            f"💵 뱅크롤: ${self.bankroll:.2f}\n"
            f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            reply_markup=self._tg_keyboard(),
        )

    # ──────────────────────────────────────────────
    # Contrarian 유틸리티
    # ──────────────────────────────────────────────

    def _resolve_contrarian_outcome(self, market: dict, original_outcome_name: str) -> tuple:
        """반대 outcome의 (name, token_id) 반환. 바이너리 마켓(2 outcomes) 전용."""
        outcomes = market.get("outcomes", [])
        if len(outcomes) != 2:
            print(f"[Bot][WARN] 비바이너리 마켓 ({len(outcomes)} outcomes) → Contrarian 스킵")
            return (original_outcome_name, "")
        for o in outcomes:
            if o.get("name", "").upper() != original_outcome_name.upper():
                return (o.get("name", ""), o.get("onChainId", ""))
        return (original_outcome_name, "")

    # ──────────────────────────────────────────────
    # 메인 루프
    # ──────────────────────────────────────────────

    def run(self):
        print("[Bot] 메인 루프 시작 (WebSocket 이벤트 폴링)")
        while True:
            try:
                # WebSocket 감지된 고래 거래 처리
                whale_trades = self.watcher.pop_recent_trades()
                for trade in whale_trades:
                    self._handle_whale_trade(trade)

                # 대기열 주문 처리 (매 사이클)
                self._process_pending_orders()

                # 포지션 정산 체크 (60초마다)
                if int(time.time()) % 60 < 5:
                    self._settle_positions()

                time.sleep(2)
            except KeyboardInterrupt:
                print("[Bot] 종료 신호 수신")
                break
            except Exception as e:
                print(f"[Bot][ERR] 메인 루프 오류: {e}")
                time.sleep(5)

    # ──────────────────────────────────────────────
    # 고래 거래 처리
    # ──────────────────────────────────────────────

    def _handle_whale_trade(self, trade: dict):
        """WebSocket으로 감지된 고래 거래 → 복사 매매 판단"""
        addr         = trade.get("address", "")
        market_id    = trade.get("marketId", "")
        side         = trade.get("side", "")
        price        = float(trade.get("price") or 0)
        size_usdt    = float(trade.get("size_usdt") or 0)
        tx_hash      = trade.get("transactionHash", "")
        ts           = trade.get("timestamp", int(time.time()))
        outcome_name = trade.get("outcome_name", "")   # "Yes"/"No"/"Lakers" 등
        token_id     = trade.get("token_id", "")       # 온체인 토큰 ID

        # ── 중복 방어 ──
        if tx_hash and tx_hash in self.seen_txs:
            return
        if tx_hash:
            self.seen_txs[tx_hash] = ts
            if len(self.seen_txs) > 5000:
                for _ in range(500):
                    self.seen_txs.popitem(last=False)

        # ── 백로그 차단 (5분 이상 된 거래 무시) ──
        if time.time() - ts > 300:
            return

        # ── Filter 1: BUY만 복사 (SELL은 MIRROR EXIT 체크) ──
        side_upper = str(side).upper()
        if side_upper in ("SELL", "1"):
            self._handle_mirror_exit(addr, market_id, price)
            return
        if side_upper not in ("BUY", "0", "YES"):
            return

        # ── Filter 2: 포지션 한도 ──
        if len(self.positions) >= self.MAX_POSITIONS:
            return

        # ── Filter 3: 가격 범위 ──
        _filter_price = (1.0 - price) if config.CONTRARIAN_MODE else price
        if _filter_price < config.MIN_PRICE or _filter_price > config.MAX_PRICE:
            return

        # ── Filter 3.5: 0.50~0.60 구간 차단 (수익성 불명확 구간) ──
        if 0.50 <= _filter_price <= 0.60:
            print(f"[Bot] [SKIP] 0.50~0.60 가격 구간 차단: {_filter_price:.3f}")
            return

        # ── Filter 4: 고래 스코어 확인 ──
        db = load_whales_db()
        whale_info = db.get(addr)
        if whale_info:
            score = whale_info.get("score", 0)
            is_bootstrap = whale_info.get("bootstrap", False)
            lb_pnl = whale_info.get("leaderboard_pnl", 0) or 0
            vol = whale_info.get("total_volume", 0) or 0

            if is_bootstrap:
                # bootstrap 고래: 리더보드 PnL 기준으로 판단 (score 기준 아님)
                # lb_pnl $5000 이상만 허용 — 검증 안 된 고래 최소 필터
                if lb_pnl < 5000:
                    print(f"[Bot] [SKIP] bootstrap 고래 lb_pnl 미달 (${lb_pnl:.0f}): {addr[:8]}")
                    return
            else:
                # 실데이터 고래: score 기준 (데이터 축적 단계 → 0.07 이상)
                if score > 0 and score < 0.07:
                    return
                # 미평가 고래(score=0): vol >= $2K 이상만 허용
                if score == 0 and lb_pnl <= 0 and vol < 2000:
                    print(f"[Bot] [SKIP] 미평가 고래 차단 (vol=${vol:.0f}): {addr[:8]}")
                    return
        else:
            # DB에 없는 완전 신규 고래 → $1000 이상 거래만 허용
            if size_usdt < 1000:
                print(f"[Bot] [SKIP] 신규 고래 최소 크기 미달 (${size_usdt:.0f}): {addr[:8]}")
                return

        # ── Filter 5: 동일 마켓 중복 진입 차단 ──
        # [Fix1-A] 이미 같은 마켓 포지션 보유 중이면 고래 무관하게 차단
        if any(p.get("marketId") == market_id for p in self.positions.values()):
            print(f"[Bot] [SKIP] 동일 마켓 중복 차단: {market_id[:12]}...")
            return

        # [Fix1-B] addr 제외한 마켓+방향 키로 15분 TTL 중복 방어 (PAPER: 900s, LIVE: 1800s 권장)
        sig_key = f"{market_id}:{side_upper}"
        now = int(time.time())
        if now - self._seen_market_signals.get(sig_key, 0) < 900:
            return
        self._seen_market_signals[sig_key] = now
        if len(self._seen_market_signals) > 500:
            cutoff = now - 1800
            self._seen_market_signals = {
                k: v for k, v in self._seen_market_signals.items() if v > cutoff
            }

        # ── 마켓 검증 ──
        market = self.client.get_market(market_id)
        if not market:
            return

        # [Fix2-DEBUG] 마켓 필드 구조 확인용 — logs/market_debug.jsonl 에 누적 저장
        try:
            import json as _json
            _debug_path = os.path.join(config.LOGS_DIR, "market_debug.jsonl")
            _fields = {k: v for k, v in market.items() if k not in ("outcomes",)}
            with open(_debug_path, "a", encoding="utf-8") as _df:
                _df.write(_json.dumps({"market_id": market_id, "fields": _fields}, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

        if self._is_resolved(market):
            print(f"[Bot] [SKIP] 정산 완료 마켓: {market_id} (status={market.get('status')})")
            return

        # ── Filter 5-F: FDV/토큰가치 마켓 차단 ──
        _question_text = (
            market.get("question") or market.get("title") or
            market.get("name") or market.get("description") or ""
        ).lower()
        _FDV_KEYWORDS = ["fdv", "market cap", "fully diluted", "mcap", "token launch",
                         "listing price", "token price"]
        if any(kw in _question_text for kw in _FDV_KEYWORDS):
            print(f"[Bot] [SKIP] FDV/토큰가치 마켓 차단: {_question_text[:50]}")
            return

        # ── Filter 5-G: 만기 불명 / 60일 초과 장기 마켓 차단 ──
        # boostEndsAt=null → 비부스트 마켓 = 장기/투기성 마켓 패턴 (market_debug.jsonl 검증 완료)
        _boost_ends = market.get("boostEndsAt")
        if not _boost_ends:
            print(f"[Bot] [SKIP] 비부스트 마켓 차단 (boostEndsAt=null): {_question_text[:50]}")
            return
        try:
            _end_dt = datetime.fromisoformat(str(_boost_ends).replace("Z", "+00:00"))
            _days_left = (_end_dt - datetime.now(timezone.utc)).total_seconds() / 86400
            if _days_left > 60:
                print(f"[Bot] [SKIP] 장기 마켓 차단 ({_days_left:.0f}일 남음): {_question_text[:40]}")
                return
        except Exception:
            pass  # 파싱 실패 시 통과 (방어적)

        # ── Contrarian Mode: outcome 반전 ──
        if config.CONTRARIAN_MODE:
            _orig_outcome = outcome_name
            outcome_name, token_id = self._resolve_contrarian_outcome(market, outcome_name)
            if outcome_name == _orig_outcome:
                print(f"[Bot] [SKIP] Contrarian 반전 불가: {market_id[:12]}...")
                return
            print(f"[Bot] [CONTRARIAN] 🔄 {_orig_outcome} → {outcome_name} 반전")

        # ── Filter 6: 스프레드 과다 차단 (>15%) + 가격 이탈 방어 ──
        ob = self.client.get_orderbook(market_id)
        spread = self._get_orderbook_spread(ob)
        if spread is not None and spread > 0.15:
            print(f"[Bot] [SKIP] 스프레드 {spread:.1%} 과다: {market_id[:12]}...")
            return
        # 현재 오더북 ask vs 고래 거래가 25% 이상 이탈 → stale trade 차단
        # No/Down 결과물은 "1 - best_bid"가 실제 진입가이므로 bids 기준으로 계산
        _is_no_outcome = outcome_name.upper() in ("NO", "DOWN", "BELOW", "UNDER")
        if ob:
            ref_levels = ob.get("bids", []) if _is_no_outcome else ob.get("asks", [])
            if ref_levels:
                lv = ref_levels[0]
                ref_price = float(lv[0]) if isinstance(lv, (list, tuple)) else float(lv.get("price") or lv.get("p") or 0)
                raw_ask = max(1.0 - ref_price, 0.01) if _is_no_outcome else ref_price
                if raw_ask > 0:
                    if raw_ask > config.MAX_PRICE:
                        print(f"[Bot] [SKIP] 현재 ask {raw_ask:.3f} > MAX_PRICE: {market_id[:12]}...")
                        return
                    drift = abs(raw_ask - price) / max(price, 0.01)
                    if drift > 0.25:
                        print(f"[Bot] [SKIP] 가격 이탈 {drift:.0%} (고래:{price:.3f} 현재ask:{raw_ask:.3f}): {market_id[:12]}...")
                        return

        # ── 베팅금 계산 ──
        portfolio = self._get_portfolio()
        _base_bet = portfolio / config.BET_DIVISOR
        # 스코어 가중치: 고래 점수에 비례하여 배팅금 조절
        _score_val = (whale_info or {}).get("score", 0)
        _weight = max(0.2, min(_score_val * 2, 1.0))  # score 0~1 → weight 0.2~1.0
        # 역배 제한: entry_price < 0.35일 때 (price/0.35)² 곡선 적용
        _bet_price = (1.0 - price) if config.CONTRARIAN_MODE else price
        _underdog_factor = min(1.0, (_bet_price / 0.35) ** 2) if _bet_price < 0.35 else 1.0
        bet_size = _base_bet * _weight * _underdog_factor
        if bet_size > self.bankroll:
            bet_size = self.bankroll * 0.9
        if bet_size < 1.0:
            return

        whale_name = (whale_info or {}).get("name", addr[:8])

        # ── Overlap 배율: 0.4 반감 수렴 공식 (최대 ≈×1.667) ──
        overlap_count = self._get_overlap_count(market_id)
        if overlap_count >= 2:
            bonus = sum(0.4 ** i for i in range(1, overlap_count))
            multiplier = 1.0 + bonus
            bet_size = min(bet_size * multiplier, self.bankroll * 0.9)
            print(f"[Bot] [OVERLAP {overlap_count}명] 배율 {multiplier:.3f}x 적용: ${bet_size:.2f}")

        # ── Pending 중복 등록 방지 ──
        _already_pending = any(
            o.get("market_id") == market_id and o.get("outcome_name") == outcome_name
            for o in self.pending_orders
        )
        if _already_pending:
            print(f"[Bot] [SKIP] 동일 마켓 이미 대기열에 등록됨 (중복 pending 차단)")
            return

        # ── 대기열 등록 (1분 관찰, 목표가 = 고래가 + 슬리피지) ──
        target_price = min(0.99, price * (1 + config.DEFAULT_SLIPPAGE_BPS / 10_000))
        _ud_str = f" ×역배{_underdog_factor:.0%}" if _underdog_factor < 1.0 else ""
        _wt_str = f" ×점수{_weight:.0%}" if _weight < 1.0 else ""
        print(f"⏳ [PENDING] 🐋 {whale_name} 픽 감지 -> 대기열 등록 (1분 관찰, 목표가 ${target_price:.3f}, 배팅 ${bet_size:.2f}{_ud_str}{_wt_str})")
        self.pending_orders.append({
            "trade": trade,
            "market": market,
            "market_id": market_id,
            "whale_name": whale_name,
            "whale_addr": addr,
            "whale_price": price,
            "target_price": target_price,
            "bet_size": bet_size,
            "outcome_name": outcome_name,
            "token_id": token_id,
            "expires_at": time.time() + 60,
        })

    def _handle_mirror_exit(self, addr: str, market_id: str, price: float):
        """고래 SELL 감지 → 동일 마켓 동방향 포지션 조기 청산 (이익/본전일 때만)"""
        for pos_key, pos in list(self.positions.items()):
            if pos.get("marketId") != market_id:
                continue
            current = pos.get("current_price", pos["entry_price"])
            if current < pos["entry_price"]:
                print(f"[Bot] [MIRROR HOLD] 손실 중 — 자연정산 대기 ({pos_key[:12]}...)")
                continue
            print(f"[Bot] [MIRROR EXIT] 고래 SELL 감지 → 청산: {pos_key[:12]}...")
            self._execute_sell(pos_key, pos, current, reason="MIRROR_EXIT")

    # ──────────────────────────────────────────────
    # Overlap 배율 계산
    # ──────────────────────────────────────────────

    def _load_overlap_map(self) -> dict:
        """overlap_positions.json 로드 (1시간 TTL)"""
        now = int(time.time())
        cached = getattr(self, "_overlap_cache", None)
        if cached and now - cached.get("loaded_at", 0) < 3600:
            return cached.get("data", {})
        try:
            if os.path.exists(OVERLAP_FILE):
                with open(OVERLAP_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # 파일 자체가 6시간 이상 오래되면 무시
                if now - raw.get("timestamp", 0) < 6 * 3600:
                    self._overlap_cache = {"loaded_at": now, "data": raw.get("overlap", {})}
                    return self._overlap_cache["data"]
        except Exception:
            pass
        self._overlap_cache = {"loaded_at": now, "data": {}}
        return {}

    def _get_overlap_count(self, market_id: str) -> int:
        """해당 마켓에 포지션 보유 중인 상위 고래 수 반환"""
        overlap = self._load_overlap_map()
        return len(overlap.get(str(market_id), []))

    # ──────────────────────────────────────────────
    # 주문 실행
    # ──────────────────────────────────────────────

    def _process_pending_orders(self):
        """대기열 주문 처리: 매 사이클마다 best_ask 확인, 목표가 이하 시 체결"""
        if not self.pending_orders:
            return

        now = time.time()
        active_orders = []

        for order in self.pending_orders:
            # 만료 체크
            if now > order["expires_at"]:
                print(f"⏰ [EXPIRED] {order['whale_name']} 픽 체결 실패 (시장가가 목표가 ${order['target_price']:.3f} 이내로 오지 않음)")
                continue

            market_id = order["market_id"]
            outcome_name = order.get("outcome_name", "")

            # 현재 best_ask 조회
            _is_no = outcome_name.upper() in ("NO", "DOWN", "BELOW", "UNDER")
            if _is_no:
                yes_bid = self.client.get_best_price(market_id, side="SELL")
                current_ask = round(1.0 - yes_bid, 4) if yes_bid is not None else None
            else:
                current_ask = self.client.get_best_price(market_id, side="BUY")

            if current_ask is not None and current_ask <= order["target_price"]:
                print(f"✅ [PENDING Filled] 🐋 {order['whale_name']} 픽 체결! (ask: ${current_ask:.3f} <= 목표가 ${order['target_price']:.3f})")
                self._execute_trade(
                    market_id, order["market"], order["whale_price"],
                    order["bet_size"], order["whale_addr"], order["whale_name"],
                    outcome_name, order.get("token_id", ""),
                )
            else:
                remain = int(order["expires_at"] - now)
                ask_str = f"${current_ask:.3f}" if current_ask is not None else "None(유동성없음)"
                print(f"⏳ [PENDING Wait] {order['whale_name']} 대기중 (ask={ask_str} > 목표가 ${order['target_price']:.3f}, 잔여 {remain}s)")
                active_orders.append(order)

        self.pending_orders = active_orders

    def _execute_trade(self, market_id: str, market: dict, price: float,
                        bet_size: float, whale_addr: str, whale_name: str,
                        outcome_name: str = "", token_id: str = ""):
        """BUY 주문 실행"""
        result = self.client.place_order(
            market_id=market_id,
            side=0,  # BUY
            price=price,
            size_usdt=bet_size,
            slippage_bps=0,  # pending 대기열에서 이미 목표가 필터링 완료 → 이중 슬리피지 방지
            outcome_name=outcome_name,
            token_id=token_id,
        )
        if not result:
            print(f"[Bot][WARN] 주문 실패: {market_id[:12]}...")
            return

        # TODO: 응답 status 필드명 확인 ('matched'/'filled'/'live')
        status = result.get("status", "")
        if status not in ("matched", "filled", "paper"):
            order_id = result.get("orderId") or result.get("id") or ""
            if order_id:
                self.client.cancel_order(order_id)
            print(f"[Bot][WARN] 주문 미체결 (status={status}) → 취소")
            return

        # 실제 체결가 사용 (paper 시 오더북+슬리피지+수수료 반영된 가격)
        exec_price = float(result.get("price") or price)
        net_size   = float(result.get("size") or bet_size)  # 수수료 차감 후 실투자액
        shares = net_size / exec_price if exec_price > 0 else 0
        pos_key = f"{market_id}_{whale_addr[:8]}_{int(time.time())}"

        market_name = (
            market.get("question") or market.get("title") or
            market.get("name") or market.get("description") or
            market.get("marketQuestion") or str(market_id)
        )

        with self._position_lock:
            # Double-Check: Lock 획득 후 동일 마켓 중복 재확인 (WebSocket race condition 방어)
            if any(p.get("marketId") == market_id for p in self.positions.values()):
                print(f"[Bot] [SKIP] Lock 내 중복 감지 → 체결 취소: {market_id[:12]}...")
                self.bankroll += bet_size  # 이미 차감된 경우 복구
                return
            self.positions[pos_key] = {
                "marketId":     market_id,
                "whale_addr":   whale_addr,
                "whale_name":   whale_name,
                "entry_price":  exec_price,
                "current_price": exec_price,
                "size_usdc":    bet_size,
                "shares":       shares,
                "opened_at":    int(time.time()),
                "question":     market_name,
                "outcome_name": outcome_name,
                "token_id":     token_id,
                "end_date":     market.get("boostEndsAt"),
            }

        self.bankroll -= bet_size
        self.stats["total_bets"] += 1
        self._save_state()
        self._log_trade("OPEN", pos_key, price, 0)

        fee_paid = result.get("fee", 0)
        price_diff = exec_price - price
        slip_info = f" (고래:{price:.3f}→체결:{exec_price:.3f})" if abs(price_diff) > 0.001 else ""
        tg_notifier.send_message(
            f"🟢 <b>진입 [{('PAPER' if config.PAPER_TRADING else 'LIVE')}]</b>\n"
            f"🐳 {whale_name}\n"
            f"📊 {market_name[:40]}\n"
            f"💵 ${bet_size:.2f} @ {exec_price:.3f}{slip_info}\n"
            f"💸 수수료: ${fee_paid:.2f}\n"
            f"📌 포지션: {len(self.positions)}개"
        )

    def _execute_sell(self, pos_key: str, pos: dict, sell_price: float, reason: str = "WIN"):
        """포지션 청산"""
        shares = pos.get("shares", 0)

        result = self.client.place_order(
            market_id=pos["marketId"],
            side=1,  # SELL
            price=sell_price,
            size_usdt=shares * sell_price,
            outcome_name=pos.get("outcome_name", ""),
            token_id=pos.get("token_id", ""),
        )

        # PnL 계산
        if reason == "WIN" and sell_price >= 0.99:
            # 정산 승리: shares × $1 − 원금 (수수료 포함 정확한 수익)
            pnl = shares * 1.0 - pos["size_usdc"]
        elif reason == "LOSS" and sell_price <= 0.01:
            # 정산 패배: 전액 손실
            pnl = -pos["size_usdc"]
        elif result and config.PAPER_TRADING:
            # 페이퍼: 실제 오더북 bid − 슬리피지 가격 + 매도 수수료 반영
            actual_sell = float(result.get("price") or sell_price)
            sell_fee    = float(result.get("fee") or 0)
            pnl         = (actual_sell - pos["entry_price"]) * shares - sell_fee
            sell_price  = actual_sell  # 로그/텔레그램용
        else:
            pnl = (sell_price - pos["entry_price"]) * shares

        recovered = pos["size_usdc"] + pnl
        self.bankroll += recovered
        self.stats["total_pnl"] += pnl
        if pnl >= 0:
            self.stats["wins"] += 1
        else:
            self.stats["losses"] += 1

        # ── 섀도우(반대편) PnL 계산 ──
        # 동일 금액을 반대 outcome(1 - entry_price)에 투자했을 때의 가상 수익
        _entry = pos["entry_price"]
        _size  = pos["size_usdc"]
        _shadow_entry = 1.0 - _entry
        if _shadow_entry > 0.01:  # 0에 가까우면 계산 무의미
            _shadow_shares = _size / _shadow_entry
            _shadow_sell   = 1.0 - sell_price  # 반대편 가격
            _shadow_pnl    = _shadow_shares * _shadow_sell - _size
            self.stats.setdefault("shadow_pnl", 0.0)
            self.stats.setdefault("shadow_wins", 0)
            self.stats.setdefault("shadow_losses", 0)
            self.stats["shadow_pnl"] += _shadow_pnl
            if _shadow_pnl >= 0:
                self.stats["shadow_wins"] += 1
            else:
                self.stats["shadow_losses"] += 1

        with self._position_lock:
            self.positions.pop(pos_key, None)

        self._save_state()
        self._log_trade(reason, pos_key, sell_price, pnl, pos)
        self._record_whale_result(pos, pnl, reason)

        icon = "✅" if pnl >= 0 else "❌"
        tg_notifier.send_message(
            f"{icon} <b>{reason}</b>\n"
            f"📊 {pos.get('question', pos_key)[:40]}\n"
            f"💵 PnL: ${pnl:+.2f} | 잔고: ${self.bankroll:.2f}"
        )

    def _record_whale_result(self, pos: dict, pnl: float, reason: str):
        """포지션 결과를 고래 DB에 역기록 → 스코어 계산 활성화"""
        from whale_manager import load_whales_db, save_whales_db
        addr = pos.get("whale_addr", "")
        if not addr:
            return
        try:
            db = load_whales_db()
            if addr not in db:
                return
            w = db[addr]
            # 같은 marketId 거래 중 action 미기록 건에 결과 주입
            market_id = pos.get("marketId", "")
            for t in reversed(w.get("trades", [])):
                if t.get("marketId") == market_id and "action" not in t:
                    t["action"] = reason
                    t["pnl"]    = round(pnl, 4)
                    break
            # 집계 업데이트
            if pnl > 0:
                w["wins"] = w.get("wins", 0) + 1
            else:
                w["losses"] = w.get("losses", 0) + 1
            w["total_pnl"] = round(w.get("total_pnl", 0.0) + pnl, 4)
            save_whales_db(db)
        except Exception as e:
            print(f"[Bot][WARN] 고래 결과 역기록 실패: {e}")

    # ──────────────────────────────────────────────
    # 포지션 정산
    # ──────────────────────────────────────────────

    def _settle_positions(self):
        """보유 포지션 상태 체크 → 정산/손절"""
        now = int(time.time())
        for pos_key, pos in list(self.positions.items()):
            try:
                market = self.client.get_market(pos["marketId"])
                if not market:
                    continue

                if self._is_resolved(market):
                    # 승/패 판별: resolution 필드 우선 → 포지션 outcome과 비교
                    resolution = market.get("resolution") or {}
                    won_name = (resolution.get("name") or "").upper()
                    our_outcome = pos.get("outcome_name", "").upper()
                    if won_name:
                        # 우리가 보유한 outcome과 승리 outcome 비교
                        reason = "WIN" if won_name == our_outcome else "LOSS"
                        current = 1.0 if reason == "WIN" else 0.0
                    else:
                        # resolution name 없으면 현재가 기준
                        current = self.client.get_best_price(pos["marketId"], side="SELL") or pos["entry_price"]
                        reason = "WIN" if current >= 0.95 else "LOSS"
                    self._execute_sell(pos_key, pos, current, reason=reason)
                    continue

                # 현재가 조회 (No 결과물: 현재가 = 1 - YES ask)
                _pos_is_no = pos.get("outcome_name", "").upper() in ("NO", "DOWN", "BELOW", "UNDER")
                if _pos_is_no:
                    yes_ask = self.client.get_best_price(pos["marketId"], side="BUY")
                    current = round(1.0 - yes_ask, 4) if yes_ask is not None else None
                else:
                    current = self.client.get_best_price(pos["marketId"], side="SELL")

                if current is not None:
                    pos["current_price"] = current

                    # [우선순위 1] 자동 익절
                    if config.CONTRARIAN_MODE:
                        # Contrarian 구간별 차등 익절:
                        #   역진입가 >= 0.70 → +20% 익절 (만기 ROI 13~25%라 빠른 회전 유리)
                        #   역진입가 0.50~0.69 → 만기 보유 (50~80% ROI 기대)
                        #   역진입가 < 0.50 → 만기 보유 (300%+ ROI 기대)
                        _c_entry = pos["entry_price"]
                        _roi = (current - _c_entry) / _c_entry if _c_entry > 0 else 0
                        if _c_entry >= 0.70 and _roi >= 0.20:
                            print(f"[Bot] 🟢 Contrarian 익절 (고가진입 {_c_entry:.2f}, +{_roi:.0%}): {pos_key[:12]}...")
                            self._execute_sell(pos_key, pos, current, reason="TAKE_PROFIT")
                            continue
                        elif current >= 0.98:
                            print(f"[Bot] 🟢 자동 익절: {pos_key[:12]}... (현재가 {current:.3f} >= 0.98)")
                            self._execute_sell(pos_key, pos, current, reason="TAKE_PROFIT")
                            continue
                    else:
                        if current >= 0.98:
                            print(f"[Bot] 🟢 자동 익절: {pos_key[:12]}... (현재가 {current:.3f} >= 0.98)")
                            self._execute_sell(pos_key, pos, current, reason="TAKE_PROFIT")
                            continue

                    # [우선순위 2] VOID 마켓 감지: 가격 0.48~0.52 고착 8회 연속
                    if 0.48 <= current <= 0.52:
                        cnt = self._void_price_counter.get(pos_key, 0) + 1
                        self._void_price_counter[pos_key] = cnt
                        if cnt >= 8:
                            print(f"[Bot] ⚪ VOID 감지 (가격 {current:.3f} 고착 {cnt}회): {pos_key[:12]}...")
                            # VOID: 원금 복구, W/L 미반영
                            self.bankroll += pos["size_usdc"]
                            with self._position_lock:
                                self.positions.pop(pos_key, None)
                            self._void_price_counter.pop(pos_key, None)
                            self._save_state()
                            self._log_trade("VOID", pos_key, current, 0, pos)
                            tg_notifier.send_message(f"⚪ <b>VOID</b>\n📊 {pos.get('question', pos_key)[:40]}\n💵 원금 ${pos['size_usdc']:.2f} 복구")
                            continue
                    else:
                        self._void_price_counter.pop(pos_key, None)

                    # [우선순위 3] Hard Stop -90% — 비활성화 (2026-03-21)
                    # 사유: -90% 시점에서 건질 금액이 거의 없고, 회복 가능성만 차단함
                    # roi = (current - pos["entry_price"]) / pos["entry_price"]
                    # if roi <= -0.90:
                    #     self._execute_sell(pos_key, pos, current, reason="STOP_LOSS")
                    #     continue

                    # [우선순위 4] 일반 손절 -40%
                    drop = (pos["entry_price"] - current) / pos["entry_price"]
                    if drop >= config.STOP_LOSS_PCT:
                        print(f"[Bot] 🔴 손절: {pos_key[:12]}... ({drop:.0%} 하락)")
                        self._execute_sell(pos_key, pos, current, reason="STOP_LOSS")
                        continue
                else:
                    # 오더북 조회 실패 → 마지막 알려진 가격으로 손절 재시도
                    last = pos.get("current_price")
                    if last is not None:
                        drop = (pos["entry_price"] - last) / pos["entry_price"]
                        if drop >= config.STOP_LOSS_PCT:
                            print(f"[Bot] 🔴 손절(오더북 없음): {pos_key[:12]}... ({drop:.0%})")
                            self._execute_sell(pos_key, pos, last, reason="STOP_LOSS")
                            continue

                # 타임아웃 (7일)
                if now - pos.get("opened_at", now) > 7 * 86400:
                    fallback = pos.get("current_price", pos["entry_price"])
                    self._execute_sell(pos_key, pos, fallback, reason="TIMEOUT")

            except Exception as e:
                print(f"[Bot][WARN] 포지션 체크 실패 ({pos_key[:12]}...): {e}")

    # ──────────────────────────────────────────────
    # 유틸리티
    # ──────────────────────────────────────────────

    def _is_resolved(self, market: dict) -> bool:
        """마켓 정산 완료 여부 — API 응답 필드 정규화 (대소문자 불일치 방어)"""
        if market.get("resolved"):
            return True
        if market.get("resolution") is not None:
            return True
        status = market.get("status", "").upper()
        if status in ("RESOLVED", "CLOSED"):
            return True
        trading_status = market.get("tradingStatus", "").upper()
        if trading_status in ("CLOSED", "RESOLVED"):
            return True
        return False

    def _get_orderbook_spread(self, ob: dict | None) -> float | None:
        """오더북 dict → best ask - best bid 스프레드 (None = 계산 불가)"""
        if not ob:
            return None
        try:
            def _price(lv):
                if isinstance(lv, (list, tuple)):
                    return float(lv[0])
                return float(lv.get("price") or lv.get("p") or 0)
            asks = ob.get("asks", [])
            bids = ob.get("bids", [])
            if not asks or not bids:
                return None
            best_ask = _price(asks[0])
            best_bid = _price(bids[0])
            if best_ask <= 0 or best_bid <= 0 or best_ask <= best_bid:
                return None
            return best_ask - best_bid
        except Exception:
            return None

    def _get_portfolio(self) -> float:
        invested = sum(p["size_usdc"] for p in self.positions.values())
        pos_value = sum(
            p.get("current_price", p["entry_price"]) * p.get("shares", 0)
            for p in self.positions.values()
        )
        return self.bankroll + pos_value

    def _log_trade(self, action: str, pos_key: str, price: float, pnl: float, pos: dict = None):
        entry = {
            "action": action,
            "pos_key": pos_key,
            "price": price,
            "pnl": round(pnl, 4),
            "question": (pos or {}).get("question", ""),
            "bankroll_after": round(self.bankroll, 4),
            "timestamp": datetime.now().isoformat(),
        }
        with open(self.trade_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _save_state(self):
        now = time.time()
        if now - self._last_save_time < 10:
            return
        self._last_save_time = now
        state = {
            "bankroll": self.bankroll,
            "peak_bankroll": self.peak_bankroll,
            "stats": self.stats,
            "positions": self.positions,
            "seen_txs": list(self.seen_txs.keys())[-1000:],
        }
        tmp = self.state_file_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self.state_file_path)
        except Exception as e:
            print(f"[Bot][WARN] 상태 저장 실패: {e}")

    def _load_state(self):
        if not os.path.exists(self.state_file_path):
            return
        try:
            with open(self.state_file_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.bankroll      = state.get("bankroll", self.bankroll)
            self.peak_bankroll = state.get("peak_bankroll", self.bankroll)
            self.stats         = state.get("stats", self.stats)
            # 섀도우 필드 호환: 기존 state에 없으면 0으로 초기화
            self.stats.setdefault("shadow_wins", 0)
            self.stats.setdefault("shadow_losses", 0)
            self.stats.setdefault("shadow_pnl", 0.0)
            self.positions     = state.get("positions", {})
            for tx in state.get("seen_txs", []):
                self.seen_txs[tx] = 0
            # _startup_time 유지 — 백로그 차단 방어선 보존 (None 으로 해제하지 않음)
            print(f"[Bot] 상태 복구: 포지션 {len(self.positions)}개, PnL ${self.stats['total_pnl']:.2f}")
        except Exception as e:
            print(f"[Bot][WARN] 상태 복구 실패: {e}")

    # ──────────────────────────────────────────────
    # 유지보수 루프
    # ──────────────────────────────────────────────

    def _maintenance_loop(self):
        """주기적 스코어 갱신, 상태 저장"""
        last_score_update = 0
        while True:
            try:
                now = time.time()
                # 6시간마다 고래 스코어 갱신
                if now - last_score_update > 6 * 3600:
                    self.scorer.update_all()
                    db = load_whales_db()
                    self._active_whale_count = sum(1 for w in db.values() if w.get("score", 0) >= 0.05)
                    last_score_update = now
                self._save_state()
            except Exception as e:
                print(f"[Bot][WARN] 유지보수 루프 오류: {e}")
            time.sleep(300)

    # ──────────────────────────────────────────────
    # 텔레그램 인터페이스
    # ──────────────────────────────────────────────

    def _telegram_poll_loop(self):
        offset = None
        while True:
            try:
                updates = tg_notifier.get_updates(offset=offset)
                for upd in updates:
                    offset = upd["update_id"] + 1
                    # Reply Keyboard: 버튼 누르면 텍스트 메시지로 수신
                    msg = upd.get("message", {})
                    if msg.get("text"):
                        self._handle_tg_command(msg["text"].strip())
            except Exception:
                pass
            time.sleep(2)

    def _handle_tg_command(self, text: str):
        cmd = text.replace("📊 ", "").replace("📋 ", "").replace("📌 ", "") \
                  .replace("🐳 ", "").replace("🔄 ", "").replace("⏹ ", "").strip().lower()
        if cmd in ("status", "봇 상태"):
            self._send_telegram_status()
        elif cmd in ("trades", "거래 내역"):
            self._send_telegram_trades()
        elif cmd in ("positions", "포지션"):
            self._send_telegram_positions()
        elif cmd in ("whales", "고래"):
            self._send_telegram_whales()
        elif cmd in ("restart", "재시작"):
            if self._pending_confirm and self._pending_confirm.get("action") == "restart":
                tg_notifier.send_message("🔄 재시작 중...")
                self._save_state()
                os.execv(__import__("sys").executable, [__import__("sys").executable] + __import__("sys").argv)
            else:
                self._pending_confirm = {"action": "restart", "expires": time.time() + 30}
                tg_notifier.send_message("⚠️ 재시작하려면 30초 내 다시 누르시오.")
        elif cmd in ("stop", "종료"):
            tg_notifier.send_message("⏹ 봇 종료")
            self._save_state()
            os._exit(0)

    def _tg_keyboard(self):
        """하단 고정 Reply Keyboard — 채팅 전역에서 항상 보임"""
        return json.dumps({
            "keyboard": [
                [{"text": "📊 Status"}, {"text": "📋 Trades"}, {"text": "📌 Positions"}],
                [{"text": "🐳 Whales"}, {"text": "🔄 Restart"}, {"text": "⏹ Stop"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        })

    def _send_telegram_status(self):
        try:
            settled = self.stats["wins"] + self.stats["losses"]
            win_rate = (self.stats["wins"] / settled * 100) if settled else 0.0
            invested = sum(p["size_usdc"] for p in self.positions.values())
            pos_value = sum(
                p.get("current_price", p["entry_price"]) * p.get("shares", 0)
                for p in self.positions.values()
            )
            unrealized = pos_value - invested
            portfolio = self.bankroll + pos_value
            mode_str = "LIVE" if not config.PAPER_TRADING else "🟡 PAPER"
            if config.CONTRARIAN_MODE:
                mode_str += " CONTRARIAN"
            sep = "─────────────────"

            roi_line = ""
            try:
                total_return = self.stats["total_pnl"] + unrealized
                roi = total_return / config.INITIAL_BANKROLL * 100
                days_running = 0.0
                if os.path.exists(self.trade_log_path):
                    with open(self.trade_log_path, "r", encoding="utf-8") as _f:
                        first_line = _f.readline().strip()
                    if first_line:
                        first_ts = json.loads(first_line).get("timestamp")
                        if first_ts:
                            first_dt = datetime.fromisoformat(str(first_ts).split(".")[0]).replace(tzinfo=timezone.utc)
                            days_running = (datetime.now(timezone.utc) - first_dt).total_seconds() / 86400
                if days_running >= 1.0:
                    apr = roi / days_running * 365
                    roi_line = f"💹 ROI: {roi:+.2f}% | APR: {apr:+.1f}%\n"
                else:
                    roi_line = f"💹 ROI: {roi:+.2f}%\n"
            except Exception:
                pass

            msg = (
                f"📊 <b>BOT STATUS [{mode_str}]</b>\n"
                f"{sep}\n"
                f"💼 포트폴리오: ${portfolio:.2f}\n"
                f"💵 가용 잔고: ${self.bankroll:.2f}\n"
                f"{sep}\n"
                f"📈 실현 PnL: ${self.stats['total_pnl']:+.2f}\n"
                f"📉 미실현 PnL: ${unrealized:+.2f}\n"
                f"🎯 승률: {win_rate:.1f}% ({self.stats['wins']}W/{self.stats['losses']}L)\n"
                f"{roi_line}"
                f"{sep}\n"
                f"🐳 <b>{'고래 직접 카피 시 (가상)' if config.CONTRARIAN_MODE else '반대매매 시 (가상)'}</b>\n"
                f"📈 가상 PnL: ${self.stats.get('shadow_pnl', 0):+.2f}\n"
                f"🎯 가상 승률: {(self.stats.get('shadow_wins',0) / max(1, self.stats.get('shadow_wins',0) + self.stats.get('shadow_losses',0)) * 100):.1f}% ({self.stats.get('shadow_wins',0)}W/{self.stats.get('shadow_losses',0)}L)\n"
                f"{sep}\n"
                f"📌 활성 포지션: {len(self.positions)}개\n"
                f"🐳 추적 고래: {self._active_whale_count}마리\n"
                f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} (UTC)"
            )
            tg_notifier.send_message(msg, reply_markup=self._tg_keyboard())
        except Exception as e:
            print(f"[Bot][WARN] 상태 전송 실패: {e}")

    def _send_telegram_trades(self):
        try:
            if not os.path.exists(self.trade_log_path):
                tg_notifier.send_message("📋 <b>거래 내역 없음</b>")
                return
            with open(self.trade_log_path, "r", encoding="utf-8") as f:
                all_trades = [json.loads(l) for l in f if l.strip()]
            settled = [t for t in all_trades if t.get("action") in ("WIN","LOSS","STOP_LOSS","MIRROR_EXIT","TIMEOUT")]
            if not settled:
                tg_notifier.send_message("📋 <b>정산된 거래 없음</b>")
                return
            rows = []
            for t in reversed(settled[-10:]):
                icon = "✅" if t.get("action") == "WIN" else "❌"
                rows.append(f"{icon} <b>{t.get('action')}</b> {(t.get('question') or '')[:28]}\n  💵 {t.get('pnl', 0):+.2f}")
            tg_notifier.send_message("📋 <b>최근 거래 (10건)</b>\n" + "\n".join(rows))
        except Exception as e:
            print(f"[Bot][WARN] 거래 내역 전송 실패: {e}")

    def _resolve_question(self, pos: dict) -> str:
        """question 필드가 비어있거나 순수 숫자(=과거 market_id 저장 버그)면 API 재조회"""
        q = pos.get("question", "")
        if q and not str(q).strip().lstrip("-").isdigit():
            return q
        market_id = pos.get("marketId", "")
        if market_id:
            try:
                market = self.client.get_market(str(market_id))
                if market:
                    name = (market.get("question") or market.get("title") or
                            market.get("name") or str(market_id))
                    pos["question"] = name  # 캐시
                    return name
            except Exception:
                pass
        return str(market_id) or "unknown"

    def _send_telegram_positions(self):
        try:
            if not self.positions:
                tg_notifier.send_message("📌 <b>활성 포지션 없음</b>")
                return
            rows = []
            total_unrealized = 0.0
            for pos in self.positions.values():
                cur = pos.get("current_price", pos["entry_price"])
                unr = (cur - pos["entry_price"]) * pos.get("shares", 0)
                total_unrealized += unr
                icon = "📈" if unr >= 0 else "📉"
                label = self._resolve_question(pos)
                # 만기일 파싱 (boostEndsAt 필드)
                raw_end = pos.get("end_date")
                if raw_end:
                    try:
                        from datetime import datetime, timezone
                        ed = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
                        exp_str = ed.strftime("%m/%d %H:%M")
                    except Exception:
                        exp_str = str(raw_end)[:11]
                    rows.append(f"{icon} {label[:25]} | {unr:+.2f} | ⏰{exp_str}")
                else:
                    rows.append(f"{icon} {label[:25]} | {unr:+.2f}")
            msg = (f"📌 <b>포지션 ({len(self.positions)}개)</b>\n" +
                   "\n".join(rows) +
                   f"\n─────\n미실현 합계: ${total_unrealized:+.2f}")
            tg_notifier.send_message(msg)
        except Exception as e:
            print(f"[Bot][WARN] 포지션 전송 실패: {e}")

    def _send_telegram_whales(self):
        try:
            db = load_whales_db()
            active = [w for w in db.values() if w.get("score", 0) >= 0.05]
            active.sort(key=lambda w: w.get("score", 0), reverse=True)
            if not active:
                tg_notifier.send_message("🐳 <b>추적 고래 없음</b>\n(score >= 0.05 고래 없음)")
                return
            rows = []
            for i, w in enumerate(active, 1):
                name = w.get("name") or w.get("address", "?")[:10]
                score = w.get("score", 0.0)
                pnl = w.get("leaderboard_pnl", 0.0)
                tag = "🔷" if w.get("bootstrap") else "⭐"
                stat = f"{score*100:.1f}점 (PnL ${pnl:+.0f})"
                rows.append(f"{i}. {tag}{name} — {stat}")
            # 25마리씩 페이지 분할 (텔레그램 4096자 제한 대응)
            PAGE = 25
            total = len(active)
            pages = [rows[i:i+PAGE] for i in range(0, len(rows), PAGE)]
            for p_idx, page_rows in enumerate(pages):
                page_info = f" ({p_idx+1}/{len(pages)})" if len(pages) > 1 else ""
                msg = (
                    f"🐳 <b>추적 고래 ({total}마리){page_info}</b>\n"
                    "─────────────────\n" +
                    "\n".join(page_rows)
                )
                tg_notifier.send_message(msg)
        except Exception as e:
            print(f"[Bot][WARN] 고래 목록 전송 실패: {e}")


if __name__ == "__main__":
    bot = PredictCopyBot()
    bot.run()
