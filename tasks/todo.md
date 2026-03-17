# Predict.fun 봇 구현 계획

## 최종 상태: ✅ Phase 2 완료 — 페이퍼 트레이딩 실행 중

**마지막 업데이트**: 2026-03-17

---

## 1. Polymarket vs Predict.fun 핵심 차이점

| 항목 | Polymarket | Predict.fun | 호환성 |
|------|-----------|-------------|--------|
| 체인 | Polygon (chain_id=137) | BNB Chain (chain_id=56) | ✅ 처리됨 |
| 결제 토큰 | USDC (6 decimals) | USDT (18 decimals, wei) | ✅ 처리됨 |
| 클라이언트 SDK | py_clob_client | predict-sdk 0.0.15 | ✅ 교체 완료 |
| 주문 방식 | REST CLOB | REST CLOB | ✅ 구조 동일 |
| 고래 탐지 | data-api /activity 폴링 | /v1/orders/matches REST 폴링 | ✅ 구현 완료 |
| 리더보드 API | 없음 | 없음 (공식 확인) | ✅ 폴링 방식으로 대체 |
| 유동성 | $415M OI | $14.2M OI | ⚠️ 슬리피지 높음 |

---

## 2. 구현 체크리스트

### ✅ Phase 0 — 환경 준비
- [x] predict-sdk 0.0.15 설치 확인 (`>=0.0.1` 버전 명시)
- [x] `.env` 파일 생성 및 설정
- [x] predict-sdk SDK 구조 역분석 완료 (OrderBuilder, DepthLevel, Book 등)
- [x] BNB Chain RPC 연결 확인

### ✅ Phase 1 — API 검증
- [x] `GET /v1/markets?status=OPEN` 동작 확인
- [x] `GET /v1/markets/{id}/orderbook` 응답 구조 확인 (`{"data": {...}}` 래핑)
- [x] `GET /v1/orders/matches?minValueUsdtWei=...` 고래 탐지 동작 확인
- [x] `GET /v1/positions/{address}` 포지션 조회 확인
- [x] 인증 방식 확인 (`x-api-key` 헤더, Bearer 아님)

### ✅ Phase 2 — 핵심 기능 구현
- [x] `config.py` — BNB Chain 설정, .env 로드 경로 수정
- [x] `client_wrapper.py` — place_order, get_orderbook, Paper 현실화
- [x] `whale_manager.py` — REST 폴링 방식 고래 탐지, 워밍업 로직
- [x] `predict_copy_bot.py` — 메인 봇, Reply Keyboard 텔레그램
- [x] `whale_scorer.py` — 고래 스코어링
- [x] `telegram_notifier.py` — 알림
- [x] `run_bot.py` — Watchdog 자동 재시작

### 🟡 Phase 3 — 라이브 전환 (2026-03-31 이후)
- [ ] 2주 페이퍼 데이터 분석 (trade_history_PAPER.jsonl)
- [ ] 슬리피지/유동성 실측치 반영 파라미터 튜닝
- [ ] `PAPER_TRADING=False` 전환 + 소액 테스트
- [ ] Termux tmux 안정성 확인 또는 VPS 배포 검토

---

## 3. 주요 버그 수정 이력

| 버그 | 원인 | 수정 |
|------|------|------|
| 401 Unauthorized | Bearer 헤더 사용 | `x-api-key` 로 변경 |
| 400 Bad Request (마켓조회) | `status=active` 잘못된 값 | `status=OPEN` 으로 수정 |
| 404 orderbook | 엔드포인트 오류 | `/v1/markets/{id}/orderbook` 으로 수정 |
| 신규 고래 전부 차단 | `score < 0.2` 필터가 score=0(미평가)도 차단 | `score > 0 and score < 0.2` 로 수정 |
| 봇 시작 시 과거 거래 몰림 | 첫 폴링 결과 전부 처리 | 워밍업 dry_run 로직 추가 |
| 즉시 -$46 손절 | MAX_PRICE=0.93으로 정산직전 마켓 진입 | MAX_PRICE=0.85 로 하향 |
| Paper PnL 낙관편향 20~25% | whale 가격 기준 shares 계산 | exec_price(오더북+슬리피지+수수료) 기준 전환 |

---

## 4. 현재 운영 파라미터

```
INITIAL_BANKROLL = 3000.0
PAPER_TRADING    = True
MAX_POSITIONS    = 20
BET_DIVISOR      = 20       → 1회 베팅 ~$150
STOP_LOSS_PCT    = 0.40
MIN_WHALE_SIZE   = $500
MIN_PRICE        = 0.10
MAX_PRICE        = 0.85
SLIPPAGE         = 300 bps (3%)
```

---

## 5. 페이퍼 트레이딩 현황

- **시작일**: 2026-03-17
- **뱅크롤**: $3000.00 PAPER
- **환경**: Termux (Android) + tmux 백그라운드
- **분석 예정**: 2026-03-31 (2주 후)
- **확인 파일**: `data/trade_history_PAPER.jsonl`, `data/whales_predict.json`
