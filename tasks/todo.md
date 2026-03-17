# Predict.fun 봇 구현 계획

## 사전 분석 보고서

### ✅ 구현 가능 여부: 가능 (단, 3개 검증 필요)

---

## 1. Polymarket vs Predict.fun 핵심 차이점

| 항목 | Polymarket | Predict.fun | 호환성 |
|------|-----------|-------------|--------|
| 체인 | Polygon (chain_id=137) | BNB Chain (chain_id=56) | ⚠️ RPC/주소 변경 |
| 결제 토큰 | USDC (6 decimals) | USDT (18 decimals, wei) | ⚠️ 단위 변환 로직 |
| 클라이언트 SDK | py_clob_client | predict-sdk (공식) | ✅ 교체 |
| 주문 방식 | REST CLOB | REST CLOB | ✅ 구조 동일 |
| 실시간 피드 | 없음 (폴링) | WebSocket 지원 | ✅ 오히려 유리 |
| 고래 탐지 | data-api /activity 폴링 | **리더보드 API 미확인** | 🔴 최대 리스크 |
| 정산(Claim) | Polygon 온체인 ERC1155 | BNB Chain 온체인 | ⚠️ 컨트랙트 주소 상이 |
| 유동성 | $415M OI | $14.2M OI | ⚠️ 슬리피지 ~30배 |
| 승인(Approve) | Exchange/NegRiskExchange | Predict 컨트랙트 | ⚠️ 주소 확인 필요 |

---

## 2. 우려 사항 (심각도 순)

### 🔴 HIGH — 고래 리더보드 API 미확인
- **문제**: Polymarket은 data-api.polymarket.com/activity?user=지갑주소 로 특정 고래의 거래만 구독 가능
- **Predict.fun**: 리더보드(predict.fun/points)의 REST API 엔드포인트 미확인
- **대안**: WebSocket으로 ALL 거래 수신 → 크기 기준 필터링 (SIZE >= MIN_WHALE_SIZE_USDT)
  → 폴리마켓보다 오히려 완전한 시장 관측 가능 (더 나을 수 있음)
- **검증 필요**: API 키 받은 후 `/v1/leaderboard` 또는 `/v1/traders` 엔드포인트 존재 여부 확인

### 🔴 HIGH — predict-sdk 실제 인터페이스 미검증
- **문제**: `pip install predict-sdk` 후 실제 클래스/메서드명 확인 필요
- **대안**: predict-sdk 없어도 직접 REST API 호출로 동작하도록 이중 구현
- **검증 필요**: `from predict_sdk import PredictClient` 등 실제 임포트 확인

### 🟡 MEDIUM — 유동성 부족으로 슬리피지 증가
- **문제**: OI $14.2M → 대형 마켓 외 bid-ask spread 10~20% 가능
- **대응**: MIN_WHALE_SIZE_USDT 기준 상향, slippage_bps 여유있게 설정 (200~500bps)
- **현재 설정**: DEFAULT_SLIPPAGE_BPS=300 (3%)

### 🟡 MEDIUM — USDT wei 단위 변환
- **문제**: Polymarket은 USDC 소수점 방식, Predict.fun은 wei (1e18)
- **대응**: `to_wei(amount, 'ether')` web3.py 함수 사용 → 이미 처리됨

### 🟢 LOW — WebSocket 재연결 안정성
- **문제**: WS 연결 끊길 경우 거래 감지 누락
- **대응**: 자동 재연결 로직 (backoff 포함) 구현 완료

---

## 3. 구현 전략

### 고래 탐지 방식 (2-Track)
```
Track A (우선): WebSocket 실시간 감지
  wss://ws.predict.fun/ws 구독
  → 모든 체결 수신 → size >= MIN_WHALE_SIZE_USDT 필터
  → 해당 지갑을 "고래 후보"로 등록 + 이후 행동 추적

Track B (보조): 리더보드 API (확인 후 활성화)
  GET /v1/leaderboard → 상위 트레이더 목록
  → whales.json DB 초기 시딩용
```

---

## 4. 파일 구조

```
predict_trader_bot/
├── core/
│   ├── config.py              # BNB Chain 설정
│   ├── client_wrapper.py      # predict-sdk + REST 래퍼
│   ├── whale_manager.py       # WebSocket 기반 고래 탐지
│   ├── whale_scorer.py        # 성과 스코어링 (폴리마켓 로직 재사용)
│   ├── predict_copy_bot.py    # 메인 봇
│   └── telegram_notifier.py  # 텔레그램 알림 (폴리마켓 동일)
├── data/
├── logs/
├── tasks/
│   └── todo.md
├── .env.example
├── requirements.txt
└── run_bot.py
```

---

## 5. 구현 체크리스트

### Phase 0 — 환경 준비 (즉시)
- [ ] `pip install predict-sdk web3 python-dotenv requests websocket-client` 설치
- [ ] `.env` 파일 생성 (PREDICT_API_KEY, PRIVATE_KEY, WALLET_ADDRESS 입력)
- [ ] predict-sdk 실제 임포트 테스트 (`python -c "import predict_sdk; print(dir(predict_sdk))"`)
- [ ] WebSocket 연결 테스트 (`python -c "..."`)

### Phase 1 — API 검증 (착수 전 필수)
- [ ] `GET /v1/markets` 호출 → 마켓 구조 확인 (conditionId, outcomeIndex 필드명)
- [ ] `GET /v1/markets/{id}/activity` 호출 → 거래 내역 필드명 확인
- [ ] `GET /v1/leaderboard` 또는 유사 엔드포인트 존재 확인
- [ ] WebSocket 메시지 포맷 확인 (subscribe payload, 수신 JSON 구조)
- [ ] `GET /v1/positions` 호출 → 포지션 구조 확인

### Phase 2 — 핵심 기능 구현
- [ ] client_wrapper.py — place_order, get_orderbook, get_positions 동작 확인
- [ ] whale_manager.py — WebSocket 고래 탐지 동작 확인
- [ ] predict_copy_bot.py — PAPER 모드 거래 시뮬레이션
- [ ] 텔레그램 상태 알림 동작 확인

### Phase 3 — 라이브 전환
- [ ] PAPER_TRADING=True 로 1주일 시뮬레이션
- [ ] 슬리피지/유동성 실측치 반영 파라미터 튜닝
- [ ] PAPER_TRADING=False 전환 + 소액($100) 테스트
- [ ] VPS 배포 (screen 방식)

---

## 6. 검증 방법

```bash
# Phase 0 검증
cd predict_trader_bot/core
python -c "from config import config; print(config.PREDICT_API_KEY)"
python -c "import predict_sdk; print('SDK OK')"

# API 연결 테스트
python -c "
import requests
r = requests.get('https://api.predict.fun/v1/markets', headers={'Authorization': 'Bearer YOUR_KEY'}, params={'limit': 1})
print(r.status_code, r.json())
"
```

---

## 완료 기록
- [x] 폴더 구조 생성 (2026-03-17)
- [x] 분석 보고서 작성 (2026-03-17)
- [x] 스켈레톤 파일 생성 (2026-03-17)
