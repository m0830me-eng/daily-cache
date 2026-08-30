import os
import sys
import json
import re
import time
import random
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from curl_cffi import requests

# GitHub Actions에서도 로그를 즉시 표시
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


KST = ZoneInfo("Asia/Seoul")

SITE_NO = "0013"
SITE_NAME = "CGV 용산아이파크몰"

DAYS = 50

# 날짜별 요청 분산: 공개 CGV 알리미 방식 참고
DATE_REQUEST_MIN_SECONDS = 0.8
DATE_REQUEST_MAX_SECONDS = 1.2
START_DELAY = float(os.environ.get("START_DELAY", "0"))
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "120"))

BOOKING_PAGE = "https://cgv.co.kr/cnm/movieBook"
API_URL = "https://cgv.co.kr/api/v1/booking/searchMovScnInfo"

STATE_FILE = "seen_cgv_yongsan.json"
BASELINE_FILE = "baseline_cgv_yongsan.done"

GV_WEBHOOK = os.environ.get("DISCORD_CGV_YONGSAN_GV", "")
STAGE_WEBHOOK = os.environ.get("DISCORD_CGV_YONGSAN_STAGE", "")
DISCORD_USER_ID = "1383846907847381184"

# 사용자 제공 실제 용산 4DX 예매 링크에서 확인된 상영관 번호
YONGSAN_4DX_SCNS_NO = "003"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Referer": BOOKING_PAGE,
}


def clean_text(value):
    return " ".join(str(value or "").split())


def all_row_text(value):
    parts = []

    def walk(item):
        if isinstance(item, dict):
            for v in item.values():
                walk(v)
        elif isinstance(item, (list, tuple, set)):
            for v in item:
                walk(v)
        elif item is not None:
            text = clean_text(item)
            if text:
                parts.append(text)

    walk(value)
    return " | ".join(parts)


def send_discord(webhook, message):
    if not webhook:
        print("WEBHOOK MISSING")
        return False

    try:
        r = requests.post(
            webhook,
            json={
                "content": message,
                "flags": 4,
                "allowed_mentions": {
                    "users": [DISCORD_USER_ID]
                },
            },
            impersonate="chrome",
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print("DISCORD ERROR:", repr(e))
        return False


def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception as e:
        print("STATE LOAD ERROR:", repr(e))
        return set()


def save_seen(seen):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, ensure_ascii=False, indent=2)
        print("STATE SAVED:", len(seen))
    except Exception as e:
        print("STATE SAVE ERROR:", repr(e))


def mark_baseline_done():
    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        f.write(datetime.now(KST).isoformat())
    print("BASELINE MARKER CREATED")


def baseline_done():
    return os.path.exists(BASELINE_FILE)


def event_key(date, row, event_type):
    return "|".join([
        SITE_NO,
        date,
        str(row.get("movNo") or ""),
        str(row.get("scnsNo") or ""),
        str(row.get("scnSseq") or ""),
        str(row.get("scnsrtTm") or ""),
        event_type,
    ])


def make_booking_link(date, row):
    params = {
        "movNo": str(row.get("movNo") or ""),
        "scnYmd": date,
        "siteNo": SITE_NO,
        "scnsNo": str(row.get("scnsNo") or ""),
        "siteNm": SITE_NAME,
        "scnSseq": str(row.get("scnSseq") or ""),
    }
    return "https://cgv.co.kr/cnm/movieBook/movie?" + urlencode(params)


def pretty_date(date):
    dt = datetime.strptime(date, "%Y%m%d")
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} ({weekdays[dt.weekday()]})"


