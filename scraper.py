"""Delhi High Court - fetch orders judge-wise, save new ones to Google Drive,
and log case details (with party names read by OpenAI) to Google Sheets."""
import base64
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import time
from urllib.parse import urljoin

import requests
from pypdf import PdfReader

BASE = "https://delhihighcourt.nic.in/app/"
PAGE = BASE + "sitting-judge-wise"
DAYS_BACK = int(os.environ.get("DAYS_BACK", "20"))
TEST_LIMIT = int(os.environ.get("TEST_LIMIT") or "0")  # 0 means no limit
DRIVE_URL = os.environ.get("DRIVE_URL", "")
DRIVE_SECRET = os.environ.get("DRIVE_SECRET", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"
SEEN_FILE = "downloaded.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STOP_WORDS = {"justice", "hon", "ble", "honble", "the", "acting", "chief",
              "mr", "ms", "mrs", "dr"}
PARTY_PROMPT = (
    "You extract party names from Delhi High Court orders. The cause title lists "
    "parties as 'X ... Petitioner' versus 'Y ... Respondent'. Treat appellant, "
    "plaintiff, applicant and complainant as the petitioner side, and defendant, "
    "respondent and accused as the respondent side. Use the first-named party on "
    "each side; if more parties are on that side, add ' & Ors.'. Give names only, "
    "without addresses, designations of advocates, or words like 'Petitioner'. "
    "Reply as JSON: {\"petitioner\": \"...\", \"respondent\": \"...\"}. "
    "Use an empty string if a name cannot be found.")


def words(name):
    return [w for w in re.findall(r"[a-z]+", name.lower())
            if w not in STOP_WORDS and len(w) > 1]


def text_of(fragment):
    t = re.sub(r"<[^>]+>", " ", str(fragment or ""))
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def links_in(fragment):
    return [html.unescape(h) for h in
            re.findall(r'href=["\']([^"\']+)["\']', str(fragment or ""))
            if not h.startswith("javascript")]


def safe(s, n=80):
    s = re.sub(r"[^A-Za-z0-9.\-]+", "_", s).strip("_")
    return s[:n] or "order"


def read_judges(path="judges.txt"):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def open_page(s):
    r = s.get(PAGE, timeout=60)
    r.raise_for_status()
    t = r.text
    token = re.search(r'"_token"\s*:\s*"([^"]+)"', t)
    cap = re.search(r'id="captcha-code"[^>]*>\s*([^<\s]+)\s*<', t)
    if not token or not cap:
        raise RuntimeError("Could not find the security token or captcha on the page")
    opts = [(v, html.unescape(n).strip()) for v, n in
            re.findall(r'<option value="([^"]+)">([^<]+)</option>', t)]
    return token.group(1), cap.group(1), opts


def solve_captcha(s, token, code):
    r = s.post(BASE + "validateCaptcha",
               data={"_token": token, "captchaInput": code},
               headers={"X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": token,
                        "Referer": PAGE, "Origin": "https://delhihighcourt.nic.in"},
               timeout=60)
    try:
        ok = r.json().get("success") is True
    except ValueError:
        ok = False
    if not ok:
        raise RuntimeError(f"Captcha not accepted (HTTP {r.status_code}): {r.text[:200]}")


def match_judge(name, opts):
    want = words(name)
    exact = [o for o in opts if words(o[1]) == want]
    if exact:
        return exact[0]
    subset = [o for o in opts if want and set(want) <= set(words(o[1]))]
    if len(subset) == 1:
        return subset[0]
    if len(subset) > 1:
        print(f"  ! '{name}' matches several judges: {[o[1] for o in subset]} - please be more specific")
    return None


def dt_params(code, from_d, to_d, start, length, draw):
    p = {"draw": draw, "start": start, "length": length,
         "search[value]": "", "search[regex]": "false",
         "order[0][column]": "1", "order[0][dir]": "desc",
         "s_judge_name": code, "from_date": from_d, "to_date": to_d,
         "hindi_order": "", "btn_view": "", "_": int(time.time() * 1000)}
    cols = [("DT_RowIndex", "DT_RowIndex", False, False),
            (None, "order_date.timestamp", True, True),
            ("case_no_order_link", "case_no_order_link", True, True),
            ("corrigendum", "corrigendum", True, False),
            ("hindi_order", "hindi_order", False, False),
            ("btn_view", "btn_view", False, False)]
    for i, (data, name, srch, orde) in enumerate(cols):
        if data is None:
            p[f"columns[{i}][data][_]"] = "order_date.display"
            p[f"columns[{i}][data][sort]"] = "order_date.timestamp"
        else:
            p[f"columns[{i}][data]"] = data
        p[f"columns[{i}][name]"] = name
        p[f"columns[{i}][searchable]"] = str(srch).lower()
        p[f"columns[{i}][orderable]"] = str(orde).lower()
        p[f"columns[{i}][search][value]"] = ""
        p[f"columns[{i}][search][regex]"] = "false"
    return p


def fetch_rows(s, code, from_d, to_d):
    rows, start, draw, size = [], 0, 1, 100
    while True:
        r = s.get(PAGE, params=dt_params(code, from_d, to_d, start, size, draw),
                  headers={"X-Requested-With": "XMLHttpRequest", "Referer": PAGE,
                           "Accept": "application/json, text/javascript, */*; q=0.01"},
                  timeout=90)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"Unexpected reply (HTTP {r.status_code}): {r.text[:300]}")
        data = j.get("data") or []
        rows.extend(data)
        total = int(j.get("recordsFiltered") or j.get("recordsTotal") or 0)
        start += size
        draw += 1
        if not data or start >= total:
            return rows


