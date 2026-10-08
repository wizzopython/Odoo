import requests
from requests.auth import HTTPDigestAuth
import json

url = "http://192.168.0.184:80/ISAPI/AccessControl/AcsEvent?format=json"
auth = HTTPDigestAuth("admin", "wizzo@773311")
payload = {
    "AcsEventCond": {
        "searchID": "1",
        "searchResultPosition": 0,
        "maxResults": 1000,
        "major": 5,
        "minor": 75,
        "startTime": "2026-08-05T00:00:00+00:00"
    }
}
try:
    response = requests.post(url, json=payload, auth=auth, timeout=10)
    print("Status:", response.status_code)
    data = response.json()
    events = data.get("AcsEvent", {}).get("InfoList", [])
    print(f"Total events found: {len(events)}")
    for i, event in enumerate(events[:5]):
        print(f"Event {i}: {json.dumps(event, indent=2)}")
except Exception as e:
    print(e)
