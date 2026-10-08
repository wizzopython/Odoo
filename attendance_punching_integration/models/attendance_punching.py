import logging
import re
from encodings.punycode import selective_find

from odoo import models, fields, api, _
from collections import defaultdict
from datetime import timedelta, datetime

import requests
from requests.auth import HTTPDigestAuth
import json

_logger = logging.getLogger(__name__)

import pytz

from odoo.exceptions import UserError
try:
    from zk import ZK
except ImportError:
    ZK = None
    _logger.warning("zk library not installed. ZKTeco devices will not work.")

class HrEmployee(models.Model):
    _inherit = "hr.employee"

    device_user_id = fields.Char()

    _sql_constraints = [
        ('unique_device_user_id', 'unique(device_user_id)', 'Employee id is exist another one employee,The Device User ID must be unique.')
    ]

#
class AttendancePunching(models.Model):
    _name = 'attendance.punching'
    _description = 'Attendance Punching Integration'

    name = fields.Char(string="Name")
    device_type = fields.Selection([
        ('zkteco', 'ZKTeco'),
        ('hikvision', 'Hikvision')
    ], string="Device Type", default='zkteco', required=True)
    device_ip = fields.Char(string="Device IP", required=True)
    device_tcp_com_port = fields.Integer(string="Port No", required=True)
    device_username = fields.Char(string="Username (Hikvision)")
    device_password = fields.Char(string="Password (Hikvision)")
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.user.company_id.id)
    latest_log = fields.Char(string="Latest Log")
    latest_log_time = fields.Datetime()

    def action_fetch_attendance(self):
        self._cron_fetch_attendance()

    @api.model
    def _cron_fetch_attendance(self):
        records = self.search([])
        for rec in records:
            machine_ip = rec.device_ip
            port = rec.device_tcp_com_port
            timeout = 120

            if rec.device_type == 'zkteco':
                if not ZK:
                    raise UserError(_("The 'zk' python library is not installed. Please install it to use ZKTeco devices."))
                zk = ZK(machine_ip, port=port, timeout=timeout, password=0, force_udp=False, ommit_ping=True)
                conn = zk.connect()

                if conn:
                    zk.enable_device()
                    # Fetch attendance logs
                    attendances = zk.get_attendance()
                    rec.latest_log = attendances
                    if attendances:
                        attendances_dict = defaultdict(list)
                        for record in attendances:
                            attendances_dict[record.user_id].append(record)
                        self._process_attendance_logs(rec, attendances_dict)

            elif rec.device_type == 'hikvision':
                url = f"http://{machine_ip}:{port}/ISAPI/AccessControl/AcsEvent?format=json"
                auth = HTTPDigestAuth(rec.device_username or '', rec.device_password or '')
                
                start_time_str = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                if rec.latest_log_time:
                    start_time_str = rec.latest_log_time.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                
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
                
                try:
                    response = requests.post(url, json=payload, auth=auth, timeout=timeout)
                    if response.status_code == 200:
                        data = response.json()
                        events = data.get("AcsEvent", {}).get("InfoList", [])
                        
                        rec.latest_log = f"{len(events)} events (Hikvision)"
                        
                        if events:
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
                                if not user_id or not time_str:
                                    continue
                                
                                # parse "2023-01-01T12:00:00+08:00"
                                time_str_clean = time_str[:19].replace('Z', '')
                                time_dt = datetime.strptime(time_str_clean, "%Y-%m-%dT%H:%M:%S")
                                
                                att_status = event.get("attendanceStatus", "")
                                result_code = 0
                                if att_status in ["checkOut", "breakOut", "overtimeOut"]:
                                    result_code = 1
                                
                                attendances_dict[user_id].append(HikvisionRecord(time_dt, result_code))
                                
                            self._process_attendance_logs(rec, attendances_dict)
                except Exception as e:
                    _logger.error(f"Hikvision connection error: {e}")

    def _process_attendance_logs(self, rec, attendances_dict):
        _logger.info(f"Processing attendances: {attendances_dict}")
        for user_id, records in attendances_dict.items():
            _logger.info(f"User {user_id} has {len(records)} records")
            if rec.latest_log_time:

                utc_timezone = pytz.utc

                # Define target timezone
                target_timezone = pytz.timezone('Asia/Muscat')

                # Convert UTC datetime to target timezone
                localized_datetime = utc_timezone.localize(rec.latest_log_time)
                target_datetime = localized_datetime.astimezone(target_timezone)

                # Convert localized datetime to naive datetime (without timezone info)
                latest_log_time_new = target_datetime.replace(tzinfo=None)

                filtered_records = [record for record in records if record.timestamp >= latest_log_time_new]
            else:
                filtered_records = records

            for record in filtered_records:

                employee_records = self.env['hr.employee']
                device_user_employee = employee_records.search([('device_user_id', '=', user_id)])
                if device_user_employee:
                    record_str = str(record)
                    match = re.search(r"\((\d+), (\d+)\)", record_str)
                    check_in = None
                    check_out = None
                    if match:
                        result = int(match.group(2))
                        hr_attendance = self.env['hr.attendance']

                        if result == 0 or result == 4:
                            check_in_timestamp = record.timestamp
                            local_timezone = pytz.timezone('Asia/Muscat')
                            local_datetime = local_timezone.localize(check_in_timestamp)
                            check_in = local_datetime.astimezone(pytz.utc).replace(tzinfo=None)

                            miss_checkouts = hr_attendance.search([
                                ('employee_id', '=', device_user_employee.id),
                                ('check_in', '<=', check_in),
                                ('check_out', '=', False)
                            ],limit=1)

                            if miss_checkouts:
                                miss_checkouts.write({
                                    'check_out': miss_checkouts.check_in + timedelta(seconds=1),
                                })

                            existing_attendance = hr_attendance.search([
                                ('employee_id', '=', device_user_employee.id),
                                ('check_in', '=', check_in),
                            ], limit=1)
                            if not existing_attendance:
                                _logger.info(f"Creating attendance for {device_user_employee.name} at {check_in}")
                                data = hr_attendance.create({
                                    'employee_id': device_user_employee.id,
                                    'check_in': check_in,
                                })
                                _logger.info(f"Created: {data}")
                                if data and rec.device_type == 'zkteco':
                                    records_to_remove = []
                                    for rec_to_remove in records:
                                        if rec_to_remove.timestamp == record.timestamp:
                                            records_to_remove.append(rec_to_remove)

                                    # After iteration, remove the marked records
                                    for rec_to_remove in records_to_remove:
                                        records.remove(rec_to_remove)

                        elif result == 1 or result == 5:
                            check_out_timestamp = record.timestamp
                            local_timezone = pytz.timezone('Asia/Muscat')
                            local_datetime = local_timezone.localize(check_out_timestamp)
                            check_out = local_datetime.astimezone(pytz.utc).replace(tzinfo=None)

                            existing_attendance_checkout = hr_attendance.search([
                                ('employee_id', '=', device_user_employee.id),
                                ('check_out', '=', check_out),
                            ], limit=1)

                            if not existing_attendance_checkout:
                                existing_attendance_checkin = hr_attendance.search([
                                    ('employee_id', '=', device_user_employee.id),
                                    ('check_out', '=', False)
                                ], order="check_in desc", limit=1)
                                if existing_attendance_checkin:
                                    existing_attendance_checkin.write({
                                        'check_out': check_out,
                                    })
                                else:
                                    hr_attendance.create({
                                        'employee_id': device_user_employee.id,
                                        'check_in': check_out - timedelta(seconds=1),
                                        'check_out': check_out,
                                    })
        rec.latest_log_time = datetime.now()
