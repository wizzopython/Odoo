import sys
sys.path.append(r"C:\Program Files\Odoo 18.0.20260811\server")
import odoo
from datetime import datetime, timedelta
import pytz
import re
import requests
from requests.auth import HTTPDigestAuth
from collections import defaultdict, namedtuple

odoo.tools.config.parse_config(['-c', r'C:\Program Files\Odoo 18.0.20260811\server\odoo.conf', '-d', 'my_company'])
registry = odoo.registry('my_company')
with registry.cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    
    rec = env['attendance.punching'].search([], limit=1)
    print(f"Device: {rec.name}")
    
    start_time_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if rec.latest_log_time:
        start_time_str = rec.latest_log_time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        
    print(f"Start time: {start_time_str}")
    
    url = f"http://{rec.device_ip}:{rec.device_tcp_com_port}/ISAPI/AccessControl/AcsEvent?format=json"
    auth = HTTPDigestAuth(rec.device_username, rec.device_password)
    payload = {
        "AcsEventCond": {
            "searchID": "1",
            "searchResultPosition": 0,
            "maxResults": 1000,
            "major": 5,
            "minor": 75,
            "startTime": start_time_str
        }
    }
    
    response = requests.post(url, json=payload, auth=auth, timeout=10)
    print(f"Response: {response.status_code}")
    data = response.json()
    events = data.get("AcsEvent", {}).get("InfoList", [])
    print(f"Events: {len(events)}")
    
    class HikvisionRecord:
        def __init__(self, ts, res):
            self.timestamp = ts
            self.result_code = res
        def __str__(self):
            return f"(0, {self.result_code})"
            
    attendances_dict = defaultdict(list)
    for event in events:
        user_id = event.get("employeeNoString")
        time_str = event.get("time")
        if not user_id or not time_str: continue
        time_str_clean = time_str[:19].replace('Z', '')
        time_dt = datetime.strptime(time_str_clean, "%Y-%m-%dT%H:%M:%S")
        att_status = event.get("attendanceStatus", "")
        result_code = 0
        if att_status in ["checkOut", "breakOut", "overtimeOut"]:
            result_code = 1
        attendances_dict[user_id].append(HikvisionRecord(time_dt, result_code))
        
    for user_id, records in attendances_dict.items():
        print(f"Processing user {user_id} with {len(records)} records")
        device_user_employee = env['hr.employee'].search([('device_user_id', '=', user_id)])
        print(f"Employee found: {device_user_employee}")
        if device_user_employee:
            filtered_records = []
            if rec.latest_log_time:
                local_timezone = pytz.timezone('Asia/Muscat')
                latest_log_time_new = rec.latest_log_time.replace(tzinfo=pytz.utc).astimezone(local_timezone).replace(tzinfo=None)
                filtered_records = [r for r in records if r.timestamp >= latest_log_time_new]
            else:
                filtered_records = records
                
            print(f"Filtered records: {len(filtered_records)}")
            for record in filtered_records:
                record_str = str(record)
                match = re.search(r"\((\d+), (\d+)\)", record_str)
                if match:
                    result = int(match.group(2))
                    if result == 0:
                        check_in = pytz.timezone('Asia/Muscat').localize(record.timestamp).astimezone(pytz.utc).replace(tzinfo=None)
                        print(f"Check in: {check_in}")
                        existing = env['hr.attendance'].search([('employee_id', '=', device_user_employee.id), ('check_in', '=', check_in)])
                        if not existing:
                            print(f"Creating attendance for {device_user_employee.id} at {check_in}")
                            env['hr.attendance'].create({'employee_id': device_user_employee.id, 'check_in': check_in})
                            cr.commit()
                        else:
                            print("Attendance exists!")
