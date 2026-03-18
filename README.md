# Predict.fun 고래 카피 트레이딩 봇

BNB Chain 기반 예측 시장 플랫폼 **Predict.fun**에서 대형 트레이더(고래)의 거래를 실시간으로 감지하고 자동으로 복사 매매를 수행하는 봇이다.

---

## 목차

1. [작동 원리 개요](#1-작동-원리-개요)
2. [시스템 아키텍처](#2-시스템-아키텍처)
3. [핵심 모듈 설명](#3-핵심-모듈-설명)
4. [고래 탐지 메커니즘](#4-고래-탐지-메커니즘)
5. [진입 필터 체계 (7단계)](#5-진입-필터-체계-7단계)
6. [베팅 전략 및 자금 관리](#6-베팅-전략-및-자금-관리)
7. [리스크 관리](#7-리스크-관리)
8. [고래 스코어링 시스템](#8-고래-스코어링-시스템)
9. [페이퍼 트레이딩 모드](#9-페이퍼-트레이딩-모드)
10. [텔레그램 인터페이스](#10-텔레그램-인터페이스)
11. [설치 및 실행](#11-설치-및-실행)
12. [환경 변수 설정](#12-환경-변수-설정)
13. [데이터 파일 구조](#13-데이터-파일-구조)
14. [주요 버그 수정 이력](#14-주요-버그-수정-이력)

---

## 1. 작동 원리 개요

**예측 시장(Prediction Market)** 이란, 특정 사건의 결과에 베팅하는 시장이다. 예를 들어 "Lakers가 Heat를 이길까?"라는 질문에 YES/NO 토큰을 사고파는 방식이다. 결과가 확정되면 승리한 쪽 토큰은 $1(100%)로, 패배한 쪽은 $0(0%)으로 정산된다.

이 봇의 핵심 전략은 다음과 같다:

```
1. 고래($500 이상 대형 거래) 실시간 탐지
        ↓
2. 7단계 필터로 위험 거래 차단
        ↓
3. 필터 통과 시 동일 포지션 복사 매매
        ↓
4. 정산/손절/조기청산 조건 충족 시 자동 청산
```

**핵심 전제**: 대형 트레이더는 일반 투자자보다 정보력과 분석력이 뛰어나다. 그들의 거래를 복사함으로써 수익을 추구한다.

---

## 2. 시스템 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                    run_bot.py (Watchdog)              │
│  크래시 감지 → 5초 후 자동 재시작 (최대 10회/5분)      │
└───────────────────────┬─────────────────────────────┘
                        │ subprocess 실행
                        ▼
┌─────────────────────────────────────────────────────┐
│             predict_copy_bot.py (메인 봇)             │
│                                                      │
│  ┌─────────────┐    ┌──────────────────────────┐    │
│  │whale_manager│    │    메인 루프 (2초 주기)    │    │
│  │(30초 REST   │───▶│  고래 거래 처리           │    │
│  │ 폴링 스레드)│    │  포지션 정산 (60초 주기)  │    │
│  └─────────────┘    └──────────────────────────┘    │
│                              │                       │
│  ┌─────────────┐    ┌────────▼───────────────┐      │
│  │whale_scorer │    │   client_wrapper.py    │      │
│  │(6시간 주기  │    │  Predict.fun API 통신  │      │
│  │ 스코어 갱신)│    │  주문 실행 / 오더북 조회│      │
│  └─────────────┘    └────────────────────────┘      │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │         telegram_notifier.py                  │   │
│  │  텔레그램 알림 발송 + 버튼 명령 수신 (2초 주기)│   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**스레드 구성:**
- `메인 스레드`: 고래 거래 처리 + 포지션 정산
- `whale_manager 스레드`: 30초마다 API 폴링
- `maintenance 스레드`: 6시간마다 고래 스코어 갱신
- `telegram_poll 스레드`: 2초마다 텔레그램 명령 수신

---

## 3. 핵심 모듈 설명

### `run_bot.py` — Watchdog
봇 프로세스를 자식 프로세스로 실행하고 감시한다.
- 봇 크래시 시 5초 후 자동 재시작
- 5분 내 10회 이상 크래시 → 무한루프 방지를 위해 중단
- `.stop_bot` 파일 생성 시 영구 종료
- 텔레그램 ⏹ Stop 버튼도 이 파일을 생성하는 방식

### `core/predict_copy_bot.py` — 메인 봇
봇의 핵심 로직 전체를 담당한다.
- 고래 거래 수신 → 7단계 필터 → 주문 실행
- 포지션 정산 (승/패 판별, 손절, 타임아웃)
- 상태 파일(`state_Predict_PAPER.json`) 저장/복구
- 텔레그램 명령 처리 (Status, Trades, Positions, Restart, Stop)

### `core/whale_manager.py` — 고래 탐지
REST API를 30초마다 폴링하여 대형 거래를 탐지한다.
- `GET /v1/orders/matches?minValueUsdtWei={500 × 1e18}&first=50`
- cursor 기반 페이지네이션으로 이미 처리한 거래 중복 방지
- 첫 폴링(워밍업): 50건 dedup 등록만 하고 처리 스킵 (봇 시작 직전 거래 복사 방지)
- 탐지된 고래를 `whales_predict.json` DB에 자동 등록

### `core/whale_scorer.py` — 고래 스코어링
고래의 거래 이력을 분석하여 신뢰도 점수(0~1)를 계산한다.
- 수익률(ROI) 40% + 승률(Win Rate) 40% + 거래빈도 20% 가중 합산
- 최근 30일 활동 부족 시 recency_penalty 0.8 적용
- 6시간마다 전체 DB 갱신

### `core/client_wrapper.py` — API 클라이언트
Predict.fun REST API 통신을 담당한다.
- 마켓 정보 조회, 오더북 조회, 주문 실행, 포지션 조회
- PAPER 모드: 실제 오더북 + 슬리피지 + 수수료를 시뮬레이션
- LIVE 모드: predict-sdk의 OrderBuilder로 BNB Chain 온체인 트랜잭션 실행

### `core/config.py` — 설정
`.env` 파일에서 환경 변수를 읽어 전역 설정 객체를 제공한다.

### `core/telegram_notifier.py` — 텔레그램 알림
진입/청산/상태 알림을 텔레그램으로 발송하고, Reply Keyboard 버튼 명령을 수신한다.

### `reset_paper.py` — 초기화 스크립트
페이퍼 트레이딩 상태를 완전히 초기화한다.
- 뱅크롤/포지션/통계 리셋
- 거래 기록 삭제
- 고래 스코어 0 초기화 (주소/거래량 이력은 보존)

---

## 4. 고래 탐지 메커니즘

### API 폴링 방식

```
30초마다 실행:
GET /v1/orders/matches
  ?minValueUsdtWei=500000000000000000000  (= $500 × 10^18)
  &first=50
  &after={cursor}  ← 이전 폴링 커서 (없으면 최신 50건)

응답:
{
  "cursor": "abc123",
  "data": [
    {
      "amountFilled": "750000000000000000000",  ← $750 (wei 단위)
      "executedAt": "2026-03-18T17:56:00Z",
      "makers": [{
        "signer": "0xfe6d...",       ← 고래 지갑 주소
        "price": "510000000000000000", ← 0.51 (wei)
        "quoteType": "Bid",           ← Bid=BUY, Ask=SELL
        "outcome": { "name": "Lakers", "onChainId": "..." }
      }],
      "market": { "id": 65291, "question": "Lakers vs. Heat", ... }
    }
  ]
}
```

### 워밍업 로직 (중요)
봇 시작 시 첫 폴링은 `dry_run=True`로 실행한다. 이는 최근 50건을 "이미 처리한 것"으로 등록만 하고 실제 거래는 하지 않는 것이다. 이 로직이 없으면 봇 재시작 시마다 직전 30초~수 분 치 고래 거래가 한꺼번에 들어와 뱅크롤을 순식간에 소진한다.

### 중복 방지
- cursor 기반: 다음 폴링에서 이미 처리한 거래 건너뜀
- dedup_key: `executedAt:address:marketId` 조합으로 2차 방어
- `_seen_market_signals`: 동일 마켓+방향 시그널 30분 TTL로 중복 차단

---

## 5. 진입 필터 체계 (7단계)

고래 거래가 탐지되면 아래 7단계 필터를 순서대로 통과해야만 실제 진입이 이루어진다.

```
고래 거래 감지
      │
      ▼
Filter 1: BUY만 복사 (SELL → MIRROR EXIT 검토로 분기)
      │
      ▼
Filter 2: 포지션 한도 (현재 포지션 수 < MAX_POSITIONS=30)
      │
      ▼
Filter 3: 가격 범위 (MIN_PRICE=0.10 ~ MAX_PRICE=0.85)
      │
      ▼
Filter 3.5: 0.50~0.60 구간 차단 (수익성 불명확 구간)
      │
      ▼
Filter 4: 고래 스코어
  ├─ DB에 있고 score > 0이면서 < 0.2 → SKIP (저품질)
  ├─ DB에 있고 score = 0 → SKIP (스코어링 불가)
  └─ DB에 없는 신규 고래: 거래 크기 < $1000 → SKIP
      │
      ▼
Filter 5: 동일 마켓 중복 진입 차단
  ├─ 이미 같은 마켓 포지션 보유 중 → SKIP
  └─ 같은 마켓+방향 시그널 30분 내 처리됨 → SKIP
      │
      ▼
마켓 API 조회 + 정산 완료 마켓 차단 (_is_resolved 검사)
      │
      ▼
Filter 6: 오더북 품질 검사
  ├─ 스프레드 > 15% → SKIP (유동성 부족)
  ├─ 현재 ask > MAX_PRICE → SKIP (슬리피지 후 초과 예방)
  └─ 현재 ask vs 고래 가격 25% 이상 이탈 → SKIP (stale trade)
      │
      ▼
✅ 진입 실행
```

### 각 필터 상세 설명

**Filter 3.5 (0.50~0.60 차단)**
가격이 0.50~0.60인 마켓은 사실상 동전 던지기에 가깝다. 어느 쪽도 압도적 우위가 없어 엣지를 찾기 어렵다.

**Filter 4 (고래 스코어)**
고래 DB에 스코어가 계산된 트레이더만 복사한다. 현재는 DB 구축 초기라 대부분 score=0이므로, 스코어링 데이터가 쌓이기 전까지 신규 고래는 $1,000 이상 거래만 허용한다.

**Filter 6 (stale trade 방어)**
고래가 0.72에 베팅했는데 지금 시장 가격이 0.27이라면, 시장이 새로운 부정적 정보를 이미 반영한 것이다. 이런 오래된 시그널을 따라가면 손실로 이어진다.

**`_is_resolved()` 검사**
API 응답의 `status`, `tradingStatus`, `resolution` 필드를 종합해 이미 정산 완료된 마켓인지 판별한다. 대소문자 정규화(`upper()`)로 "RESOLVED", "CLOSED" 모두 처리한다.

---

## 6. 베팅 전략 및 자금 관리

### 베팅 금액 계산
```python
portfolio = 가용잔고(bankroll) + 보유포지션 현재가치
bet_size  = portfolio / BET_DIVISOR  # BET_DIVISOR=40 → 포트폴리오의 2.5%
```

예시: 뱅크롤 $5,000, 포지션 없음 → bet_size = $5,000 / 40 = $125

포지션이 늘어날수록 portfolio가 줄어들어 bet_size도 자동으로 줄어드는 자연스러운 켈리 기준 근사 방식이다.

### 주문 실행 흐름 (BUY)
```
1. place_order(BUY, price=고래가, size=$125)
2. 오더북 ask[0] 조회 → 실제 체결가 계산
3. 슬리피지 적용: actual_price = ask[0] × (1 + slippage_bps/10000)
   (DEFAULT_SLIPPAGE_BPS=300 → 3%)
4. 수수료 차감: fee = size × feeRateBps/10000
   (feeRateBps=200 → 2%)
5. shares = (size - fee) / actual_price
6. 포지션 등록: { marketId, entry_price, shares, size_usdc, ... }
7. bankroll -= size
```

---

## 7. 리스크 관리

### 손절 (Stop Loss)
60초마다 모든 포지션의 현재가를 조회한다.
```
drop = (entry_price - current_price) / entry_price
if drop >= STOP_LOSS_PCT (0.40 = 40%): 손절 청산
```
예시: $0.70에 진입 → $0.42 이하로 하락 시 손절

오더북 조회 실패(`get_best_price()` = None) 시에는 마지막으로 알려진 가격(`current_price`)으로 손절 여부를 재판단한다. 과거에는 조회 실패 시 손절을 완전히 스킵해 죽은 포지션이 영구 유지되는 버그가 있었다.

### 타임아웃
7일 이상 미정산 포지션은 마지막 알려진 가격으로 강제 청산한다.

### MIRROR EXIT (조기 청산)
어떤 고래든 우리가 포지션을 보유 중인 마켓에서 SELL 신호를 보내면 조기 청산을 검토한다.
```
현재가 >= 진입가 (이익/본전 상태) → 청산 실행 (수익 실현)
현재가 < 진입가 (손실 상태) → 유지 (자연 정산 대기)
```
손실 상태에서 청산하면 손실만 확정되므로 정산까지 기다리는 것이 합리적이다.

### 정산 판별 로직
마켓이 resolved 상태가 되면 resolution 필드로 결과를 확인한다:
```python
resolution.name in ("YES", "UP", "ABOVE", "OVER") → WIN (price=1.0)
resolution.name in ("NO", "DOWN", "BELOW", "UNDER") → LOSS (price=0.0)
없으면 현재가 >= 0.95 → WIN, else LOSS (fallback)
```

### PnL 계산
```
WIN:  pnl = shares × 1.0 - size_usdc  (실제 수령액 - 투자금)
LOSS: pnl = -size_usdc                 (전액 손실)
STOP_LOSS/MIRROR_EXIT/TIMEOUT:
  pnl = (actual_sell_price - entry_price) × shares - sell_fee
```

---

## 8. 고래 스코어링 시스템

### 스코어 계산 공식
```
score = (profit_score × 0.40) + (wr_score × 0.40) + (freq_score × 0.20)
        × recency_penalty

profit_score  = min(ROI / 100, 1.0)
wr_score      = (win_rate - 50) / 50  (승률 50% 이하면 0)
freq_score    = min(최근30일 거래수 / 20, 1.0)
recency_penalty = 1.0 (최근30일 20건 이상) | 0.8 (미만)
```

### 진입 기준
- score >= 0.2: 진입 허용
- score > 0 이면서 < 0.2: 차단 (저품질 고래)
- score = 0: 차단 (스코어링 데이터 없음)

### 현재 제한사항
whale_manager가 DB에 저장하는 거래 기록에는 `pnl`, `action` 필드가 없다. 따라서 whale_scorer가 스코어를 계산하려면 `/v1/traders/{address}/activity` API에서 실제 수익 데이터를 가져와야 한다. 이 데이터가 쌓이기 전까지는 대부분 score=0이 유지된다.

### 고래 DB 구조 (`data/whales_predict.json`)
```json
{
  "0xfe6d...": {
    "address": "0xfe6d...",
    "name": "whale_0xfe6d",
    "total_trades": 15,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0,
    "total_volume": 12500.0,
    "score": 0.0,
    "trades": [ ... 최근 50건 ... ]
  }
}
```

---

## 9. 페이퍼 트레이딩 모드

`PAPER_TRADING=True`로 설정 시 실제 블록체인 트랜잭션 없이 동일한 로직을 시뮬레이션한다.

### 현실성 구현 항목

| 항목 | 시뮬레이션 방법 |
|------|---------------|
| BUY 체결가 | 실제 오더북 ask[0] + 슬리피지 3% |
| BUY 수수료 | feeRateBps=200 (2%) 적용 |
| shares 계산 | `(size - fee) / exec_price` (수수료 차감 후) |
| SELL 체결가 | 실제 오더북 bid[0] - 슬리피지 3% |
| SELL 수수료 | feeRateBps=200 (2%) 적용 |
| WIN 정산 | $1.00 고정 (온체인 정산 동일) |
| LOSS 정산 | $0.00 고정 |
| 스프레드 필터 | 실제 오더북 기반 |

### 상태 파일
`data/state_Predict_PAPER.json`에 뱅크롤, 포지션, 통계를 저장한다. 봇 재시작 시 자동 복구된다.

---

## 10. 텔레그램 인터페이스

봇 시작 시 텔레그램 채팅에 고정 버튼(Reply Keyboard)이 나타난다.

| 버튼 | 기능 |
|------|------|
| 📊 Status | 포트폴리오, 잔고, PnL, 승률, 포지션 수, ROI, APR |
| 📋 Trades | 최근 10건 정산 거래 (WIN/LOSS/STOP_LOSS만 표시) |
| 📌 Positions | 현재 보유 포지션 전체 + 미실현 손익 |
| 🔄 Restart | 봇 재시작 (30초 내 재확인 필요) |
| ⏹ Stop | 영구 종료 (.stop_bot 파일 생성) |

### 알림 종류
- 🟢 **진입**: 마켓명, 고래명, 체결가, 수수료, 슬리피지 정보
- ✅ **WIN / ❌ LOSS**: 마켓명, PnL, 잔고
- 🔴 **STOP_LOSS**: 하락률, PnL, 잔고

---

## 11. 설치 및 실행

### 요구사항
- Python 3.10+
- pip 패키지: `requirements.txt` 참고

### 설치
```bash
git clone <repository_url>
cd predict_trader_bot
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # .env 파일 생성 후 설정값 입력
```

### 실행 (Watchdog 포함, 권장)
```bash
python run_bot.py
```

### 봇만 직접 실행
```bash
python core/predict_copy_bot.py
```

### 페이퍼 초기화 (뱅크롤 리셋)
```bash
python reset_paper.py
```

### 영구 종료
```bash
touch .stop_bot
# 또는 텔레그램 ⏹ Stop 버튼
```

---

## 12. 환경 변수 설정

`.env` 파일에 아래 항목을 설정한다.

```env
# === Predict.fun API ===
PREDICT_API_KEY=your_api_key_here
PREDICT_API_BASE=https://api.predict.fun
PREDICT_WS_URL=wss://ws.predict.fun/ws

# === BNB Chain 지갑 (실거래 시 필수) ===
PRIVATE_KEY=0x...
WALLET_ADDRESS=0x...

# === BSC RPC (기본값 사용 가능) ===
BSC_RPC_URL=https://bsc-dataseed1.binance.org

# === 운영 설정 ===
INITIAL_BANKROLL=5000.0       # 시작 뱅크롤 (USDT)
PAPER_TRADING=True            # True=페이퍼, False=실거래
DEBUG_MODE=True

# === 고래 설정 ===
MAX_POSITIONS=30              # 최대 동시 포지션 수
BET_DIVISOR=40                # 포트폴리오 ÷ 40 = 2.5%씩 베팅
STOP_LOSS_PCT=0.40            # 40% 하락 시 손절
MIN_WHALE_SIZE_USDT=500.0     # 고래 최소 거래액
MIN_PRICE=0.10                # 최소 진입 가격 (극저확률 차단)
MAX_PRICE=0.85                # 최대 진입 가격 (정산 임박 차단)
DEFAULT_SLIPPAGE_BPS=300      # 슬리피지 3%

# === 고래 스코어링 기준 ===
MIN_WIN_RATE=55.0
MIN_TRADES=5

# === 텔레그램 ===
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

---

## 13. 데이터 파일 구조

```
predict_trader_bot/
├── core/
│   ├── predict_copy_bot.py    # 메인 봇
│   ├── whale_manager.py       # 고래 탐지
│   ├── whale_scorer.py        # 고래 스코어링
│   ├── client_wrapper.py      # API 클라이언트
│   ├── config.py              # 설정
│   └── telegram_notifier.py   # 텔레그램
├── data/
│   ├── state_Predict_PAPER.json    # 봇 상태 (뱅크롤, 포지션, 통계)
│   ├── trade_history_PAPER.jsonl   # 거래 기록 (한 줄 = 1건)
│   └── whales_predict.json         # 고래 DB (주소별 정보/스코어)
├── logs/
│   ├── market_debug.jsonl     # 마켓 API 응답 디버그 로그
│   └── match_debug.jsonl      # 고래 체결 raw 응답 (방향 검증용, 30건)
├── run_bot.py                 # Watchdog 실행기
├── reset_paper.py             # 페이퍼 초기화
└── requirements.txt
```

### 거래 기록 형식 (`trade_history_PAPER.jsonl`)
```jsonl
{"action": "OPEN", "pos_key": "65291_0xfe6d..._1710780000", "price": 0.525, "pnl": 0, "question": "Lakers vs. Heat", "bankroll_after": 4875.0, "timestamp": "2026-03-18T17:56:00"}
{"action": "WIN", "pos_key": "65291_0xfe6d..._1710780000", "price": 1.0, "pnl": 118.5, "question": "Lakers vs. Heat", "bankroll_after": 5118.5, "timestamp": "2026-03-19T02:15:00"}
```

---

## 14. 주요 버그 수정 이력

### 핵심 버그 (뱅크롤 $3000 → $0.88 소진 원인)

| 버그 | 원인 | 수정 |
|------|------|------|
| Resolved 마켓 필터 미작동 | `status == "resolved"` 소문자 비교 실패 (API는 대문자 반환) | `_is_resolved()` 헬퍼로 `.upper()` 정규화 |
| 백로그 차단 비활성화 | `_load_state()` 실행 시 `_startup_time = None`으로 덮어쓰기 | 해당 코드 제거 |
| 워밍업 cursor 미설정 | 첫 폴링 후 cursor를 저장하지 않아 2번째 폴링도 같은 50건 재처리 | 워밍업 완료 시 cursor 저장 |

### 추가 개선 사항

| 개선 | 내용 |
|------|------|
| 가격 이탈 방어 | 현재 오더북 vs 고래 가격 25% 이상 차이 → 차단 (stale trade 방지) |
| 스프레드 필터 | bid-ask 스프레드 15% 초과 → 차단 (유동성 부족 마켓 방지) |
| MAX_PRICE 슬리피지 방어 | 현재 ask > MAX_PRICE → 슬리피지 후 초과 예방 |
| 0.50~0.60 구간 차단 | 수익성 불명확 구간 필터링 |
| score=0 완전 차단 | 스코어링 데이터 없는 고래 진입 불허 |
| MIRROR EXIT 확장 | 어떤 고래의 SELL이든 해당 마켓 포지션 청산 검토 |
| 손절 None 방어 | 오더북 조회 실패 시 마지막 가격으로 손절 재시도 |
| WIN/LOSS PnL 정확화 | `shares × 1.0 - size_usdc`로 수수료 포함 정확한 수익 계산 |
| Paper SELL 현실화 | 실제 오더북 bid - 슬리피지 - 매도수수료 반영 |
| shares 과대계산 수정 | `(size - fee) / exec_price`로 수수료 차감 후 정확한 shares 계산 |
| 수수료 기본값 수정 | 150bps(1.5%) → 200bps(2%) (실제 API 기준) |
| maker/taker 방향 디버그 | `logs/match_debug.jsonl`에 raw 응답 30건 저장 (방향 검증용) |

---

## 주의사항

- **이 봇은 투자를 권유하지 않는다.** 모든 투자의 책임은 본인에게 있다.
- `PRIVATE_KEY`는 절대 코드에 직접 입력하거나 외부에 노출하지 말 것.
- 실거래(`PAPER_TRADING=False`) 전 반드시 충분한 페이퍼 트레이딩 검증을 거칠 것.
- 예측 시장은 변동성이 매우 높으며 원금 손실 가능성이 있다.
