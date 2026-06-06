import csv
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

SERPAPI_URL = "https://serpapi.com/search.json"
JST = timezone(timedelta(hours=9))

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.csv"))
LOG_PATH = Path(os.getenv("LOG_PATH", "rank_log.csv"))

SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "1Aa9eB0N0BAESe1TZk-y90pc6ryhvtcOhLZjrsGUSiSk")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", "service-account.json")
ENABLE_GOOGLE_SHEETS = os.getenv("ENABLE_GOOGLE_SHEETS", "TRUE").strip().lower() in {"true", "1", "yes", "y", "on"}

DEFAULT_LAT = os.getenv("DEFAULT_LAT", "35.6670")
DEFAULT_LNG = os.getenv("DEFAULT_LNG", "139.6000")
DEFAULT_ZOOM = os.getenv("DEFAULT_ZOOM", "14")


def normalize(text: str) -> str:
    return "".join((text or "").lower().replace("　", " ").split())


def is_enabled(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "on"}


def load_config(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"config.csv not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if is_enabled(r.get("enabled", "TRUE"))]


def build_ll(conf: Dict[str, str]) -> str:
    lat = (conf.get("lat") or DEFAULT_LAT).strip()
    lng = (conf.get("lng") or DEFAULT_LNG).strip()
    zoom = (conf.get("zoom") or DEFAULT_ZOOM).strip().replace("z", "")
    return f"@{lat},{lng},{zoom}z"


def fetch_google_maps_results(keyword: str, ll: str, max_pages: int = 2) -> List[Dict[str, Any]]:
    if not SERPAPI_API_KEY:
        raise RuntimeError("SERPAPI_API_KEY is not set. Put it in .env or GitHub Secrets.")

    all_results: List[Dict[str, Any]] = []
    params = {
        "engine": "google_maps",
        "q": keyword,
        "hl": "ja",
        "gl": "jp",
        "type": "search",
        "ll": ll,
        "api_key": SERPAPI_API_KEY,
    }

    for _ in range(max_pages):
        response = requests.get(SERPAPI_URL, params=params, timeout=30)
        data = response.json()
        if "error" in data:
            raise RuntimeError(data["error"])

        all_results.extend(data.get("local_results", []))

        next_page_token = data.get("serpapi_pagination", {}).get("next_page_token")
        if not next_page_token:
            break
        params["next_page_token"] = next_page_token
        time.sleep(2)

    return all_results


def find_store(results: List[Dict[str, Any]], match_name: str) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    target = normalize(match_name)
    for idx, item in enumerate(results, start=1):
        title = normalize(item.get("title", ""))
        if target and (target in title or title in target):
            return idx, item
    return None, None


def append_csv_rows(rows: List[Dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    exists = path.exists()
    fieldnames = list(rows[0].keys())
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def get_spreadsheet():
    if not ENABLE_GOOGLE_SHEETS:
        return None
    if gspread is None or Credentials is None:
        raise RuntimeError("gspread/google-auth is not installed. Run: pip3 install -r requirements.txt")
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is not set in .env")
    if not Path(SERVICE_ACCOUNT_FILE).exists():
        raise FileNotFoundError(f"Service account JSON not found: {SERVICE_ACCOUNT_FILE}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


def ensure_worksheet(spreadsheet, sheet_name: str, headers: List[str]):
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=max(20, len(headers)))
        ws.append_row(headers)
        return ws

    # Read only first row once. Avoid get_all_values() because it quickly hits quota.
    first_row = ws.row_values(1)
    if not first_row:
        ws.append_row(headers)
    return ws


def append_sheet_rows(spreadsheet, sheet_name: str, rows: List[Dict[str, Any]]) -> None:
    if not spreadsheet or not rows:
        return
    headers = list(rows[0].keys())
    ws = ensure_worksheet(spreadsheet, sheet_name, headers)
    values = [[row.get(h, "") for h in headers] for row in rows]
    ws.append_rows(values, value_input_option="USER_ENTERED")


def main() -> None:
    checked_at = datetime.now(JST).isoformat(timespec="seconds")
    today = datetime.now(JST).date().isoformat()

    configs = load_config(CONFIG_PATH)
    if not configs:
        print("No enabled rows in config.csv")
        return

    rank_rows: List[Dict[str, Any]] = []

    for conf in configs:
        store_name = conf.get("store_name", "")
        match_name = conf.get("match_name", store_name)
        keyword = conf.get("keyword", "")
        location_label = conf.get("location", "")
        max_pages = int(conf.get("max_pages", "2") or 2)
        ll = build_ll(conf)

        try:
            results = fetch_google_maps_results(keyword, ll, max_pages=max_pages)
            rank, item = find_store(results, match_name)

            rank_rows.append({
                "checked_at": checked_at,
                "date": today,
                "store_name": store_name,
                "keyword": keyword,
                "location": location_label,
                "ll": ll,
                "rank": rank if rank is not None else "NOT_FOUND",
                "rank_numeric": rank if rank is not None else "",
                "found_title": item.get("title", "") if item else "",
                "rating": item.get("rating", "") if item else "",
                "reviews": item.get("reviews", "") if item else "",
                "address": item.get("address", "") if item else "",
                "data_cid": item.get("data_cid", "") if item else "",
                "place_id": item.get("place_id", "") if item else "",
                "status": "OK",
                "note": "",
            })
            print(f"{store_name} / {keyword}: {rank if rank else 'NOT_FOUND'}")

        except Exception as e:
            rank_rows.append({
                "checked_at": checked_at,
                "date": today,
                "store_name": store_name,
                "keyword": keyword,
                "location": location_label,
                "ll": ll,
                "rank": "ERROR",
                "rank_numeric": "",
                "found_title": "",
                "rating": "",
                "reviews": "",
                "address": "",
                "data_cid": "",
                "place_id": "",
                "status": "ERROR",
                "note": str(e),
            })
            print(f"ERROR {store_name} / {keyword}: {e}")

    # Local CSV backup
    append_csv_rows(rank_rows, LOG_PATH)

    # Google Sheets batch write: only 2 writes per run, avoiding 429 quota errors.
    if ENABLE_GOOGLE_SHEETS:
        spreadsheet = get_spreadsheet()
        append_sheet_rows(spreadsheet, "rank_log", rank_rows)
        time.sleep(1)
        append_sheet_rows(spreadsheet, "daily_rank", rank_rows)

    print(f"Saved {len(rank_rows)} rank rows.")


if __name__ == "__main__":
    main()
