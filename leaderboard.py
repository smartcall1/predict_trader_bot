"""
Predict.fun PnL 기반 리더보드
GraphQL API로 전체 트레이더 수집 → PnL/ROI/볼륨 기준 재정렬

실행:
  python leaderboard.py           # PnL TOP 50 출력
  python leaderboard.py --top 100 # TOP 100
  python leaderboard.py --sort roi # ROI 기준
  python leaderboard.py --min-vol 1000 # 최소 볼륨 $1000
  python leaderboard.py --seed-whales  # 상위 PnL 주소를 whales_predict.json에 자동 주입
"""

import json
import time
import argparse
import os
import sys
import requests
from datetime import datetime, timezone

# Windows cp949 터미널 UTF-8 강제
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)

GRAPHQL_URL = "https://graphql.predict.fun/graphql"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
WHALES_DB = os.path.join(DATA_DIR, "whales_predict.json")

QUERY = """
query Leaderboard($cursor: String) {
  leaderboard(pagination: {first: 50, after: $cursor}) {
    edges {
      node {
        rank
        totalPoints
        account {
          address
          name
          twitterUsername
          statistics {
            pnlUsd
            volumeUsd
            marketsCount
          }
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""


def fetch_all(max_pages: int = 20) -> list:
    """전체 리더보드 수집 (50개씩 페이지네이션)"""
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json", "User-Agent": "PredictBot/1.0"})

    rows = []
    cursor = None
    page = 0

    print(f"[Leaderboard] 데이터 수집 중...", end="", flush=True)
    while page < max_pages:
        variables = {"cursor": cursor} if cursor else {}
        resp = session.post(GRAPHQL_URL, json={"query": QUERY, "variables": variables}, timeout=15)
        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            print(f"\n[ERROR] {body['errors'][0]['message']}")
            break

        lb = body["data"]["leaderboard"]
        edges = lb["edges"]
        page_info = lb["pageInfo"]

        for e in edges:
            n = e["node"]
            acc = n["account"]
            st = acc.get("statistics") or {}
            pnl = float(st.get("pnlUsd") or 0)
            vol = float(st.get("volumeUsd") or 0)
            rows.append({
                "pts_rank":  n["rank"],
                "pts":       float(n["totalPoints"]),
                "name":      acc.get("name") or acc["address"][:10],
                "address":   acc["address"],
                "twitter":   acc.get("twitterUsername") or "",
                "pnl":       pnl,
                "vol":       vol,
                "markets":   int(st.get("marketsCount") or 0),
                "roi":       (pnl / vol * 100) if vol > 500 else 0.0,
            })

        print(".", end="", flush=True)
        page += 1

        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        time.sleep(0.3)  # rate limit 방어

    print(f" 완료! ({len(rows)}명 수집)")
    return rows


def print_leaderboard(rows: list, sort_by: str = "pnl", min_vol: float = 0, top_n: int = 50):
    """정렬 및 출력"""

    # 필터
    filtered = [r for r in rows if r["vol"] >= min_vol]

    # 정렬
    key_map = {
        "pnl": lambda x: x["pnl"],
        "roi": lambda x: x["roi"],
        "vol": lambda x: x["vol"],
    }
    sorted_rows = sorted(filtered, key=key_map.get(sort_by, key_map["pnl"]), reverse=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    profitable = sum(1 for r in rows if r["pnl"] > 0)

    print()
    print("=" * 90)
    print(f"  [LEADERBOARD]  Predict.fun PnL 리더보드  |  기준: {sort_by.upper()}  |  {now}")
    print(f"  수집: {len(rows)}명  |  수익자: {profitable}명 ({profitable/len(rows)*100:.0f}%)  |  최소볼륨: ${min_vol:,.0f}")
    print("=" * 90)
    print(f"{'순위':>4}  {'포인트순':>6}  {'이름':22}  {'PnL':>12}  {'ROI':>7}  {'볼륨':>14}  {'마켓수':>5}")
    print("-" * 90)

    for i, r in enumerate(sorted_rows[:top_n], 1):
        pnl_sign = "+" if r["pnl"] >= 0 else "-"
        twitter = f"@{r['twitter']}" if r["twitter"] else ""
        name_display = f"{r['name'][:18]}{(' '+twitter[:8]) if twitter else ''}"
        print(
            f"{i:4}.  "
            f"#{r['pts_rank']:5}  "
            f"{name_display:22}  "
            f"{pnl_sign}${abs(r['pnl']):>10,.0f}  "
            f"{r['roi']:>6.1f}%  "
            f"${r['vol']:>13,.0f}  "
            f"{r['markets']:>5}"
        )

    print("=" * 90)

    # 요약 통계
    top10_pnl = sum(r["pnl"] for r in sorted_rows[:10])
    print(f"\n  TOP 10 합산 PnL: ${top10_pnl:,.0f}")
    if sorted_rows:
        best = sorted_rows[0]
        print(f"  최고 {sort_by.upper()}: {best['name']} → PnL=${best['pnl']:,.0f}, ROI={best['roi']:.1f}%, Vol=${best['vol']:,.0f}")
    print()

    return sorted_rows


def seed_whales(top_rows: list, min_pnl: float = 5000, min_vol: float = 100000, top_n: int = 30):
    """상위 PnL 트레이더 주소를 whales_predict.json에 자동 주입"""
    os.makedirs(DATA_DIR, exist_ok=True)

    # 기존 DB 로드
    db = {}
    if os.path.exists(WHALES_DB):
        try:
            with open(WHALES_DB, "r", encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            pass

    candidates = [r for r in top_rows if r["pnl"] >= min_pnl and r["vol"] >= min_vol][:top_n]
    added, updated = 0, 0

    for r in candidates:
        addr = r["address"].lower()
        if addr not in db:
            db[addr] = {
                "address":      addr,
                "name":         r["name"],
                "total_trades": r["markets"],
                "wins":         0,
                "losses":       0,
                "total_pnl":    round(r["pnl"], 2),
                "total_volume": round(r["vol"], 2),
                "score":        0.0,
                "status":       "seeded",
                "twitter":      r["twitter"],
                "leaderboard_pnl": round(r["pnl"], 2),
                "leaderboard_roi": round(r["roi"], 2),
                "last_seen":    int(time.time()),
                "trades":       [],
            }
            added += 1
        else:
            # 리더보드 최신 PnL 업데이트
            db[addr]["leaderboard_pnl"] = round(r["pnl"], 2)
            db[addr]["leaderboard_roi"] = round(r["roi"], 2)
            db[addr]["total_volume"]    = round(r["vol"], 2)
            updated += 1

    # 저장
    tmp = WHALES_DB + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp, WHALES_DB)

    print(f"[Whales] DB 업데이트 완료: 신규 {added}명 추가, {updated}명 업데이트 → 총 {len(db)}마리")
    print(f"[Whales] 시드 기준: PnL >= ${min_pnl:,.0f}, Vol >= ${min_vol:,.0f}")


def main():
    parser = argparse.ArgumentParser(description="Predict.fun PnL 리더보드")
    parser.add_argument("--top",          type=int,   default=50,    help="표시할 순위 수 (기본 50)")
    parser.add_argument("--sort",         type=str,   default="pnl", choices=["pnl", "roi", "vol"], help="정렬 기준")
    parser.add_argument("--min-vol",      type=float, default=0,     help="최소 볼륨 필터 (USDT)")
    parser.add_argument("--max-pages",    type=int,   default=20,    help="최대 페이지 수 (50개/페이지)")
    parser.add_argument("--seed-whales",  action="store_true",       help="상위 PnL 주소를 whales DB에 주입")
    parser.add_argument("--seed-min-pnl", type=float, default=5000,  help="시드 최소 PnL (기본 $5,000)")
    parser.add_argument("--seed-min-vol", type=float, default=50000, help="시드 최소 볼륨 (기본 $50,000)")
    args = parser.parse_args()

    rows = fetch_all(max_pages=args.max_pages)
    if not rows:
        print("[ERROR] 데이터 수집 실패")
        return

    sorted_rows = print_leaderboard(
        rows,
        sort_by=args.sort,
        min_vol=args.min_vol,
        top_n=args.top,
    )

    if args.seed_whales:
        seed_whales(sorted_rows, min_pnl=args.seed_min_pnl, min_vol=args.seed_min_vol)


if __name__ == "__main__":
    main()