def detect_event_type(row):
    event_fields = [
        "videoAddexpCdNm",
        "videoAddexpNm",
        "videoAddexpCd",
        "eventNm",
        "eventName",
        "specialEventNm",
        "specialEventName",
        "addexpNm",
        "addexpName",
        "movNm",
        "movName",
    ]

    event_text = " | ".join(
        clean_text(row.get(field))
        for field in event_fields
        if clean_text(row.get(field))
    )
    full_text = all_row_text(row)

    compact_event = re.sub(r"\s+", "", event_text)
    compact_full = re.sub(r"\s+", "", full_text)

    if (
        "무대인사" in compact_event
        or "무대인사" in compact_full
        or "舞台挨拶" in full_text
    ):
        return "무대인사"

    if (
        "관객과의대화" in compact_event
        or "관객과의대화" in compact_full
    ):
        return "GV"

    if re.search(r"(?<![A-Z0-9])GV(?![A-Z0-9])", event_text.upper()):
        return "GV"

    if re.search(r"(?<![A-Z0-9])GV(?![A-Z0-9])", full_text.upper()):
        return "GV"

    return None


def detect_format(row):
    format_fields = [
        "scnsNm",
        "scnsName",
        "screenNm",
        "screenName",
        "playKindNm",
        "playKindName",
        "screenTypeNm",
        "screenTypeName",
        "formatNm",
        "formatName",
    ]

    format_text = " | ".join(
        clean_text(row.get(field))
        for field in format_fields
        if clean_text(row.get(field))
    )
    full_text = all_row_text(row)

    format_upper = format_text.upper()
    full_upper = full_text.upper()

    # 용산 4DX는 실제 예매 링크에서 scnsNo=003 확인됨.
    if (
        "4DX" in format_upper
        or "4DX" in full_upper
        or str(row.get("scnsNo") or "").strip() == YONGSAN_4DX_SCNS_NO
    ):
        return "4DX"

    if (
        "IMAX" in format_upper
        or "아이맥스" in format_text
        or "IMAX" in full_upper
        or "아이맥스" in full_text
    ):
        return "IMAX"

    return None


def get_target_type(row):
    # GV/무대인사 회차가 특별관이어도 이벤트 알림을 우선해 중복 알림 방지.
    event_type = detect_event_type(row)
    if event_type:
        return event_type

    return detect_format(row)


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


def check_one_date(session, date):
    events = {}

    try:
        started = time.monotonic()
        r = session.get(
            API_URL,
            params={
                "coCd": "A420",
                "siteNo": SITE_NO,
                "scnYmd": date,
                "rtctlScopCd": "08",
            },
            headers=HEADERS,
            timeout=20,
        )
        elapsed = time.monotonic() - started

        print(
            f"API {date} STATUS={r.status_code} "
            f"TIME={elapsed:.2f}s SIZE={len(r.content):,} bytes"
        )

        if r.status_code in (403, 429, 503):
            print(f"!!! CGV RATE LIMIT / BLOCK ON {date} !!!")
            return None

        if r.status_code != 200:
            return events

        data = r.json()
        rows = extract_rows(data)

        for row in rows:
            target_type = get_target_type(row)
            if not target_type:
                continue

            key = event_key(date, row, target_type)
            events[key] = {
                "date": date,
                "type": target_type,
                "movie": clean_text(row.get("movNm") or row.get("movName")),
                "screen": clean_text(
                    row.get("scnsNm")
                    or row.get("scnsName")
                    or row.get("screenNm")
                    or row.get("screenName")
                ),
                "time": clean_text(row.get("scnsrtTm")),
                "row": row,
            }

    except Exception as e:
        print("DATE ERROR:", date, repr(e))

    return events


def make_dates():
    today = datetime.now(KST)
    return [
        (today + timedelta(days=i)).strftime("%Y%m%d")
        for i in range(DAYS)
    ]


def print_target_counts(events):
    counts = {
        "GV": 0,
        "무대인사": 0,
        "IMAX": 0,
        "4DX": 0,
    }

    for event in events.values():
        event_type = event.get("type")
        if event_type in counts:
            counts[event_type] += 1

    print("GV COUNT:", counts["GV"])
    print("STAGE COUNT:", counts["무대인사"])
    print("IMAX COUNT:", counts["IMAX"])
    print("4DX COUNT:", counts["4DX"])


