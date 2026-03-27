"""
prune_whale_db.py — predict 고래 DB 대량 정리
실행: python3 scripts/prune_whale_db.py [--apply]

기준: leaderboard_pnl > 0 인 리더보드 검증 고래만 유지
      나머지(폴링 감지 노이즈, 미검증) 제거
"""
import json, os, sys, time

DB_FILE = os.path.join(os.path.dirname(__file__), "../data/whales_predict.json")

def main():
    apply = "--apply" in sys.argv

    with open(DB_FILE, encoding="utf-8") as f:
        db = json.load(f)

    before = len(db)

    # 유지 조건: 리더보드 검증 고래 (lb_pnl > 0)
    keep = {
        addr: w for addr, w in db.items()
        if (w.get("leaderboard_pnl") or 0) > 0
    }
    removed = before - len(keep)

    print(f"--- Predict Whale DB Prune ---")
    print(f"전체: {before}마리 → 유지: {len(keep)}마리 (제거: {removed}마리)")
    print(f"\n[유지 고래 상위 10]")
    for addr, w in sorted(keep.items(), key=lambda x: x[1].get("leaderboard_pnl", 0), reverse=True)[:10]:
        print(f"  {w.get('name', addr[:8])}: lb_pnl=${w.get('leaderboard_pnl', 0):.0f} | score={w.get('score', 0):.3f}")

    if not apply:
        print(f"\n[DRY RUN] 실제 적용하려면 --apply 옵션 사용")
        return

    # 백업
    bak = DB_FILE + ".bak"
    with open(bak, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)
    print(f"\n백업 저장: {bak}")

    # 저장
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DB_FILE)
    print(f"✅ 완료: {before}마리 → {len(keep)}마리")

if __name__ == "__main__":
    main()
