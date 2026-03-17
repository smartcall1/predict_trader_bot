# Predict Trader Bot 🤖

`predict_trader_bot`은 Predict.fun 플랫폼의 예측 시장 데이터를 분석하고 고래(Whale)들의 거래를 추종하여 자동으로 베팅을 수행하는 트레이딩 봇이야. Watchdog 기능이 포함되어 있어 봇이 예기치 않게 종료되도 자동으로 재시작해줘.

## 주요 기능
- **고래 추종 매매**: 고래들의 거래를 실시간으로 감지하고 점수화하여 전략적으로 따라붙어.
- **Watchdog 시스템**: 프로그램 크래시 시 자동 재시작 및 영구 종료 신호 처리 기능.
- **텔레그램 알림**: 실시간 거래 현황 및 봇 상태를 텔레그램으로 전송해줘.
- **안전 장치**: 페이퍼 트레이딩 모드, 슬리피지 제어, 스탑로스 설정 가능.

## 설치 방법

1. **저장소 복제 및 이동**
   ```bash
   git clone <repository_url>
   cd predict_trader_bot
   ```

2. **가상환경 설정 (권장)**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   ```

3. **의존성 설치**
   ```bash
   pip install -r requirements.txt
   ```

## 설정

`.env.example` 파일을 복사하여 `.env` 파일을 만들고 필요한 정보를 입력해줘.

```bash
cp .env.example .env
```

### 주요 설정 항목
- `PREDICT_API_KEY`: Predict.fun API 키 (디스코드 발급)
- `PRIVATE_KEY`: 지갑 개인키 (실거래 시 필수)
- `PAPER_TRADING`: `True` 설정 시 실제 트랜잭션 없이 테스트 모드로 동작
- `TELEGRAM_BOT_TOKEN` & `CHAT_ID`: 알림 수신용

## 실행 방법

봇은 감시자(Watchdog)와 함께 실행하는 것을 권장해:

```bash
python run_bot.py
```

- 봇을 영구 종료하려면 프로젝트 루트에 `.stop_bot` 파일을 만들면 돼.

## 프로젝트 구조
- `core/`: 봇의 핵심 로직 (클라이언트, 모니터링, 추종 로직 등)
- `tasks/`: 할 일 목록 및 관리 파일
- `logs/`: 봇 실행 로그 저장소
- `data/`: 분석 및 임시 데이터 저장소

## 주의사항
- 이 제품은 투자를 권유하지 않아. 모든 투자의 책임은 본인에게 있어!
- 개인키(`PRIVATE_KEY`)는 절대 외부에 노출되지 않도록 주의해줘.
