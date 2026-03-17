"""
Predict.fun 클라이언트 래퍼 (predict-sdk 0.0.15 기반)

확인된 SDK 흐름:
  1. get_orderbook() → Book(market_id, ts, asks=[DepthLevel((price_wei, size_wei))], bids=[...])
  2. get_market_order_amounts(MarketHelperValueInput(BUY, value_wei, slippage_bps), book) → OrderAmounts
  3. build_typed_data(BuildOrderInput(side, token_id, maker_amount, taker_amount, fee_rate_bps)) → EIP712TypedData
  4. sign_typed_data_order(typed_data) → SignedOrder
  5. POST /v1/orders

TODO (API 응답 첫 확인 시 수정):
  - get_markets() 응답 필드명 (tokenId? token_id? id?)
  - get_markets() fee_rate_bps 필드명
  - get_markets() precision 필드명
  - get_orderbook() asks/bids price/size 필드명
  - market_id 가 int인지 str인지
"""

import logging
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from config import config

from predict_sdk import (
    OrderBuilder, BuildOrderInput,
    ChainId, Side,
    ADDRESSES_BY_CHAIN_ID, RPC_URLS_BY_CHAIN_ID,
    generate_order_salt,
    LimitHelperInput, MarketHelperInput, MarketHelperValueInput,
    Book, DepthLevel, SignedOrder,
)
from eth_account import Account
from web3 import Web3

logger = logging.getLogger(__name__)

CHAIN_ID          = ChainId.BNB_MAINNET
WEI               = 10 ** 18
DEFAULT_PRECISION = 5   # TODO: 마켓 API 응답에서 가져오도록 수정


def usdt_to_wei(amount: float) -> int:
    return int(amount * WEI)

def wei_to_usdt(amount_wei: int) -> float:
    return amount_wei / WEI