def date_display(row):
    d = row.get("order_date")
    if isinstance(d, dict):
        d = d.get("display")
    return text_of(d)


def order_date(row):
    d = date_display(row)
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(d, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return safe(d, 20)


def get_pdf(s, url):
    r = s.get(url, headers={"Referer": PAGE}, timeout=120)
    r.raise_for_status()
    if b"%PDF" not in r.content[:1024]:
        raise RuntimeError(f"Not a PDF (type {r.headers.get('Content-Type')})")
    return r.content


def first_pages_text(content, pages=2):
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:pages])[:6000]
    except Exception:
        return ""


def parties(content, case):
    if not OPENAI_KEY:
        return "", ""
    text = first_pages_text(content)
    if not text.strip():
        print("    (no readable text in PDF - party names left blank)")
        return "", ""
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            json={"model": OPENAI_MODEL,
                  "response_format": {"type": "json_object"},
                  "messages": [
                      {"role": "system", "content": PARTY_PROMPT},
                      {"role": "user", "content": f"Case number: {case}\n\nOrder text:\n{text}"}]},
            timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        j = json.loads(r.json()["choices"][0]["message"]["content"])
        return str(j.get("petitioner") or "").strip(), str(j.get("respondent") or "").strip()
    except Exception as e:
        print(f"    (could not read party names: {e})")
        return "", ""


def upload(content, name, subfolder, extra):
    payload = json.dumps({"secret": DRIVE_SECRET, "name": name, "subfolder": subfolder,
                          "data": base64.b64encode(content).decode(), **extra})
    last = None
    for attempt in range(3):
        try:
            r = requests.post(DRIVE_URL, data=payload,
                              headers={"Content-Type": "text/plain"}, timeout=180)
            try:
                j = r.json()
            except ValueError:
                raise RuntimeError(f"Drive replied with a web page (HTTP {r.status_code}) - "
                                   "check the Apps Script 'Who has access' is set to 'Anyone'")
            if j.get("ok"):
                return j
            raise RuntimeError(f"Drive said: {j.get('error')}")
        except Exception as e:
            last = e
            time.sleep(5)
    raise RuntimeError(f"Upload to Drive failed: {last}")


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=0)


def main():
    if not DRIVE_URL or not DRIVE_SECRET:
        raise SystemExit("The DRIVE_URL / DRIVE_SECRET secrets are missing in GitHub")
    if not OPENAI_KEY:
        print("Note: OPENAI_API_KEY is missing, so party names will be left blank")
    judges = read_judges()
    today = dt.datetime.now(IST).date()
    from_d = (today - dt.timedelta(days=DAYS_BACK)).strftime("%d-%m-%Y")
    to_d = today.strftime("%d-%m-%Y")
    print(f"Looking for orders from {from_d} to {to_d}")

    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            seen = set(json.load(f))
    print(f"Already saved earlier: {len(seen)} orders")

    s = requests.Session()
    s.headers["User-Agent"] = UA
    _, _, opts = open_page(s)
    print(f"Website lists {len(opts)} sitting judges")

    new_files, skipped, failures, streak = 0, 0, 0, 0
    for name in judges:
        print(f"\n=== {name} ===")
        judge = match_judge(name, opts)
        if not judge:
            print("  ! Name not found on the website - check spelling in judges.txt")
            failures += 1
            continue
        code, full_name = judge
        print(f"  Matched: {full_name}")
        try:
            token, cap, _ = open_page(s)
            solve_captcha(s, token, cap)
            rows = fetch_rows(s, code, from_d, to_d)
            print(f"  {len(rows)} orders listed")
            done = 0
            for row in rows:
                if TEST_LIMIT and done >= TEST_LIMIT:
                    break
                case = text_of(row.get("case_no_order_link"))
                for link in links_in(row.get("case_no_order_link")):
                    url = urljoin(PAGE, link)
                    if url in seen:
                        skipped += 1
                        continue
                    tag = hashlib.md5(url.encode()).hexdigest()[:6]
                    fname = f"{order_date(row)}_{safe(case)}_{tag}.pdf"
                    try:
                        content = get_pdf(s, url)
                        pet, resp = parties(content, case)
                        res = upload(content, fname, full_name,
                                     {"case": case, "petitioner": pet, "respondent": resp,
                                      "order_date": date_display(row)})
                        seen.add(url)
                        streak = 0
                        done += 1
                        if res.get("skipped"):
                            skipped += 1
                            print(f"  = already in Drive: {fname}")
                        else:
                            new_files += 1
                            print(f"  + {case} | {pet or '?'} vs {resp or '?'}")
                    except Exception as e:
                        print(f"  ! {fname}: {e}")
                        streak += 1
                        if streak >= 5:
                            save_seen(seen)
                            raise SystemExit("Stopping: 5 failures in a row. See the messages above.")
        except Exception as e:
            print(f"  ! Failed: {e}")
            failures += 1
        save_seen(seen)

    print(f"\nDone. New orders saved to Drive: {new_files}. "
          f"Skipped (already saved): {skipped}. Judges with problems: {failures}")
    if failures and failures == len(judges):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