def send_new_events(events, seen):
    new_events = []

    for key in sorted(events):
        if key in seen:
            continue

        event = events[key]
        start = event["time"]

        if len(start) == 4 and start.isdigit():
            start = start[:2] + ":" + start[2:]

        new_events.append({
            "key": key,
            "event": event,
            "start": start,
            "link": make_booking_link(event["date"], event["row"]),
        })

    if not new_events:
        return 0

    groups = {}

    for item in new_events:
        event = item["event"]

        if event["type"] == "IMAX":
            group_type = "IMAX"
            title = "새로운 용산 IMAX 상영 일정"
            webhook = GV_WEBHOOK
        elif event["type"] == "4DX":
            group_type = "4DX"
            title = "새로운 용산 4DX 상영 일정"
            webhook = GV_WEBHOOK
        elif event["type"] == "GV":
            group_type = "GV"
            title = "새로운 용산 GV 상영 일정"
            webhook = GV_WEBHOOK
        else:
            group_type = "무대인사"
            title = "새로운 용산 무대인사 상영 일정"
            webhook = STAGE_WEBHOOK

        group_key = (event["date"], group_type)
        groups.setdefault(
            group_key,
            {
                "title": title,
                "webhook": webhook,
                "items": [],
            },
        )["items"].append(item)

    sent_count = 0

    for (date, group_type), group in sorted(groups.items()):
        group["items"].sort(
            key=lambda item: (
                item["start"],
                item["event"]["movie"],
            )
        )

        lines = [
            f"<@{DISCORD_USER_ID}>",
            "",
            f"🎉 **{group['title']}** 🎉",
            "",
            f"📅 **{pretty_date(date)}**",
        ]

        for item in group["items"]:
            event = item["event"]
            screen_suffix = f" | {event['screen']}" if event["screen"] else ""
            lines.append(
                f"• 🎟️ [{item['start']} — {event['movie']}{screen_suffix}]"
                f"({item['link']})"
            )

        print(
            "NEW EVENT GROUP:",
            group_type,
            date,
            "COUNT=",
            len(group["items"]),
        )

        if send_discord(group["webhook"], "\n".join(lines)):
            for item in group["items"]:
                seen.add(item["key"])
                sent_count += 1

    return sent_count


def distributed_wait():
    wait_seconds = random.uniform(
        DATE_REQUEST_MIN_SECONDS,
        DATE_REQUEST_MAX_SECONDS,
    )
    print(f"NEXT DATE WAIT: {wait_seconds:.2f}s")
    time.sleep(wait_seconds)


def collect_baseline(session):
    all_events = {}
    blocked_dates = []
    dates = make_dates()

    for index, date in enumerate(dates):
        events = check_one_date(session, date)
        if events is None:
            blocked_dates.append(date)
        else:
            all_events.update(events)

        if index < len(dates) - 1:
            distributed_wait()

    if blocked_dates:
        print(
            "BASELINE BLOCKED DATES:",
            len(blocked_dates),
            "/",
            DAYS,
            "- 기준값 생성은 다음 실행에서 다시 시도합니다.",
        )
        return None

    return all_events