class PredictFunClient:
    def __init__(self):
        self.api_base = config.PREDICT_API_BASE
        self.api_key  = config.PREDICT_API_KEY
        self.authenticated = bool(
            self.api_key and "your_" not in (self.api_key or "").lower()
        )

        # HTTP 세션 (retry 포함)
        self.session = requests.Session()
        retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({
            "User-Agent": "PredictBot/1.0",
            "Accept": "application/json",
            # 공개 API: 인증 불필요. 주문 등 private 작업: x-api-key + Authorization: Bearer <JWT>
            "x-api-key": self.api_key or "",
        })

        # Web3 + OrderBuilder 초기화 (LIVE 모드 + PRIVATE_KEY 있을 때만)
        self.w3      = None
        self.builder = None
        if not config.PAPER_TRADING and config.PRIVATE_KEY:
            self._init_builder()

        mode = "LIVE" if self.authenticated else "UNAUTHENTICATED"
        sdk  = "OK" if self.builder else "SKIP(페이퍼/키없음)"
        print(f"[Client] PredictFunClient 초기화 ({mode}, SDK={sdk})")

    def _init_builder(self):
        try:
            rpc_url = RPC_URLS_BY_CHAIN_ID.get(CHAIN_ID, "https://bsc-dataseed.bnbchain.org/")
            self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))

            if not self.w3.is_connected():
                for url in config.BSC_RPC_URLS:
                    w3 = Web3(Web3.HTTPProvider(url))
                    if w3.is_connected():
                        self.w3 = w3
                        break

            signer    = Account.from_key(config.PRIVATE_KEY)
            addresses = ADDRESSES_BY_CHAIN_ID[CHAIN_ID]

            self.builder = OrderBuilder(
                chain_id=CHAIN_ID,
                precision=DEFAULT_PRECISION,
                addresses=addresses,
                generate_salt_fn=generate_order_salt,
                logger=logger,
                signer=signer,
                predict_account=config.WALLET_ADDRESS,
                web3=self.w3,
            )
            print(f"[Client] OrderBuilder 초기화 성공 (RPC: {rpc_url})")
        except Exception as e:
            print(f"[Client][WARN] OrderBuilder 초기화 실패: {e}")
            self.builder = None

    # ─────────────────────────────────────────
    # 마켓 조회
    # ─────────────────────────────────────────

    def get_markets(self, limit: int = 100, offset: int = 0) -> list:
        try:
            r = self.session.get(
                f"{self.api_base}/v1/markets",
                params={"limit": limit, "offset": offset, "status": "OPEN"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            # 응답: 배열 직접 or {"data": [...]} or {"markets": [...]}
            if isinstance(data, list):
                return data
            return data.get("data", data.get("markets", []))
        except Exception as e:
            print(f"[Client][ERR] get_markets: {e}")
            return []

    def get_market(self, market_id: str) -> dict | None:
        try:
            r = self.session.get(f"{self.api_base}/v1/markets/{market_id}", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[Client][ERR] get_market({market_id}): {e}")
            return None

    def get_orderbook(self, market_id: str) -> dict | None:
        """오더북 raw dict 반환 — 엔드포인트: /v1/markets/{id}/orderbook"""
        try:
            r = self.session.get(
                f"{self.api_base}/v1/markets/{market_id}/orderbook", timeout=10
            )
            r.raise_for_status()
            raw = r.json()
            # {"success": true, "data": {...}} 래핑 처리
            return raw.get("data", raw)
        except Exception as e:
            print(f"[Client][ERR] get_orderbook({market_id}): {e}")
            return None

    def _build_book(self, market_id: str, ob: dict) -> Book | None:
        """
        API 오더북 응답 → predict_sdk.Book 변환
        응답: {"asks": [[price, qty], ...], "bids": [...], "marketId": int, "updateTimestampMs": ms}
        DepthLevel = tuple 서브클래스: DepthLevel((price_wei, size_wei))
        """
        try:
            def parse_levels(levels: list) -> list:
                result = []
                for lv in levels:
                    if isinstance(lv, (list, tuple)) and len(lv) >= 2:
                        price_wei = int(float(lv[0]) * WEI)
                        size_wei  = int(float(lv[1]) * WEI)
                    elif isinstance(lv, dict):
                        p = lv.get("price") or lv.get("p") or 0
                        s = lv.get("size") or lv.get("quantity") or lv.get("amount") or 0
                        price_wei = int(float(p) * WEI)
                        size_wei  = int(float(s) * WEI)
                    else:
                        continue
                    if price_wei > 0 and size_wei > 0:
                        result.append(DepthLevel((price_wei, size_wei)))
                return result

            asks = parse_levels(ob.get("asks", []))
            bids = parse_levels(ob.get("bids", []))
            mid_raw = ob.get("marketId") or ob.get("market_id") or market_id
            try:
                mid_int = int(mid_raw)
            except (ValueError, TypeError):
                mid_int = 0
            ts = ob.get("updateTimestampMs") or ob.get("update_timestamp_ms") or int(time.time() * 1000)
            return Book(market_id=mid_int, update_timestamp_ms=int(ts), asks=asks, bids=bids)
        except Exception as e:
            print(f"[Client][WARN] Book 변환 실패: {e}")
            return None

    def get_best_price(self, market_id: str, side: str = "BUY") -> float | None:
        """오더북 최우선 호가 (0~1 범위)"""
        ob = self.get_orderbook(market_id)
        if not ob:
            return None
        try:
            levels = ob.get("asks" if side == "BUY" else "bids", [])
            if not levels:
                return None
            lv = levels[0]
            if isinstance(lv, (list, tuple)):
                return float(lv[0])
            return float(lv.get("price") or lv.get("p") or 0)
        except Exception:
            return None

    def get_market_activity(self, market_id: str, limit: int = 50) -> list:
        try:
            r = self.session.get(
                f"{self.api_base}/v1/markets/{market_id}/activity",
                params={"limit": limit}, timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("activity", data.get("data", []))
        except Exception as e:
            print(f"[Client][ERR] get_market_activity: {e}")
            return []

    # ─────────────────────────────────────────
    # 주문 실행
    # ─────────────────────────────────────────

    def place_order(
        self,
        market_id: str,
        side: int,           # 0=BUY, 1=SELL
        price: float,        # 0~1 (limit 주문 시 사용)
        size_usdt: float,    # USDT 금액
        slippage_bps: int | None = None,
        order_type: str = "MARKET",  # "MARKET" | "LIMIT"
    ) -> dict | None:
        if config.PAPER_TRADING:
            # ── 현실적 페이퍼: 실제 오더북 + 슬리피지 + 수수료 반영 ──
            bps = slippage_bps if slippage_bps is not None else config.DEFAULT_SLIPPAGE_BPS

            # 실제 오더북 호가 조회
            actual_price = price
            try:
                ob = self.get_orderbook(market_id)
                if ob:
                    levels = ob.get("asks" if side == 0 else "bids", [])
                    if levels:
                        lv = levels[0]
                        actual_price = float(lv[0]) if isinstance(lv, (list, tuple)) else float(lv.get("price", price))
            except Exception:
                pass  # 조회 실패 시 whale 가격 fallback

            # 슬리피지 적용 (BUY: 가격 상승, SELL: 가격 하락)
            if side == 0:
                actual_price = min(actual_price * (1 + bps / 10_000), 0.99)
            else:
                actual_price = max(actual_price * (1 - bps / 10_000), 0.01)

            # 수수료 차감 (마켓별 feeRateBps, 기본 150bps=1.5%)
            fee_bps = 150
            try:
                mkt = self.get_market(market_id)
                fee_bps = int((mkt or {}).get("feeRateBps", 150))
            except Exception:
                pass
            fee = size_usdt * fee_bps / 10_000

            return {
                "status": "matched",
                "paper": True,
                "price": actual_price,
                "size": size_usdt - fee,
                "fee": fee,
            }

        if not self.authenticated:
            print("[Client][WARN] 인증 정보 없음")
            return None

        if not self.builder:
            print("[Client][ERR] OrderBuilder 없음 — PRIVATE_KEY 확인")
            return None

        bps      = slippage_bps if slippage_bps is not None else config.DEFAULT_SLIPPAGE_BPS
        sdk_side = Side.BUY if side == 0 else Side.SELL

        # ── 1. 마켓 정보 (token_id, fee_rate_bps 필요) ──
        market = self.get_market(market_id)
        if not market:
            print(f"[Client][ERR] 마켓 정보 없음: {market_id}")
            return None

        # token_id = outcomes[N].onChainId (YES/UP outcome 우선)
        outcomes = market.get("outcomes", [])
        yes_outcome = next(
            (o for o in outcomes if o.get("name", "").upper() in ("YES", "UP", "ABOVE")),
            outcomes[0] if outcomes else None,
        )
        token_id     = yes_outcome.get("onChainId") if yes_outcome else None
        fee_rate_bps = int(market.get("feeRateBps") or 0)
        precision    = int(market.get("decimalPrecision") or DEFAULT_PRECISION)

        if not token_id:
            print(f"[Client][ERR] token_id(onChainId) 없음 — outcomes: {outcomes}")
            return None

        # OrderBuilder precision을 마켓별로 동기화
        if self.builder:
            try:
                self.builder._precision = precision
            except Exception:
                pass

        # ── 2. 오더북 → Book ──
        ob = self.get_orderbook(market_id)
        if not ob:
            print("[Client][ERR] 오더북 조회 실패")
            return None
        book = self._build_book(market_id, ob)
        if not book:
            return None

        # ── 3. 주문 금액 계산 ──
        try:
            if order_type == "LIMIT":
                amounts = self.builder.get_limit_order_amounts(
                    LimitHelperInput(
                        side=sdk_side,
                        price_per_share_wei=int(price * WEI),
                        quantity_wei=usdt_to_wei(size_usdt / price if price > 0 else size_usdt),
                    )
                )
            elif sdk_side == Side.BUY:
                amounts = self.builder.get_market_order_amounts(
                    MarketHelperValueInput(
                        side=Side.BUY,
                        value_wei=usdt_to_wei(size_usdt),
                        slippage_bps=bps,
                    ),
                    book,
                )
            else:  # SELL — 수량 기준
                shares_wei = usdt_to_wei(size_usdt / price) if price > 0 else usdt_to_wei(size_usdt)
                amounts = self.builder.get_market_order_amounts(
                    MarketHelperInput(
                        side=Side.SELL,
                        quantity_wei=shares_wei,
                        slippage_bps=bps,
                    ),
                    book,
                )
        except Exception as e:
            print(f"[Client][ERR] 주문 수량 계산 실패: {e}")
            return None

        # ── 4. 서명 ──
        try:
            typed_data = self.builder.build_typed_data(BuildOrderInput(
                side=sdk_side,
                token_id=token_id,
                maker_amount=amounts.maker_amount,
                taker_amount=amounts.taker_amount,
                fee_rate_bps=fee_rate_bps,
            ))
            signed: SignedOrder = self.builder.sign_typed_data_order(typed_data)
        except Exception as e:
            print(f"[Client][ERR] 주문 서명 실패: {e}")
            return None

        # ── 5. POST /v1/orders ──
        try:
            payload = signed.model_dump() if hasattr(signed, "model_dump") else signed.__dict__
            r = self.session.post(
                f"{self.api_base}/v1/orders",
                json=payload,
                timeout=15,
            )
            r.raise_for_status()
            result = r.json()
            print(f"[Client] 주문 응답: {result}")
            return result
        except Exception as e:
            print(f"[Client][ERR] POST /v1/orders 실패: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        if config.PAPER_TRADING:
            return True
        try:
            r = self.session.post(
                f"{self.api_base}/v1/orders/remove",
                json={"orderId": order_id},
                timeout=10,
            )
            return r.status_code == 200
        except Exception as e:
            print(f"[Client][ERR] cancel_order: {e}")
            return False

    # ─────────────────────────────────────────
    # 잔고 / 포지션
    # ─────────────────────────────────────────

    def get_positions(self) -> list:
        if not self.authenticated:
            return []
        try:
            r = self.session.get(
                f"{self.api_base}/v1/positions",
                params={"wallet": config.WALLET_ADDRESS},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("positions", [])
        except Exception as e:
            print(f"[Client][ERR] get_positions: {e}")
            return []

    def get_usdt_balance(self) -> float | None:
        """USDT 잔고 (SDK balance_of 우선, fallback web3 직접)"""
        if not config.WALLET_ADDRESS:
            return None
        # SDK 경로
        if self.builder:
            try:
                bal_wei = self.builder.balance_of(
                    address=config.WALLET_ADDRESS,
                    token_address=ADDRESSES_BY_CHAIN_ID[CHAIN_ID].USDT,
                )
                return wei_to_usdt(bal_wei)
            except Exception:
                pass
        # web3 직접 조회 fallback
        if self.w3:
            try:
                abi = [{"inputs": [{"name": "account", "type": "address"}],
                        "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                        "stateMutability": "view", "type": "function"}]
                contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(ADDRESSES_BY_CHAIN_ID[CHAIN_ID].USDT),
                    abi=abi,
                )
                bal_wei = contract.functions.balanceOf(
                    Web3.to_checksum_address(config.WALLET_ADDRESS)
                ).call()
                return wei_to_usdt(bal_wei)
            except Exception as e:
                print(f"[Client][ERR] get_usdt_balance: {e}")
        return None

    def ensure_approvals(self):
        """실거래 시작 전 ERC20/ERC1155 Approve 일괄 설정"""
        if config.PAPER_TRADING or not self.builder:
            return
        try:
            result = self.builder.set_approvals()
            print(f"[Client] Approval 완료: {result}")
        except Exception as e:
            print(f"[Client][WARN] ensure_approvals 실패: {e}")

    def get_leaderboard(self, limit: int = 100) -> list:
        for ep in ["/v1/leaderboard", "/v1/traders/top", "/v1/stats/leaders"]:
            try:
                r = self.session.get(
                    f"{self.api_base}{ep}", params={"limit": limit}, timeout=10
                )
                if r.status_code == 200:
                    data = r.json()
                    print(f"[Client] 리더보드 엔드포인트: {ep}")
                    return data if isinstance(data, list) else data.get("traders", data.get("data", []))
            except Exception:
                continue
        print("[Client][WARN] 리더보드 API 미확인 — WebSocket 방식 사용")
        return []
