
import concurrent.futures
import requests
import uuid
from datetime import datetime, timezone

BASE_URL = "http://127.0.0.1:8000"
EMP_CODE = "EMP398863"
REQUEST_COUNT = 10

# Use a unique date so an existing attendance record won't interfere.
# First ensure this employee exists in your local database.
punched_at = int(datetime.now(timezone.utc).timestamp() * 1000)

payload = {
    "emp_code": EMP_CODE,
    "punched_at": punched_at,
    "status": "PRESENT"
}

def send_punch_in(_):
    try:
        response = requests.post(
            f"{BASE_URL}/attendance/punch-in",
            json=payload,
            timeout=15
        )
        return response.status_code, response.text
    except requests.RequestException as exc:
        return "ERROR", str(exc)

if __name__ == "__main__":
    # All requests use the same employee and timestamp.
    with concurrent.futures.ThreadPoolExecutor(max_workers=REQUEST_COUNT) as pool:
        results = list(pool.map(send_punch_in, range(REQUEST_COUNT)))

    for i, (status, body) in enumerate(results, 1):
        print(f"Request {i}: HTTP {status}")

    statuses = [status for status, _ in results]
    print("\nSummary:")
    print("201 Created:", statuses.count(201))
    print("409 Conflict:", statuses.count(409))
    print("Other responses:", len([
        s for s in statuses if s not in (201, 409)
    ]))