def scan_cycle(session, seen, cycle_number):
    cycle_started = time.monotonic()
    all_events = {}
    success_count = 0
    new_total = 0
    blocked_dates = []
    dates = make_dates()

    print()
    print("=" * 70)
    print(
        f"CYCLE #{cycle_number} START | "
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST | "
        f"50 DAYS | DISTRIBUTED SEQUENTIAL | "
        f"DATE GAP={DATE_REQUEST_MIN_SECONDS:.1f}~"
        f"{DATE_REQUEST_MAX_SECONDS:.1f}s"
    )
    print("=" * 70)

    for index, date in enumerate(dates):
        events = check_one_date(session, date)

        if events is None:
            blocked_dates.append(date)
            print(
                f"SKIP BLOCKED DATE: {date} - "
                "다음 날짜 감시를 계속합니다."
            )
        else:
            success_count += 1
            all_events.update(events)

            # 날짜 응답이 도착한 즉시 새 회차를 Discord로 보냄.
            new_total += send_new_events(events, seen)

        # 50개 요청을 한꺼번에 몰아치지 않도록 날짜 사이를 분산.
        if index < len(dates) - 1:
            distributed_wait()

    save_seen(seen)

    cycle_elapsed = time.monotonic() - cycle_started

    print_target_counts(all_events)
    print(
        f"CYCLE #{cycle_number} DONE | "
        f"SUCCESS={success_count}/{DAYS} | "
        f"BLOCKED={len(blocked_dates)} | "
        f"NEW={new_total} | "
        f"ELAPSED={cycle_elapsed:.2f}s"
    )

    if blocked_dates:
        print(
            "BLOCKED DATES:",
            ", ".join(blocked_dates),
        )
        print(
            "긴 쿨다운 없이 다음 사이클에서 다시 확인합니다."
        )

    return len(blocked_dates), cycle_elapsed

def main():
    print("=" * 70)
    print("CGV YONGSAN 50-DAY DISTRIBUTED MONITOR")
    print("TARGET: GV / STAGE / IMAX / 4DX")
    print("DATE RANGE: TODAY ~ +49 DAYS (50 DAYS TOTAL)")
    print("SCAN MODE: 1 DATE AT A TIME / DISTRIBUTED SEQUENTIAL")
    print(
        f"DATE REQUEST GAP: {DATE_REQUEST_MIN_SECONDS:.1f}~"
        f"{DATE_REQUEST_MAX_SECONDS:.1f} SECONDS"
    )
    print("429/403/503: BLOCKED DATE만 건너뛰고 다음 날짜 계속 감시")
    print("4DX FALLBACK: YONGSAN scnsNo=003")
    print("RUN SECONDS:", RUN_SECONDS)
    print("=" * 70)

    if START_DELAY > 0:
        print(f"START STAGGER: {START_DELAY:.2f}s")
        time.sleep(START_DELAY)

    session = requests.Session(impersonate="chrome")

    try:
        r = session.get(BOOKING_PAGE, timeout=20)
        print("BOOKING PAGE STATUS:", r.status_code)
        if r.status_code != 200:
            print("CGV BOOKING PAGE ERROR")
            return
    except Exception as e:
        print("BOOKING PAGE ERROR:", repr(e))
        return

    if not baseline_done():
        print()
        print("=" * 70)
        print("INITIAL 50-DAY BASELINE")
        print("=" * 70)
        print(
            "현재 GV / 무대인사 / IMAX / 4DX를 "
            "알림 없이 기준값으로 등록합니다."
        )

        events = collect_baseline(session)
        if events is None:
            print("BASELINE FAILED: CGV RATE LIMIT / BLOCK")
            return

        print_target_counts(events)
        seen = set(events.keys())
        print("BASELINE EVENT COUNT:", len(seen))
        save_seen(seen)
        mark_baseline_done()
        print("BASELINE COMPLETE - 이번 실행에서는 Discord 알림을 보내지 않았습니다.")
        return

    seen = load_seen()
    monitor_started = time.monotonic()
    cycle_number = 0

    while time.monotonic() - monitor_started < RUN_SECONDS:
        cycle_number += 1

        blocked_count, cycle_elapsed = scan_cycle(
            session,
            seen,
            cycle_number,
        )

        print(
            f"NEXT CYCLE IMMEDIATELY | "
            f"PREVIOUS={cycle_elapsed:.2f}s | "
            f"BLOCKED={blocked_count}"
        )

    save_seen(seen)
    print("FINAL SEEN STATE:", len(seen))
    print("DONE")


if __name__ == "__main__":
    main()
