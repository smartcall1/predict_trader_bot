# Predict.fun 고래 카피 트레이딩 봇

BNB Chain 기반 예측시장 **Predict.fun**에서 대형 트레이더(고래)의 거래를 실시간 감지하고 **동방향 복사 매매**를 자동으로 수행하는 봇. Polymarket 고래 봇의 모든 로직을 BNB Chain으로 이식한 집대성 버전.

---

## 목차

1. [전략 개요](#1-전략-개요)
2. [시스템 아키텍처](#2-시스템-아키텍처)
3. [파일 구조](#3-파일-구조)
4. [핵심 모듈 상세](#4-핵심-모듈-상세)
   - 4-1. run_bot.py — Watchdog
   - 4-2. predict_copy_bot.py — 메인 봇
   - 4-3. whale_manager.py — 고래 탐지 + GraphQL 발굴
   - 4-4. whale_scorer.py — 신뢰도 스코어링
   - 4-5. client_wrapper.py — API 클라이언트
   - 4-6. config.py — 설정
   - 4-7. telegram_notifier.py — 알림
5. [고래 탐지 메커니즘](#5-고래-탐지-메커니즘)
6. [진입 필터 체계 (7단계)](#6-진입-필터-체계-7단계)
7. [동방향 전략 + Overlap 배율](#7-동방향-전략--overlap-배율)
8. [베팅 전략 및 자금 관리](#8-베팅-전략-및-자금-관리)
9. [리스크 관리](#9-리스크-관리)
10. [고래 스코어링 시스템](#10-고래-스코어링-시스템)
11. [주문 실행 흐름 (LIVE)](#11-주문-실행-흐름-live)
12. [페이퍼 트레이딩 모드](#12-페이퍼-트레이딩-모드)
13. [텔레그램 인터페이스](#13-텔레그램-인터페이스)
14. [데이터 파일 구조](#14-데이터-파일-구조)
15. [설치 및 실행](#15-설치-및-실행)
16. [환경 변수 설정 (.env)](#16-환경-변수-설정-env)
17. [주요 버그 수정 이력](#17-주요-버그-수정-이력)
18. [운영 현황 및 로드맵](#18-운영-현황-및-로드맵)
19. [주의사항](#19-주의사항)

---

## 1. 전략 개요

### 예측시장이란?

특정 사건의 결과에 베팅하는 금융 시장. 예를 들어 "Lakers가 Heat를 이길까?"에 YES/NO 토큰을 사고팔며, 결과 확정 시 승리 토큰은 $1.00(100%), 패배 토큰은 $0.00(0%)으로 정산된다.

### 봇의 핵심 전략: 동방향 카피 트레이딩

```
GraphQL 리더보드에서 pnlUsd 상위 고래 자동 발굴
              ↓
REST 폴링으로 고래 대형 체결 실시간 감지
              ↓
고래가 YES@0.35에 베팅
              ↓
우리도 동일하게 YES@0.35에 베팅 (동방향 복사)
              ↓
7단계 필터 + Overlap 배율로 위험 거래 차단 / 강한 시그널 증폭
              ↓
정산/손절/조기청산 조건 시 자동 청산
```

**전략 근거**: 검증된 고수익 고래(GraphQL pnlUsd 기준)가 베팅한 방향을 그대로 따라간다. 여러 상위 고래가 동일 마켓에 포지션을 보유 중이면(Overlap) 시그널 강도가 높다고 판단해 베팅금을 자동 증폭한다.

---

## 2. 시스템 아키텍처

```
┌───────────────────────────────────────────────────────┐
│                 run_bot.py (Watchdog)                  │
│   크래시 감지 → 5초 후 자동 재시작 (최대 10회/5분)      │
│   .stop_bot 파일 감지 시 영구 종료                      │
└──────────────────────┬────────────────────────────────┘
                       │ subprocess.Popen
                       ▼
┌───────────────────────────────────────────────────────┐
│           predict_copy_bot.py (메인 봇)                │
│                                                       │
│  ┌──────────────────┐   ┌───────────────────────┐    │
│  │  whale_manager   │   │    메인 루프 (2초)     │    │
│  │  REST 폴링 스레드│──▶│  고래 거래 처리        │    │
│  │  (30초 간격)     │   │  포지션 정산 (60초)    │    │
│  ├──────────────────┤   └───────────────────────┘    │
│  │  GraphQL 리더보드│                                 │
│  │  갱신 스레드     │                                 │
│  │  (6시간 주기)    │                                 │
│  │  · pnlUsd 시드  │                                 │
│  │  · Overlap 맵   │                                 │
│  └──────────────────┘                                 │
│                                                       │
│  ┌──────────────────┐   ┌───────────────────────┐    │
│  │  whale_scorer    │   │   client_wrapper.py   │    │
│  │  6시간 갱신 스레드│   │   Predict.fun REST API│    │
│  │  GraphQL 포지션  │   │   주문/오더북/포지션  │    │
│  │  카테고리/델타   │   └───────────────────────┘    │
│  └──────────────────┘                                 │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │         telegram_notifier.py                    │  │
│  │  알림 발송 + 버튼 명령 수신 (2초 폴링 스레드)   │  │
│  └────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────┘
```

**스레드 구성**

| 스레드 | 주기 | 역할 |
|--------|------|------|
| 메인 | 2초 | 고래 거래 처리 + 60초마다 포지션 정산 |
| whale_manager REST | 30초 | REST API 폴링, 고래 DB 업데이트 |
| whale_manager GraphQL | 6시간 | 리더보드 시드, Overlap 포지션 맵 갱신 |
| maintenance | 5분 체크 / 6시간 실행 | 고래 스코어 갱신, 상태 저장 |
| telegram_poll | 2초 | 텔레그램 명령 수신 |

---

## 3. 파일 구조

```
predict_trader_bot/
├── core/
│   ├── predict_copy_bot.py    # 메인 봇 — 필터, 주문, 정산, 텔레그램
│   ├── whale_manager.py       # 고래 탐지 — REST 폴링 + GraphQL 리더보드
│   ├── whale_scorer.py        # 고래 스코어링 — GraphQL positions 기반
│   ├── client_wrapper.py      # Predict.fun API — 주문/오더북/잔고
│   ├── config.py              # 설정 — .env 로드, 전역 상수
│   └── telegram_notifier.py   # 텔레그램 — 알림 발송, 명령 수신
├── data/
│   ├── state_Predict_PAPER.json      # 봇 상태 (뱅크롤, 포지션, 통계)
│   ├── trade_history_PAPER.jsonl     # 거래 기록 (1줄=1건)
│   ├── whales_predict.json           # 고래 DB (주소별 이력/스코어)
│   ├── leaderboard_snapshot.json     # GraphQL 리더보드 스냅샷 (6h 델타용)
│   └── overlap_positions.json        # 상위 고래 현재 포지션 중복 맵
├── logs/
│   ├── market_debug.jsonl     # 마켓 API 응답 필드 디버그
│   └── match_debug.jsonl      # 고래 체결 raw 응답 (최초 30건)
├── leaderboard.py             # GraphQL pnlUsd 리더보드 독립 실행 도구
├── run_bot.py                 # Watchdog 실행기
├── reset_paper.py             # 페이퍼 상태 완전 초기화
├── requirements.txt
└── .env.example
```

---

## 4. 핵심 모듈 상세

### 4-1. `run_bot.py` — Watchdog

봇 프로세스를 자식 프로세스로 실행하고 감시한다.

```python
MAX_CRASHES  = 10    # 임계치
CRASH_WINDOW = 300   # 5분 내 10회 크래시 → 중단
```

**동작 흐름**:
1. `.stop_bot` 파일 존재 여부 확인 → 있으면 영구 종료
2. `core/predict_copy_bot.py`를 subprocess로 실행
3. 프로세스 종료 감지 → 5초 대기 후 재시작
4. 5분 내 10회 이상 크래시 → 비정상 종료로 판단, 루프 중단

---

### 4-2. `predict_copy_bot.py` — 메인 봇

봇의 모든 핵심 로직을 담당한다.

**초기화 시 수행 작업**:
```python
config.ensure_dirs()           # data/, logs/ 디렉토리 생성
self._load_state()             # 이전 상태 복구 (봇 재시작 연속성)
self.client = PredictFunClient()
self.scorer = WhaleScorer()
self.watcher = run_manager()   # 고래 탐지 + GraphQL 리더보드 시작
# 백그라운드 스레드 2개 시작 (_maintenance_loop, _telegram_poll_loop)
```

**주요 내부 상태**:
```python
self.seen_txs: OrderedDict          # 트랜잭션 중복 방어 (최대 5000개)
self._seen_market_signals: dict     # 마켓+방향 15분 TTL 시그널 캐시
self._overlap_cache: dict           # overlap_positions.json 1시간 TTL 캐시
self.positions: dict                # 활성 포지션 (pos_key → pos_dict)
self._position_lock: Lock           # 포지션 동시 접근 방어
self.bankroll: float                # 가용 현금 잔고
self.peak_bankroll: float           # 최고 잔고 (Max Drawdown 계산용)
self.stats: dict                    # 누적 통계 (총베팅/승/패/PnL)
```

---

### 4-3. `whale_manager.py` — 고래 탐지 + GraphQL 발굴

REST API 30초 폴링 + GraphQL 리더보드 6시간 갱신으로 고래를 탐지하고 DB를 관리한다.

**고래 DB 파일 안전 저장** (atomic write):
```
write → .tmp 파일
rename .tmp → .json  (원자적 교체 — 손상 방지)
이전 .json → .bak    (백업 유지)
손상 감지 시 → .bak로 자동 복구
```

**GraphQL 리더보드 발굴** (`fetch_graphql_leaderboard`):
```python
# https://graphql.predict.fun/graphql
# leaderboard → pnlUsd 기준 재정렬 (포인트 순위 우회)
# pnl >= $2,000 상위 50명 자동 시드 → whales_predict.json
# 기존 고래: leaderboard_pnl, total_volume 최신화
```

포인트 리더보드는 실제 수익자와 괴리가 크다. GraphQL `pnlUsd` 재정렬로 숨겨진 고수익 고래를 자동 발굴한다.

**Overlap 포지션 감지** (`get_overlap_positions`):
```python
# 상위 20명 현재 미정산 포지션 조회
# 동일 market_id에 2명+ → overlap_positions.json 저장
# 봇 베팅금 배율 계산에 활용
```

**`_update_whale_db` — 고래 신규 등록/업데이트**:
```python
db[address] = {
    "address":       "0xfe6d...",
    "name":          "whale_0xfe6d",
    "total_trades":  15,
    "wins": 0, "losses": 0,
    "total_pnl":     0.0,
    "total_volume":  12500.0,
    "last_seen":     1710780000,
    "score":         0.0,
    "leaderboard_pnl": 38400.0,   # GraphQL 리더보드 최신 PnL
    "trades":        [...최근 50건...]
}
```

---

### 4-4. `whale_scorer.py` — 신뢰도 스코어링

GraphQL `positions(isResolved: true)` 기반으로 고래 신뢰도 점수(0.0~1.0)를 계산한다.

**스코어 공식**:

```
raw_score = (ROI점수 × 0.40) + (승률점수 × 0.40) + (빈도점수 × 0.20)
composite = raw_score × confidence × recency_penalty × activity_penalty × (1 + delta_bonus)
```

| 항목 | 계산식 | 설명 |
|------|--------|------|
| **ROI점수** | `log1p(ROI/100) / log(3)` | 로그 스케일, 200% ROI = 만점 1.0 |
| **승률점수** | `(win_rate - 65) / 25` | 65% 이하 0점, 90% 이상 만점 |
| **빈도점수** | `min(정산거래수 / 50, 1.0)` | 50건 기준 만점 |
| **confidence** | `min(정산거래수 / 20, 1.0)` | 소샘플 비례 할인 |
| **recency_penalty** | 4단계 (최근 WIN율 기준) | 85%↑:1.0 / 75~85%:0.75 / 65~75%:0.5 / 65%↓:0.25 |
| **activity_penalty** | 5단계 (비활동 일수 기준) | 3일↓:1.0 / ~7일:0.9 / ~14일:0.7 / 14일↑:0.5 |
| **delta_bonus** | 스냅샷 6h PnL 변화량 기반 | +0% ~ +30% (최근 활발한 고래 가중치) |

**카테고리별 승률 추적**:
```python
classify_market(question) → "sports" / "crypto" / "politics" / "other"
# 키워드 기반 분류 → 카테고리별 wins/losses/roi 별도 집계
```

**헷징/MM 차단 필터**:
```python
if win_rate >= 60% AND roi < 5% AND total_trades >= 20:
    score = 0   # 고승률+저ROI = 방향성 없는 마켓메이킹 → 차단
```

**Pruning** (6시간 갱신 시):
```python
score < 0.3 AND last_seen > 14일 → DB에서 영구 삭제
```

**status 자동 판정**:
- `score >= 0.6` → `"active"`
- `score < 0.45` → `"inactive"`
- 그 사이 → 유지 (그레이존)

---

### 4-5. `client_wrapper.py` — API 클라이언트

Predict.fun REST API 및 predict-sdk 기반 온체인 주문 실행을 담당한다.

**초기화**:
```python
# PAPER 모드: HTTP 세션만 초기화 (SDK 불필요)
# LIVE 모드 + PRIVATE_KEY 존재 시: Web3 + OrderBuilder 초기화
#   RPC 연결 실패 시 config.BSC_RPC_URLS 순서로 자동 fallback
```

**오더북 응답 파싱** (다양한 API 포맷 대응):
```python
# [[price, qty], ...]  형식
# [{"price": p, "size": s}, ...] 형식
# 두 형식 모두 Book(DepthLevel) 객체로 변환
```

**USDT 잔고 조회** (이중 fallback):
```python
1. SDK: builder.balance_of(ADDRESSES[CHAIN_ID].USDT)
2. Web3: contract.functions.balanceOf(wallet).call()
```

---

### 4-6. `config.py` — 설정

프로젝트 루트의 `.env` 파일을 자동 탐지하여 로드한다. `core/` 하위에서 실행해도 `../` 기준으로 올바르게 찾는다.

**BSC RPC 다중 Fallback**:
```python
BSC_RPC_URLS = [
    env("BSC_RPC_URL"),               # .env 우선
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed3.binance.org",
    "https://bsc.publicnode.com",
]
```

---

### 4-7. `telegram_notifier.py` — 알림

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 미설정 시 자동 비활성화 (에러 없이 무음 처리).

---

## 5. 고래 탐지 메커니즘

### REST 폴링 (30초 간격)

```
GET /v1/orders/matches
  ?minValueUsdtWei=200000000000000000000   ($200 × 10^18, PAPER 완화값)
  &first=50
```

**응답 구조**:
```json
{
  "cursor": "abc123",
  "data": [{
    "amountFilled": "750000000000000000000",
    "executedAt": "2026-03-18T17:56:00Z",
    "makers": [{
      "signer": "0xfe6d...",
      "price":  "510000000000000000",
      "quoteType": "Bid",
      "outcome": { "name": "Lakers", "onChainId": "0x123..." }
    }],
    "market": { "id": 65291, "question": "Lakers vs Heat?", ... }
  }]
}
```

**방향 판별**:
```python
quoteType == "Bid"  → BUY (고래가 매수)
quoteType == "Ask"  → SELL (고래가 매도)
```

### GraphQL 리더보드 발굴 (6시간 주기)

```graphql
query Leaderboard($cursor: String) {
  leaderboard(pagination: {first: 50, after: $cursor}) {
    edges {
      node {
        account {
          address
          statistics { pnlUsd volumeUsd marketsCount }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

- 최대 20페이지(1,000명) 수집 → `pnlUsd` 내림차순 재정렬
- `pnl >= $2,000` 상위 50명을 고래 DB에 자동 시드
- `leaderboard_snapshot.json` 저장 → 스코어러 델타 계산용

### Overlap 포지션 감지 (리더보드 갱신 직후)

```graphql
query AccountPositions($address: Address!) {
  account(address: $address) {
    positions(filter: {isResolved: false}, pagination: {first: 50}) {
      edges {
        node {
          market { id question }
          outcome { name onChainId }
          shares
          averageBuyPriceUsd
        }
      }
    }
  }
}
```

- 상위 20명 현재 미정산 포지션 조회
- 동일 `market_id`에 2명 이상 → `overlap_positions.json` 저장
- 봇이 해당 마켓 진입 시 베팅금 배율 자동 적용

### 워밍업 — 백로그 차단

```python
첫 폴링: dry_run=True
  → 최근 50건을 _seen_order_ids에 등록만
  → 실제 처리/콜백 없음
  → 이후 폴링부터 신규 거래만 처리
```

봇 재시작 시 직전 수분치 고래 거래가 한꺼번에 밀려오는 현상을 방지한다.

### 중복 방지 3중 방어

```
1. seen_txs (메인 봇): transactionHash 기준 (5000개 캐시)
2. _seen_order_ids (whale_manager): executedAt:addr:marketId 조합 (10000개 캐시)
3. _seen_market_signals (메인 봇): marketId:side 15분 TTL
```

---

## 6. 진입 필터 체계 (7단계)

```
고래 거래 감지
      │
      ▼ Filter 1: BUY만 복사
  SELL 감지 → MIRROR EXIT 검토로 분기
      │
      ▼ Filter 2: 포지션 한도
  len(positions) >= MAX_POSITIONS(30) → SKIP
      │
      ▼ Filter 3: 가격 범위
  price < MIN_PRICE(0.10) or > MAX_PRICE(0.85) → SKIP
      │
      ▼ Filter 3.5: 0.50~0.60 구간 차단
  동전 던지기 수준 — 엣지 불명확 → SKIP
      │
      ▼ Filter 4: 고래 스코어
  DB 있음 + 0 < score < 0.2 → SKIP (저품질)
  DB 있음 + score = 0 + vol < $1,000 → SKIP (데이터 불충분, PAPER 완화값)
  DB 없음 + size_usdt < MIN_WHALE_SIZE → SKIP (신규 무명 고래)
      │
      ▼ Filter 5: 동일 마켓 중복 진입 차단
  [A] 같은 marketId 포지션 보유 중 → SKIP
  [B] 같은 marketId+side 15분 내 처리됨 → SKIP
      │
      ▼ 마켓 API 검증
  _is_resolved() → 정산 완료 마켓 → SKIP
      │
      ▼ Filter 6: 오더북 품질 검사
  spread > 15% → 유동성 부족 → SKIP
  current ask > MAX_PRICE → 슬리피지 후 초과 예방 → SKIP
  |current_ask - whale_price| / whale_price > 25% → stale trade → SKIP
      │
      ▼
  ✅ 동방향 진입 실행 (Overlap 배율 적용)
```

### `_is_resolved()` — 정산 판별

API 응답 필드명 불일치(대소문자) 방어:
```python
market.get("resolved") == True          → resolved
market.get("resolution") is not None    → resolved
market.get("status", "").upper() in ("RESOLVED", "CLOSED") → resolved
market.get("tradingStatus", "").upper() in ("CLOSED", "RESOLVED") → resolved
```

---

## 7. 동방향 전략 + Overlap 배율

### 동방향 복사

```python
고래: BUY YES@0.35
봇:   BUY YES@0.35  ← 동일 outcome, 동일 방향 복사
```

고래가 BUY한 `outcome_name`, `token_id`, `price`를 그대로 사용한다. 별도 반대편 탐색 없음.

### Overlap 배율 (0.4 반감 수렴 공식)

상위 20명 고래 중 동일 마켓에 포지션을 보유한 고래 수에 따라 베팅금을 증폭한다.

```python
# multiplier = 1 + 0.4^1 + 0.4^2 + ... (겹침 추가마다 효과 40% 감쇠)
bonus      = sum(0.4 ** i for i in range(1, overlap_count))
multiplier = 1.0 + bonus
bet_size   = min(bet_size * multiplier, bankroll * 0.9)
```

| 겹침 고래 수 | 배율 | 설명 |
|---|---|---|
| 1명 | ×1.000 | 기본 베팅 |
| 2명 | ×1.400 | +40% |
| 3명 | ×1.560 | +56% |
| 4명 | ×1.624 | +62.4% |
| ∞ | ×1.667 | 자연 수렴 한계 (최대 +66.7%) |

아무리 많은 고래가 겹쳐도 ×1.667을 초과하지 않아 과집중 리스크가 자동 차단된다.

### MIRROR EXIT (고래 SELL 신호 추종)

```python
고래 SELL 감지 → 동일 marketId 동방향 포지션 검토
  current_price >= entry_price → 청산 (수익 실현)
  current_price < entry_price  → 유지 (MIRROR HOLD, 자연 정산 대기)
```

손실 상태에서 청산하면 손실만 확정되므로 홀드가 합리적이다.

---

## 8. 베팅 전략 및 자금 관리

### 베팅 금액 계산 (켈리 기준 근사)

```python
portfolio = bankroll + Σ(position.current_price × position.shares)
bet_size  = portfolio / BET_DIVISOR   # BET_DIVISOR=40 → 2.5%
# Overlap 2명 이상 시: bet_size × (1 + Σ 0.4^i) 적용
```

포지션이 늘어날수록 `bankroll`이 감소하여 `portfolio`가 줄어들고, `bet_size`도 자동으로 축소된다.

**예시**: 뱅크롤 $5,000, 포지션 없음 → bet_size = $125 / Overlap 3명 → $125 × 1.56 = $195

### 포지션 키 구조

```python
pos_key = f"{market_id}_{whale_addr[:8]}_{unix_timestamp}"
# 예: "65291_0xfe6d..._1710780000"
```

---

## 9. 리스크 관리

### 손절 (Stop Loss)

60초마다 모든 포지션의 현재가를 조회한다.

```python
drop = (entry_price - current_price) / entry_price
if drop >= STOP_LOSS_PCT(0.40):   # 40% 이상 하락
    → 즉시 청산
```

**No/Down 포지션 현재가 계산**:
```python
# No/Down 포지션: 현재가 = 1 - YES_best_ask
yes_ask = get_best_price(market_id, side="BUY")
current = 1.0 - yes_ask
```

**오더북 조회 실패 시 Fallback**:
```python
# 마지막 알려진 가격(current_price)으로 재시도
# None 반환이라도 손절 체크 스킵하지 않음 (유령 포지션 방지)
```

### 타임아웃

```python
now - pos["opened_at"] > 7 * 86400   # 7일 이상 미정산
→ 마지막 알려진 가격으로 강제 청산 (TIMEOUT)
```

### PnL 계산

| 청산 유형 | 계산식 |
|-----------|--------|
| **WIN** (정산 승리) | `shares × 1.0 - size_usdc` (실수령 - 원금) |
| **LOSS** (정산 패배) | `-size_usdc` (전액 손실) |
| **STOP_LOSS / MIRROR_EXIT** | `(actual_sell - entry) × shares - sell_fee` |
| **TIMEOUT** | `(last_known_price - entry) × shares` |

### 정산 결과 판별

```python
# 동방향이므로 우리 outcome == 승리 outcome → WIN
won_name   = resolution.get("name", "").upper()
our_outcome = pos.get("outcome_name", "").upper()

if won_name:
    reason = "WIN" if won_name == our_outcome else "LOSS"
else:
    # resolution name 없으면 현재가 기준 fallback
    reason = "WIN" if current >= 0.95 else "LOSS"
```

---

## 10. 고래 스코어링 시스템

### 진입 기준

| score | 처리 |
|-------|------|
| `>= 0.2` | ✅ 진입 허용 |
| `0 < score < 0.2` | ❌ 차단 (저품질 고래) |
| `== 0` + vol < $1,000 | ❌ 차단 (PAPER 완화값, LIVE 시 $5,000 권장) |
| `== 0` + vol >= $1,000 | ✅ 임시 허용 (초기 DB 구축 기간) |
| DB 없음 + size < MIN_WHALE_SIZE | ❌ 신규 무명 고래 차단 |

### status 자동 판정

```python
score >= 0.60 → status = "active"
score <  0.45 → status = "inactive"
그 사이       → 유지 (이전 상태 보존)
```

### DB 구조 (`data/whales_predict.json`)

```json
{
  "0xfe6d...": {
    "address": "0xfe6d...",
    "name": "whale_0xfe6d",
    "score": 0.72,
    "win_rate": 78.5,
    "roi": 45.2,
    "total_trades": 48,
    "resolved_trades": 31,
    "wins": 24,
    "losses": 7,
    "total_pnl": 5840.0,
    "total_volume": 98500.0,
    "leaderboard_pnl": 38400.0,
    "recent_count": 12,
    "confidence": 1.0,
    "recency_penalty": 0.75,
    "activity_penalty": 1.0,
    "delta_bonus": 0.15,
    "category_stats": {
      "sports":   {"wins": 10, "losses": 3, "roi": 52.1},
      "crypto":   {"wins": 8,  "losses": 2, "roi": 38.7},
      "politics": {"wins": 4,  "losses": 1, "roi": 61.2},
      "other":    {"wins": 2,  "losses": 1, "roi": 18.4}
    },
    "status": "active",
    "last_seen": 1710780000,
    "updated_at": 1710783600,
    "trades": ["...최근 50건..."]
  }
}
```

---

## 11. 주문 실행 흐름 (LIVE)

```
place_order(market_id, side=BUY, price, size_usdt)
         │
         ▼
1. get_market(market_id)
   → outcomes[], fee_rate_bps, decimalPrecision
   → token_id 탐색 (우선순위: 전달값 → outcome_name 매칭 → YES fallback)
         │
         ▼
2. get_orderbook(market_id) → Book(asks=[DepthLevel], bids=[...])
         │
         ▼
3. 주문 금액 계산
   BUY:  builder.get_market_order_amounts(MarketHelperValueInput(BUY, value_wei, slippage_bps))
   SELL: builder.get_market_order_amounts(MarketHelperInput(SELL, quantity_wei, slippage_bps))
         │
         ▼
4. builder.build_typed_data(BuildOrderInput(side, token_id, maker_amount, taker_amount, fee_rate_bps))
   → EIP712TypedData
         │
         ▼
5. builder.sign_typed_data_order(typed_data) → SignedOrder
         │
         ▼
6. POST /v1/orders (signed.model_dump())
   → 응답: {"status": "matched"/"filled"/"live", "orderId": "...", ...}
         │
         ▼
7. status 확인
   "matched" / "filled" → 포지션 등록
   "live" (미체결) → cancel_order() 후 SKIP
```

---

## 12. 페이퍼 트레이딩 모드

`PAPER_TRADING=True` 설정 시 블록체인 트랜잭션 없이 현실적 시뮬레이션을 수행한다.

| 항목 | 시뮬레이션 방법 |
|------|----------------|
| BUY 체결가 | 실제 오더북 ask[0] × (1 + 슬리피지 3%) |
| SELL 체결가 | 실제 오더북 bid[0] × (1 - 슬리피지 3%) |
| No 결과물 BUY | 1 - 실제 bids[0] + 슬리피지 |
| 수수료 | `feeRateBps` 필드 조회 (기본 200bps = 2%) |
| shares 계산 | `(size_usdt - fee) / exec_price` |
| WIN 정산 | $1.00 × shares - size_usdc |
| LOSS 정산 | -size_usdc |
| 스프레드 필터 | 실제 오더북 기반 |

**상태 파일**: `data/state_Predict_PAPER.json`
**거래 기록**: `data/trade_history_PAPER.jsonl`

---

## 13. 텔레그램 인터페이스

봇 시작 시 채팅에 고정 Reply Keyboard가 나타난다.

| 버튼 | 응답 내용 |
|------|-----------|
| 📊 **Status** | 포트폴리오, 가용잔고, 실현/미실현 PnL, 승률, ROI, APR, 포지션 수, 추적 고래 수 |
| 📋 **Trades** | 최근 정산 10건 (WIN/LOSS/STOP_LOSS/MIRROR_EXIT/TIMEOUT) |
| 📌 **Positions** | 활성 포지션 전체 + 미실현 손익 합계 |
| 🔄 **Restart** | 30초 내 재확인 후 `os.execv`로 재시작 |
| ⏹ **Stop** | `os._exit(0)` 즉시 종료 |

**알림 종류**:
- `🚀` 봇 시작
- `🟢` 진입 (마켓명, 고래명, 체결가, 수수료, Overlap 배율)
- `✅` WIN / `❌` LOSS
- `🔴` STOP_LOSS (하락률 포함)

**Status 화면 예시**:
```
📊 BOT STATUS [PAPER]
─────────────────
💼 포트폴리오: $5,245.80
💵 가용 잔고: $4,891.20
─────────────────
📈 실현 PnL: +$189.50
📉 미실현 PnL: +$54.30
🎯 승률: 67.5% (27W/13L)
💹 ROI: +8.15% | APR: +146.2%
─────────────────
📌 활성 포지션: 5개
🐳 추적 고래: 172마리
🕒 2026-03-19 10:24:05 (UTC)
```

---

## 14. 데이터 파일 구조

### `state_Predict_PAPER.json`

```json
{
  "bankroll": 4891.20,
  "peak_bankroll": 5300.00,
  "stats": {
    "total_bets": 40,
    "wins": 27,
    "losses": 13,
    "total_pnl": 189.50,
    "max_drawdown": -210.0
  },
  "positions": {
    "65291_0xfe6d_1710780000": {
      "marketId": "65291",
      "whale_addr": "0xfe6d...",
      "whale_name": "whale_0xfe6d",
      "entry_price": 0.35,
      "current_price": 0.48,
      "size_usdc": 125.0,
      "shares": 357.1,
      "opened_at": 1710780000,
      "question": "Lakers vs. Heat — Lakers win?",
      "outcome_name": "Yes",
      "token_id": "0x789..."
    }
  },
  "seen_txs": ["tx_hash_1", "...최근 1000건..."]
}
```

### `trade_history_PAPER.jsonl`

```jsonl
{"action": "OPEN", "pos_key": "65291_0xfe6d_1710780000", "price": 0.35, "pnl": 0, "question": "Lakers vs. Heat", "bankroll_after": 4875.0, "timestamp": "2026-03-18T17:56:00"}
{"action": "WIN",  "pos_key": "65291_0xfe6d_1710780000", "price": 1.0,  "pnl": 232.1, "question": "Lakers vs. Heat", "bankroll_after": 5107.1, "timestamp": "2026-03-19T02:15:00"}
{"action": "STOP_LOSS", "pos_key": "...", "price": 0.21, "pnl": -51.3, ...}
```

### `overlap_positions.json`

```json
{
  "timestamp": 1710780000,
  "overlap": {
    "65291": ["0xfe6d...", "0xabc1...", "0x9d3f..."],
    "70412": ["0xfe6d...", "0x2b88..."]
  }
}
```

### `leaderboard_snapshot.json`

```json
{
  "timestamp": 1710780000,
  "rows": [
    {"address": "0xfe6d...", "name": "Gemini", "pnl": 224000.0, "vol": 165000.0, "markets": 412}
  ]
}
```

### 로그 파일

| 파일 | 내용 | 용도 |
|------|------|------|
| `logs/market_debug.jsonl` | 마켓 API 응답 필드 전체 | API 스키마 변경 감지 |
| `logs/match_debug.jsonl` | 고래 체결 raw 응답 (최초 30건) | quoteType 방향 검증 |

---

## 15. 설치 및 실행

### 요구사항

- Python 3.10+
- predict-sdk 0.0.15
- BNB Chain 지갑 (실거래 시)
- Predict.fun API 키 (Discord에서 발급)
- 텔레그램 봇 토큰 (선택)

### 설치

```bash
git clone <repo_url>
cd predict_trader_bot
python -m venv venv

# macOS/Linux
source venv/bin/activate
# Windows
venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env
# .env 파일에 실제 설정값 입력
```

### 실행

```bash
# Watchdog 포함 실행 (권장 — 자동 재시작)
python run_bot.py

# 봇 직접 실행 (디버그용)
python core/predict_copy_bot.py

# 페이퍼 트레이딩 상태 초기화
python reset_paper.py

# 영구 종료
touch .stop_bot
# 또는 텔레그램 ⏹ Stop 버튼

# PnL 리더보드 확인 (독립 실행)
python leaderboard.py --top 50 --sort pnl
python leaderboard.py --seed-whales   # 상위 고래 DB 주입
```

### Termux (Android) 백그라운드 실행

```bash
pkill -f predict_copy_bot; sleep 1; python3 -u run_bot.py 2>&1 | tee -a bot.log
tail -f bot.log   # 로그 확인
```

---

## 16. 환경 변수 설정 (.env)

```env
# ───────────────────────────────────
# Predict.fun API
# ───────────────────────────────────
PREDICT_API_KEY=your_api_key_here       # Discord #api-key 채널에서 발급
PREDICT_API_BASE=https://api.predict.fun
PREDICT_WS_URL=wss://ws.predict.fun/ws  # 현재 미사용 (오더북만 지원)

# ───────────────────────────────────
# BNB Chain 지갑 (실거래 시 필수)
# ───────────────────────────────────
PRIVATE_KEY=0x...                       # 절대 외부 노출 금지
WALLET_ADDRESS=0x...

# BNB Chain RPC (기본값 사용 가능, 자동 fallback)
BSC_RPC_URL=https://bsc-dataseed1.binance.org

# ───────────────────────────────────
# 운영 설정
# ───────────────────────────────────
INITIAL_BANKROLL=5000.0                 # 시작 뱅크롤 (USDT)
PAPER_TRADING=True                      # False = 실거래 (충분한 검증 후)
DEBUG_MODE=True

# ───────────────────────────────────
# 고래/포지션 관리
# ───────────────────────────────────
MAX_POSITIONS=30                        # 최대 동시 포지션 수
BET_DIVISOR=40                          # 포트폴리오 ÷ 40 = 2.5% 베팅
STOP_LOSS_PCT=0.40                      # 40% 하락 시 손절
MIN_WHALE_SIZE_USDT=200.0               # 고래 최소 거래액 (PAPER: $200 / LIVE: $500 권장)
MIN_PRICE=0.10                          # 극초저확률 차단
MAX_PRICE=0.85                          # 정산 임박 마켓 차단
DEFAULT_SLIPPAGE_BPS=300                # 슬리피지 3%

# ───────────────────────────────────
# 고래 스코어링
# ───────────────────────────────────
MIN_WIN_RATE=55.0                       # 스코어링 최소 승률
MIN_TRADES=5                            # 스코어링 최소 거래 수

# ───────────────────────────────────
# 텔레그램 (선택)
# ───────────────────────────────────
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
```

---

## 17. 주요 버그 수정 이력

### 전략 전환 (역방향 → 동방향)

| 항목 | 내용 |
|------|------|
| `_get_opposite_outcome()` 제거 | 역방향 outcome 탐색 로직 완전 삭제 |
| 동방향 진입 | 고래의 `outcome_name`, `token_id`, `price` 직접 사용 |
| `CONTRARIAN HOLD` 제거 | 고래 SELL 시 단순 이익/손실 기준으로 청산 판단 |
| `is_contrarian` 필드 제거 | 포지션 dict에서 해당 필드 완전 제거 |
| 정산 판별 수정 | `YES=WIN` 하드코딩 → `won_name == our_outcome` 비교 (No/Down 포지션 WIN 정확 처리) |

### GraphQL 기반 고래 발굴 구축

| 항목 | 내용 |
|------|------|
| 리더보드 발굴 | GraphQL `pnlUsd` 재정렬 → 포인트 숨겨진 고수익 고래 자동 시드 |
| 스코어링 API | REST `/v1/traders/{addr}/activity` (404) → GraphQL `positions(isResolved: true)` |
| Overlap 감지 | 상위 20명 현재 포지션 중복 맵 생성 → 베팅금 배율 자동 적용 |
| 스냅샷 델타 | 6시간 pnlUsd 변화량 → 최근 활발한 고래 가중치 (+0~30%) |
| 카테고리 분류 | 마켓 질문 키워드 → sports/crypto/politics/other 분류 및 카테고리별 승률 집계 |

### 뱅크롤 소진 방어 (수정 완료)

| 버그 | 원인 | 수정 |
|------|------|------|
| **정산 마켓 필터 미작동** | `status == "resolved"` 소문자 비교 | `_is_resolved()` `.upper()` 정규화 |
| **백로그 차단 해제** | `_load_state()` 후 `startup_time` 덮어쓰기 | 해당 코드 제거 |
| **stale trade 진입** | 오더북 vs 고래 가격 25% 이탈 미감지 | 가격 이탈 체크 추가 |
| **GraphQL 400 오류** | `$addr: String!` 타입 오류, `quantity` 없는 필드 사용 | `Address!` 타입, 실제 필드(`shares`, `pnlUsd`)로 교체 |

### 필터 파라미터 변경 이력

| 설정 | 이전 | 현재 | 이유 |
|------|------|------|------|
| `BET_DIVISOR` | 20 (5%) | 40 (2.5%) | 분산 확대 |
| `MAX_POSITIONS` | 20 | 30 | 포지션 다양화 |
| `MAX_PRICE` | 0.93 | 0.85 | 정산 임박 마켓 차단 강화 |
| `MIN_WHALE_SIZE_USDT` | $500 | $200 (PAPER) | 데이터 수집 가속 |
| `score=0 vol 기준` | $5,000 | $1,000 (PAPER) | 초기 허용 완화 |
| `중복 TTL` | 30분 | 15분 (PAPER) | 거래수 확대 |

---

## 18. 운영 현황 및 로드맵

### 현재 단계

| 항목 | 상태 | 비고 |
|------|------|------|
| 환경 구축 | ✅ 완료 | predict-sdk 0.0.15 |
| API 검증 | ✅ 완료 | 주요 엔드포인트 동작 확인 |
| 핵심 기능 | ✅ 완료 | 7단계 필터, 동방향 전략, 스코어링, 텔레그램 |
| GraphQL 리더보드 | ✅ 완료 | pnlUsd 재정렬, 172명 자동 시드 확인 |
| Overlap 감지 | ✅ 완료 | 상위 20명 포지션 중복 맵, 0.4 반감 배율 |
| 카테고리 스코어링 | ✅ 완료 | 키워드 기반 sports/crypto/politics/other |
| 페이퍼 실행 | ✅ 진행 중 | Termux 백그라운드 ($5,000 PAPER) |
| 라이브 전환 | 🟡 보류 | 페이퍼 데이터 분석 후 결정 |

### 향후 과제

- **LIVE 전환 시 필터 강화**: `MIN_WHALE_SIZE_USDT=500`, `score=0 vol=$5000`, TTL=30분 복구
- **성과 분석 스크립트**: `trade_history_PAPER.jsonl` 자동 분석 (카테고리별 ROI)
- **카테고리 필터**: "이 고래는 크립토에서만 따라가라" 수준의 카테고리 기반 진입 필터
- **멀티아웃컴 지원**: 3개 이상 결과물 마켓 동방향 전략 설계

---

## 19. 주의사항

1. **보안**: `PRIVATE_KEY`는 절대 코드에 하드코딩하거나 외부에 노출하지 않는다. `.env` 파일은 `.gitignore`에 반드시 포함.

2. **실거래 전환**: `PAPER_TRADING=False` 전환 전 충분한 페이퍼 데이터 분석 필수. 승률, ROI, 최대 낙폭(MDD)을 종합적으로 검토한다. LIVE 전환 시 `MIN_WHALE_SIZE_USDT=500`, `score=0 vol=$5000`, TTL=1800 으로 필터 복구 권장.

3. **유동성 주의**: Predict.fun은 Polymarket 대비 유동성이 낮아 bid-ask spread가 3~8%에 달할 수 있다. 슬리피지 비용이 크게 발생할 수 있다.

4. **API 불안정성**: 응답 필드명이 예고 없이 변경될 수 있다. `market_debug.jsonl` 로그를 주기적으로 확인하여 스키마 변화를 모니터링한다.

5. **정산 지연**: 마켓 만료 후 실제 정산까지 30~60분 지연될 수 있다. 이는 오라클 결과 업데이트 대기 때문이며 정상이다.

6. **투자 책임**: 이 봇은 어떠한 수익도 보장하지 않는다. 예측시장은 원금 손실 위험이 있으며, 모든 투자 결정에 대한 책임은 운영자 본인에게 있다.

---

*Predict.fun 고래 카피 트레이딩 봇 — GraphQL 기반 고수익 고래 자동 발굴 + Overlap 신호 증폭 + 동방향 복사 매매*
