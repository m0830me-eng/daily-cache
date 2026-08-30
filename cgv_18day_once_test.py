#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi import requests

KST = ZoneInfo("Asia/Seoul")

SITE_NO = "0013"
BOOKING_PAGE = "https://cgv.co.kr/cnm/movieBook"
API_URL = "https://cgv.co.kr/api/v1/booking/searchMovScnInfo"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_PAGE,
}

START_OFFSET = 4
DAY_COUNT = 18
WORKERS = 18
TIMEOUT = 20
BLOCK_STATUSES = {403, 429, 500, 502, 503, 504}

start_barrier = threading.Barrier(DAY_COUNT)


def now_kst():
    return datetime.now(KST)


def make_dates():
    today = now_kst().date()
    return [
        (today + timedelta(days=START_OFFSET + i)).strftime("%Y%m%d")
        for i in range(DAY_COUNT)
    ]


def extract_rows(data):
    if isinstance(data, dict):
        direct = data.get("data")
        if isinstance(direct, list):
            return [row for row in direct if isinstance(row, dict)]

    rows = []

    def walk(item):
        if isinstance(item, dict):
            looks_like_show = (
                (item.get("movNo") or item.get("movNm"))
                and (item.get("scnsrtTm") or item.get("scnSseq"))
            )
            if looks_like_show:
                rows.append(item)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(data)
    return rows


def fetch_once(date):
    session = requests.Session(impersonate="chrome")

    try:
        start_barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        pass

    started = time.monotonic()

    try:
        r = session.get(
            API_URL,
            params={
                "coCd": "A420",
                "siteNo": SITE_NO,
                "scnYmd": date,
                "rtctlScopCd": "08",
            },
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        elapsed = time.monotonic() - started

        if r.status_code in BLOCK_STATUSES:
            return {
                "date": date,
                "ok": False,
                "status": r.status_code,
                "elapsed": elapsed,
                "error": f"HTTP {r.status_code}",
            }

        if r.status_code != 200:
            return {
                "date": date,
                "ok": False,
                "status": r.status_code,
                "elapsed": elapsed,
                "error": f"HTTP {r.status_code}",
            }

        try:
            data = r.json()
        except Exception as e:
            preview = r.text[:120].replace("\n", " ").replace("\r", " ")
            return {
                "date": date,
                "ok": False,
                "status": r.status_code,
                "elapsed": elapsed,
                "error": f"JSON ERROR {repr(e)} PREVIEW={preview!r}",
            }

        rows = extract_rows(data)

        return {
            "date": date,
            "ok": True,
            "status": r.status_code,
            "elapsed": elapsed,
            "bytes": len(r.content),
            "rows": len(rows),
        }

    except Exception as e:
        return {
            "date": date,
            "ok": False,
            "status": None,
            "elapsed": time.monotonic() - started,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def main():
    dates = make_dates()

    print("=" * 72, flush=True)
    print("CGV YONGSAN +4~+21 DAYS / 18-WORKER ONE-SHOT TEST", flush=True)
    print("=" * 72, flush=True)
    print(f"DATES: {dates[0]} ~ {dates[-1]} ({len(dates)} dates)", flush=True)
    print("WORKERS: 18 (all 18 dates start together)", flush=True)
    print("REPEAT: NO (one shot only)", flush=True)
    print("DISCORD/STATE WRITE: NO", flush=True)
    print(f"TIMEOUT: {TIMEOUT}s", flush=True)
    print("KST NOW:", now_kst().strftime("%Y-%m-%d %H:%M:%S"), flush=True)
    print("=" * 72, flush=True)

    total_started = time.monotonic()
    results = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(fetch_once, date) for date in dates]
        for future in as_completed(futures):
            results.append(future.result())

    total_elapsed = time.monotonic() - total_started
    results.sort(key=lambda x: x["date"])

    success = 0
    failed = []

    for item in results:
        if item["ok"]:
            success += 1
            print(
                f"{item['date']} | OK | HTTP={item['status']} | "
                f"{item['elapsed']:.2f}s | SIZE={item['bytes']:,} | "
                f"ROWS={item['rows']}",
                flush=True,
            )
        else:
            failed.append(item["date"])
            print(
                f"{item['date']} | FAIL | {item['elapsed']:.2f}s | "
                f"{item['error']}",
                flush=True,
            )

    print("=" * 72, flush=True)

    if success == DAY_COUNT:
        print(
            f"✅ TEST RESULT: ALL 18 DATES SUCCESS | {total_elapsed:.2f}s",
            flush=True,
        )
        print(
            "판정: CGV +4~+21일 18개 날짜를 동시에 시작해도 "
            "이번 테스트에서는 모두 정상 응답했습니다.",
            flush=True,
        )
    else:
        print(
            f"⚠️ TEST RESULT: PARTIAL FAILURE | success={success}/{DAY_COUNT} | "
            f"{total_elapsed:.2f}s",
            flush=True,
        )
        print("FAILED DATES:", ", ".join(failed), flush=True)

    print("STATE/DISCORD CHANGED: NO", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
