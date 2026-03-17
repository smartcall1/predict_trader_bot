"""
Predict.fun 봇 Watchdog 실행기
크래시 자동 재시작 + 영구 종료 신호 처리
"""

import subprocess
import sys
import time
import os

STOP_FILE = os.path.join(os.path.dirname(__file__), ".stop_bot")
MAX_CRASHES = 10
CRASH_WINDOW = 300  # 5분 내 MAX_CRASHES회 이상 → 이상 종료로 판단 후 중단

def main():
    crash_times = []
    print("[Watchdog] Predict.fun 봇 시작")

    while True:
        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
            print("[Watchdog] 영구 종료 신호 수신 — 종료")
            break

        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join("core", "predict_copy_bot.py")],
            cwd=os.path.dirname(__file__),
        )
        print(f"[Watchdog] 봇 프로세스 시작 (PID={proc.pid})")

        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            print("[Watchdog] KeyboardInterrupt — 종료")
            break

        exit_code = proc.returncode
        now = time.time()
        crash_times = [t for t in crash_times if now - t < CRASH_WINDOW]
        crash_times.append(now)

        print(f"[Watchdog] 봇 종료 (exit_code={exit_code})")

        if len(crash_times) >= MAX_CRASHES:
            print(f"[Watchdog] {CRASH_WINDOW}초 내 {MAX_CRASHES}회 크래시 — 비정상 종료로 판단, 중단")
            break

        if os.path.exists(STOP_FILE):
            os.remove(STOP_FILE)
            print("[Watchdog] 영구 종료 신호 — 재시작 안 함")
            break

        print("[Watchdog] 5초 후 재시작...")
        time.sleep(5)


if __name__ == "__main__":
    main()
