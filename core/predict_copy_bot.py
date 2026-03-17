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
from whale_manager import run_manager, load_whales_db
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

        # 자산
        self.bankroll = config.INITIAL_BANKROLL
        self.peak_bankroll = self.bankroll
        self.stats = {
            "total_bets": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "max_drawdown": 0.0,
        }

        suffix = "_PAPER" if config.PAPER_TRADING else "_LIVE"
        self.trade_log_path  = os.path.join(config.DATA_DIR, f"trade_history{suffix}.jsonl")
        self.state_file_path = os.path.join(config.DATA_DIR, f"state_Predict{suffix}.json")

        self._startup_time = int(time.time())
        self._load_state()

        self.client  = PredictFunClient()
        self.scorer  = WhaleScorer()

        # WebSocket 고래 탐지 시작
        self.watcher = run_manager(self.client)

        # 백그라운드 스레드
        threading.Thread(target=self._maintenance_loop, daemon=True).start()
        threading.Thread(target=self._telegram_poll_loop, daemon=True).start()

        mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        print(f"[Bot] PredictCopyBot 시작 ({mode}) — 초기 뱅크롤 ${self.bankroll:.2f}")
        tg_notifier.send_message(
            f"🚀 <b>Predict.fun 봇 시작 [{mode}]</b>\n"
            f"💵 뱅크롤: ${self.bankroll:.2f}\n"
            f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
            reply_markup=self._tg_keyboard(),
        )

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
        addr      = trade.get("address", "")
        market_id = trade.get("marketId", "")
        side      = trade.get("side", "")
        price     = float(trade.get("price") or 0)
        size_usdt = float(trade.get("size_usdt") or 0)
        tx_hash   = trade.get("transactionHash", "")
        ts        = trade.get("timestamp", int(time.time()))

        # ── 중복 방어 ──
        if tx_hash and tx_hash in self.seen_txs:
            return
        if tx_hash:
            self.seen_txs[tx_hash] = ts
            if len(self.seen_txs) > 5000:
                for _ in range(500):
                    self.seen_txs.popitem(last=False)

        # ── 백로그 차단 ──
        if self._startup_time and ts < self._startup_time:
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
        if price < config.MIN_PRICE or price > config.MAX_PRICE:
            return

        # ── Filter 4: 고래 스코어 확인 ──
        db = load_whales_db()
        whale_info = db.get(addr)
        if whale_info:
            score = whale_info.get("score", 0)
            # score > 0: 스코어링 완료 → 기준 미달이면 스킵
            # score = 0: 아직 미평가 (신규 고래) → 크기 기준으로 통과 허용
            if score > 0 and score < 0.2:
                return

        # ── Filter 5: market_signal 중복 방어 ──
        sig_key = f"{market_id}:{side_upper}:{addr}"
        now = int(time.time())
        if now - self._seen_market_signals.get(sig_key, 0) < 300:
            return
        self._seen_market_signals[sig_key] = now
        if len(self._seen_market_signals) > 500:
            cutoff = now - 600
            self._seen_market_signals = {
                k: v for k, v in self._seen_market_signals.items() if v > cutoff
            }

        # ── 마켓 검증 ──
        market = self.client.get_market(market_id)
        if not market:
            return
        # TODO: 마켓 resolved/closed 체크 (필드명 확인 필요)
        if market.get("resolved") or market.get("status") in ("resolved", "closed"):
            return

        # ── 베팅금 계산 ──
        portfolio = self._get_portfolio()
        bet_size = portfolio / config.BET_DIVISOR
        if bet_size > self.bankroll:
            bet_size = self.bankroll * 0.9
        if bet_size < 1.0:
            return

        whale_name = (whale_info or {}).get("name", addr[:8])
        print(f"[Bot] 🐳 복사 진입: {whale_name} | {market_id[:12]}... | ${bet_size:.2f} @ {price:.3f}")
        self._execute_trade(market_id, market, price, bet_size, addr, whale_name)

    def _handle_mirror_exit(self, addr: str, market_id: str, price: float):
        """고래 SELL 감지 → 동일 마켓 포지션 조기 청산"""
        for pos_key, pos in list(self.positions.items()):
            if pos.get("marketId") != market_id or pos.get("whale_addr") != addr:
                continue
            current = pos.get("current_price", pos["entry_price"])
            if current < pos["entry_price"]:
                print(f"[Bot] [MIRROR HOLD] 손실 중 — 자연정산 대기 ({pos_key[:12]}...)")
                continue
            print(f"[Bot] [MIRROR EXIT] 고래 SELL 감지 → 청산: {pos_key[:12]}...")
            self._execute_sell(pos_key, pos, current, reason="MIRROR_EXIT")

    # ──────────────────────────────────────────────
    # 주문 실행
    # ──────────────────────────────────────────────

    def _execute_trade(self, market_id: str, market: dict, price: float,
                        bet_size: float, whale_addr: str, whale_name: str):
        """BUY 주문 실행"""
        result = self.client.place_order(
            market_id=market_id,
            side=0,  # BUY
            price=price,
            size_usdt=bet_size,
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
        shares = bet_size / exec_price if exec_price > 0 else 0
        pos_key = f"{market_id}_{whale_addr[:8]}_{int(time.time())}"

        market_name = (
            market.get("question") or market.get("title") or
            market.get("name") or market.get("description") or
            market.get("marketQuestion") or str(market_id)
        )

        with self._position_lock:
            self.positions[pos_key] = {
                "marketId": market_id,
                "whale_addr": whale_addr,
                "whale_name": whale_name,
                "entry_price": exec_price,
                "current_price": exec_price,
                "size_usdc": bet_size,
                "shares": shares,
                "opened_at": int(time.time()),
                "question": market_name,
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
        pnl = (sell_price - pos["entry_price"]) * pos.get("shares", 0)
        if reason in ("WIN",) and sell_price >= 0.99:
            pnl = pos["size_usdc"] * (1 / pos["entry_price"] - 1)

        result = self.client.place_order(
            market_id=pos["marketId"],
            side=1,  # SELL
            price=sell_price,
            size_usdt=pos.get("shares", 0) * sell_price,
        )

        recovered = pos["size_usdc"] + pnl
        self.bankroll += recovered
        self.stats["total_pnl"] += pnl
        if pnl >= 0:
            self.stats["wins"] += 1
        else:
            self.stats["losses"] += 1

        with self._position_lock:
            self.positions.pop(pos_key, None)

        self._save_state()
        self._log_trade(reason, pos_key, sell_price, pnl, pos)

        icon = "✅" if pnl >= 0 else "❌"
        tg_notifier.send_message(
            f"{icon} <b>{reason}</b>\n"
            f"📊 {pos.get('question', pos_key)[:40]}\n"
            f"💵 PnL: ${pnl:+.2f} | 잔고: ${self.bankroll:.2f}"
        )

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

                # TODO: 정산 감지 필드명 확인
                resolved = market.get("resolved") or market.get("status") in ("resolved", "closed")
                if resolved:
                    # 승/패 판별
                    # TODO: 결과 필드명 확인 (winningOutcome, result 등)
                    result_val = market.get("winningOutcome") or market.get("result") or ""
                    # 단순화: 현재가로 판단 (0.95 이상이면 WIN)
                    current = self.client.get_best_price(pos["marketId"], side="SELL") or pos["entry_price"]
                    reason = "WIN" if current >= 0.95 else "LOSS"
                    self._execute_sell(pos_key, pos, current, reason=reason)
                    continue

                # 손절 체크
                current = self.client.get_best_price(pos["marketId"], side="SELL")
                if current:
                    pos["current_price"] = current
                    drop = (pos["entry_price"] - current) / pos["entry_price"]
                    if drop >= config.STOP_LOSS_PCT:
                        print(f"[Bot] 🔴 손절: {pos_key[:12]}... ({drop:.0%} 하락)")
                        self._execute_sell(pos_key, pos, current, reason="STOP_LOSS")
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
            self.positions     = state.get("positions", {})
            for tx in state.get("seen_txs", []):
                self.seen_txs[tx] = 0
            self._startup_time = None  # 복구 성공 → 백로그 필터 해제
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
                    self._active_whale_count = len(db)
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
                  .replace("🔄 ", "").replace("⏹ ", "").strip().lower()
        if cmd in ("status", "봇 상태"):
            self._send_telegram_status()
        elif cmd in ("trades", "거래 내역"):
            self._send_telegram_trades()
        elif cmd in ("positions", "포지션"):
            self._send_telegram_positions()
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
                [{"text": "🔄 Restart"}, {"text": "⏹ Stop"}],
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
                            first_dt = datetime.fromisoformat(str(first_ts).split(".")[0])
                            days_running = (datetime.utcnow() - first_dt).total_seconds() / 86400
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
                f"📌 활성 포지션: {len(self.positions)}개\n"
                f"🐳 추적 고래: {self._active_whale_count}마리\n"
                f"🕒 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} (UTC)"
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
                rows.append(f"{icon} {label[:25]} | {unr:+.2f}")
            msg = (f"📌 <b>포지션 ({len(self.positions)}개)</b>\n" +
                   "\n".join(rows) +
                   f"\n─────\n미실현 합계: ${total_unrealized:+.2f}")
            tg_notifier.send_message(msg)
        except Exception as e:
            print(f"[Bot][WARN] 포지션 전송 실패: {e}")


if __name__ == "__main__":
    bot = PredictCopyBot()
    bot.run()
