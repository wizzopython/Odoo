"""Import device punches without inventing missing attendance times."""
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from uuid import uuid4

import pytz
import requests
from requests.auth import HTTPDigestAuth

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

try:
    from zk import ZK
except ImportError:
    ZK = None

_logger = logging.getLogger(__name__)

# Hikvision major 5 minor codes for successful verification: valid card (0x01),
# fingerprint matched (0x26), face matched (0x4b). Failures are excluded.
HIKVISION_PASS_MINORS = {1, 38, 75}


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    device_user_id = fields.Char(string='Punching User ID', copy=False, index=True,
                                 groups='hr.group_hr_user')
    _unique_device_user_id = models.Constraint(
        'UNIQUE(device_user_id)',
        'The Punching User ID must be unique across employees.',
    )


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    # Marks device days with one missing punch. Such records use the only
    # real punch time for both check-in and checkout (zero worked hours).
    punch_issue = fields.Selection([
        ('missing_checkout', 'Missing Check-out'),
        ('missing_checkin', 'Missing Check-in'),
    ], string='Punch Issue', copy=False, readonly=True, index=True)

    def write(self, vals):
        # A manager correcting the times resolves the issue.
        if ('check_in' in vals or 'check_out' in vals) and 'punch_issue' not in vals \
                and not self.env.context.get('attendance_punching_import'):
            vals = dict(vals, punch_issue=False)
        return super().write(vals)


class AttendancePunching(models.Model):
    _name = 'attendance.punching'
    _description = 'Attendance Punching Integration'
    _check_company_auto = True

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    device_type = fields.Selection([
        ('zkteco', 'ZKTeco'), ('hikvision', 'Hikvision'), ('dahua', 'Dahua'),
    ], default='zkteco', required=True)
    device_ip = fields.Char(string='Device IP / Host', required=True)
    device_tcp_com_port = fields.Integer(string='Port', required=True, default=4370)
    device_timezone = fields.Selection(
        [(tz, tz) for tz in pytz.all_timezones], string='Device Time Zone',
        required=True, default=lambda self: self.env.user.tz or 'UTC',
        help='The time zone configured on the physical device. Used for timestamps without an offset.',
    )
    zk_password = fields.Integer(string='ZKTeco Communication Key', default=0)
    allow_overnight = fields.Boolean(
        string='Allow Overnight Shifts', default=False,
        help='By default, check-in and checkout must be on the same date in the device time zone. '
             'Enable only for overnight shifts. A pair may never exceed 24 hours.',
    )
    use_https = fields.Boolean(string='Use HTTPS', default=False)
    device_username = fields.Char(string='Device Username')
    device_password = fields.Char(string='Device Password', copy=False,
                                  groups='hr_attendance.group_hr_attendance_manager')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    import_since = fields.Datetime(
        string='Import From', required=True,
        default=lambda self: fields.Datetime.now() - timedelta(days=7),
        help='Each manual or scheduled fetch reads from this date up to now. Existing punches are ignored.',
    )
    latest_log = fields.Text(string='Latest Result', readonly=True)
    latest_log_time = fields.Datetime(
        string='Last Imported Punch', copy=False,
        readonly=True,
        help='Latest successfully imported punch (UTC). Each fetch reads from Import From; no reset is needed.',
    )

    _valid_port = models.Constraint('CHECK(device_tcp_com_port BETWEEN 1 AND 65535)',
                                    'The port must be between 1 and 65535.')
    _valid_zk_password = models.Constraint('CHECK(zk_password >= 0)',
                                           'The communication key cannot be negative.')

    @api.constrains('device_ip')
    def _check_host(self):
        for rec in self:
            if any(char in rec.device_ip for char in '/?#@') or any(c.isspace() for c in rec.device_ip):
                raise ValidationError(_('Enter only the device IP or host name, without a URL or path.'))

    @api.onchange('device_type', 'use_https')
    def _onchange_device_type(self):
        self.device_tcp_com_port = 4370 if self.device_type == 'zkteco' else (443 if self.use_https else 80)

    def action_fetch_attendance(self):
        self.check_access('write')
        review_required = False
        employees = self.env['hr.employee']
        for rec in self.sorted('id'):
            with self.env.cr.savepoint():
                review_required = rec._fetch_attendance() or review_required
                employees |= rec._configured_employees()
        dashboard = self.env['ir.actions.actions']._for_xml_id('hr_attendance.hr_attendance_action')
        dashboard['domain'] = [('employee_id', 'in', employees.ids)]
        # Do not inherit a previous Today/Month filter: historical pairs must be visible.
        dashboard['context'] = {'search_default_employee': 1}
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Attendance fetch complete'),
                       'message': (_('Employee attendances are shown. Some employee punches need review; see Latest Result on the device.')
                                   if review_required else _('Employee check-ins and checkouts are shown in Attendances.')),
                       'type': 'warning' if review_required else 'success', 'sticky': review_required,
                       'next': dashboard},
        }

    def _configured_employees(self):
        self.ensure_one()
        return self.env['hr.employee'].with_company(self.company_id).search([
            ('active', '=', True), ('company_id', '=', self.company_id.id),
            ('device_user_id', '!=', False), ('device_user_id', '!=', ''),
        ])

    @api.model
    def _cron_fetch_attendance(self):
        for rec in self.search([], order='id'):
            try:
                with self.env.cr.savepoint():
                    rec._fetch_attendance()
            except Exception as exc:
                # The savepoint rolls back this device's writes and checkpoint;
                # one unavailable device must not prevent others from importing.
                _logger.exception('Attendance fetch failed for device %s', rec.id)
                rec.latest_log = _('Fetch failed: %s', str(exc))

    def _fetch_attendance(self):
        self.ensure_one()
        # Both the button and cron replay the selected range. This makes moving
        # Import From earlier and retrying corrected/mapped punches sufficient.
        self.env.cr.execute('SELECT id FROM attendance_punching WHERE id = %s FOR UPDATE', [self.id])
        self.invalidate_recordset(['latest_log_time'])
        start = self.import_since
        end = fields.Datetime.now()
        if start > end:
            raise UserError(_('Import From must not be in the future.'))
        if not self._configured_employees():
            raise UserError(_('Set a Punching User ID on an active employee in this company before fetching attendance.'))
        if self.device_type == 'zkteco':
            punches = self._fetch_zkteco(start, end)
        elif self.device_type == 'hikvision':
            punches = self._fetch_hikvision(start, end)
        elif self.device_type == 'dahua':
            punches = self._fetch_dahua(start, end)
        else:
            raise UserError(_('Unsupported attendance device type.'))
        return self._process_attendance_logs(punches)

    def _as_utc(self, timestamp):
        self.ensure_one()
        if timestamp.tzinfo is None:
            try:
                timestamp = pytz.timezone(self.device_timezone).localize(timestamp, is_dst=None)
            except (pytz.AmbiguousTimeError, pytz.NonExistentTimeError) as exc:
                raise UserError(_('A device time falls in a daylight-saving transition: %s', timestamp)) from exc
        return timestamp.astimezone(pytz.utc).replace(tzinfo=None)

    def _fetch_zkteco(self, start, end):
        self.ensure_one()
        employee_ids = set(self._configured_employees().mapped('device_user_id'))
        if not employee_ids:
            return []
        if ZK is None:
            raise UserError(_("Install the 'pyzk' package in Odoo's Python environment to use ZKTeco."))
        connection = None
        try:
            device = ZK(self.device_ip, port=self.device_tcp_com_port, timeout=30,
                        password=self.zk_password, force_udp=False, ommit_ping=True)
            connection = device.connect()
            if not connection:
                raise UserError(_('Could not connect to the ZKTeco device.'))
            punches = []
            for record in connection.get_attendance() or []:
                if str(record.user_id) not in employee_ids:
                    continue
                timestamp = self._as_utc(record.timestamp)
                if not start <= timestamp <= end:
                    continue
                # Use pyzk's structured punch field; status is the verification method.
                direction = {0: 'in', 1: 'out', 4: 'in', 5: 'out'}.get(record.punch)
                # Validate direction after employee matching so unrelated users
                # cannot block imports with an unsupported device punch code.
                punches.append((str(record.user_id), timestamp, direction))
            return punches
        except UserError:
            raise
        except Exception as exc:
            raise UserError(_('ZKTeco fetch failed: %s', str(exc))) from exc
        finally:
            if connection:
                try:
                    connection.disconnect()
                except Exception:
                    _logger.warning('Could not disconnect device %s', self.id, exc_info=True)

    @api.model
    def _parse_dahua_page(self, body):
        """Read only the documented fields; do not retain sensitive card data."""
        headers, records = {}, {}
        for line in body.splitlines():
            key, separator, value = line.partition('=')
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key in ('found', 'totalcount'):
                if key in headers:
                    raise UserError(_('Dahua returned duplicate response counters.'))
                headers[key] = int(value)
            match = re.fullmatch(r'records\[(\d+)\]\.(\w+)', key)
            if match:
                index, field = int(match[1]), match[2]
                record = records.setdefault(index, {})
                if field in ('recno', 'userid', 'createtime', 'status', 'type'):
                    if field in record:
                        raise UserError(_('Dahua returned a duplicate record field.'))
                    record[field] = value
        if 'found' not in headers or 'totalcount' not in headers:
            raise UserError(_(
                'Dahua did not return the expected CGI record counters. '
                'Verify CGI access and the firmware API; no attendance was imported.'))
        found, total = headers['found'], headers['totalcount']
        if found < 0 or total < found or set(records) != set(range(found)):
            raise UserError(_('Dahua returned an incomplete record page; no attendance was imported.'))
        return total, [records[i] for i in range(found)]

    def _fetch_dahua(self, start, end):
        self.ensure_one()
        employee_ids = set(self._configured_employees().mapped('device_user_id'))
        if not employee_ids:
            return []
        if not self.device_username or not self.device_password:
            raise UserError(_('Enter the Dahua device username and password.'))
        host = self.device_ip
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        protocol = 'https' if self.use_https else 'http'
        url = f'{protocol}://{host}:{self.device_tcp_com_port}/cgi-bin/recordFinder.cgi'
        first = int(pytz.utc.localize(start).timestamp())
        last = int(pytz.utc.localize(end).timestamp())
        windows = [(first, last)]
        punches = set()
        requests_made = 0
        try:
            with requests.Session() as session:
                session.auth = HTTPDigestAuth(self.device_username, self.device_password)
                while windows:
                    lower, upper = windows.pop()
                    requests_made += 1
                    if requests_made > 10000:
                        raise UserError(_('Too many Dahua record requests. Use a shorter import period.'))
                    response = session.get(url, params={
                        'action': 'find', 'name': 'AccessControlCardRec',
                        'StartTime': lower, 'EndTime': upper, 'count': 1024,
                    }, timeout=30)
                    response.raise_for_status()
                    total, records = self._parse_dahua_page(response.text)
                    # Check completeness using ALL records, before employee filtering.
                    # A wrong/ignored time filter must never silently lose old data.
                    seen = set()
                    for record in records:
                        number = record['recno']
                        if number in seen:
                            raise UserError(_('Dahua repeated a record within a page. Retry the fetch.'))
                        seen.add(number)
                        stamp = int(record['createtime'])
                        if not lower <= stamp <= upper:
                            raise UserError(_(
                                'Dahua returned records outside the requested time range. '
                                'Verify the firmware CGI time-filter format.'))
                    if total > len(records):
                        # Query disjoint inclusive-second ranges rather than assuming
                        # page ordering or skipping tied timestamps at a page boundary.
                        if lower == upper:
                            raise UserError(_(
                                'Dahua has more records in one second than its CGI response can return. '
                                'No partial import was saved; this device needs a paginated SDK connector.'))
                        middle = (lower + upper) // 2
                        windows.extend([(middle + 1, upper), (lower, middle)])
                        continue
                    for record in records:
                        user_id = record.get('userid')
                        if user_id not in employee_ids:
                            continue
                        result = record.get('status')
                        if result == '0':
                            continue  # Failed access attempts are not attendance.
                        if result != '1':
                            raise UserError(_(
                                'A Dahua employee record has no recognized success status. '
                                'Verify the firmware record format before importing attendance.'))
                        stamp = datetime.fromtimestamp(int(record['createtime']), pytz.utc).replace(tzinfo=None)
                        if start <= stamp <= end:
                            direction = {'Entry': 'in', 'Exit': 'out'}.get(record.get('type'))
                            if direction is None:
                                raise UserError(_(
                                    'A Dahua employee record has no Entry/Exit direction. '
                                    'Check device attendance settings and the firmware API; no times were guessed.'))
                            punches.add((user_id, stamp, direction))
            return sorted(punches, key=lambda p: (p[1], p[0], p[2]))
        except UserError:
            raise
        except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError, OSError) as exc:
            raise UserError(_('Dahua fetch failed: %s', str(exc))) from exc

    def _fetch_hikvision(self, start, end):
        self.ensure_one()
        employee_ids = set(self._configured_employees().mapped('device_user_id'))
        if not employee_ids:
            return []
        if not self.device_username or not self.device_password:
            raise UserError(_('Enter the Hikvision username and password.'))
        protocol = 'https' if self.use_https else 'http'
        host = self.device_ip
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        url = f'{protocol}://{host}:{self.device_tcp_com_port}/ISAPI/AccessControl/AcsEvent?format=json'
        auth = HTTPDigestAuth(self.device_username, self.device_password)
        # Request every access-control event (minor 0 = all) and keep only
        # successful verifications locally. Filtering on face-only (75) missed
        # employees who punch by fingerprint. Failed attempts (e.g. 39) and
        # door open/close events are never attendance.
        condition = {
            'searchID': str(uuid4()), 'searchResultPosition': 0, 'maxResults': 100,
            'major': 5, 'minor': 0,
            'startTime': start.isoformat(timespec='seconds') + '+00:00',
            'endTime': end.isoformat(timespec='seconds') + '+00:00',
        }
        punches = []
        previous_page = None
        authenticated_pages = 0
        session = requests.Session()
        try:
            for _page in range(10000):
                payload = {'AcsEventCond': dict(condition)}
                response = session.post(url, json=payload, auth=auth, timeout=30)
                # Some devices stop accepting a reused Digest nonce after several
                # pages, returning 401 WITHOUT WWW-Authenticate. Requests cannot
                # refresh that challenge automatically. After proven login success,
                # retry this same read-only page once with a fresh Digest handshake.
                # Never loop on bad initial credentials or advance past a failed page.
                if (response.status_code == 401 and authenticated_pages
                        and not response.headers.get('WWW-Authenticate')):
                    response.close()
                    auth = HTTPDigestAuth(self.device_username, self.device_password)
                    response = session.post(url, json=payload, auth=auth, timeout=30)
                response.raise_for_status()
                authenticated_pages += 1
                data = response.json().get('AcsEvent')
                if not isinstance(data, dict):
                    raise UserError(_('The device did not return an AcsEvent response. Check ISAPI support and permissions.'))
                events = data.get('InfoList') or []
                status = data.get('responseStatusStrg', '')
                if status not in ('', 'OK', 'MORE', 'NO MATCH'):
                    raise UserError(_('Hikvision search failed: %s', status))
                if events and events == previous_page:
                    raise UserError(_('The device repeated a search page; no checkpoint was advanced.'))
                previous_page = events
                for event in events:
                    if event.get('minor') not in HIKVISION_PASS_MINORS:
                        continue
                    user_id = event.get('employeeNoString') or event.get('employeeNo')
                    if user_id is None or str(user_id) not in employee_ids:
                        # Discard other device IDs before parsing or storing their data.
                        continue
                    timestamp = self._as_utc(datetime.fromisoformat(event['time'].replace('Z', '+00:00')))
                    if not start <= timestamp <= end:
                        continue
                    direction = {
                        'checkIn': 'in', 'checkOut': 'out', 'breakIn': 'in',
                        'breakOut': 'out', 'overtimeIn': 'in', 'overtimeOut': 'out',
                    }.get(event.get('attendanceStatus'))
                    # Unmapped users are skipped before direction validation.
                    punches.append((str(user_id), timestamp, direction))
                condition['searchResultPosition'] += len(events)
                total = int(data.get('totalMatches', 0))
                more = status == 'MORE' or condition['searchResultPosition'] < total
                if not more:
                    return punches
                if not events:
                    raise UserError(_('Hikvision returned an incomplete search page. Retry the fetch.'))
            raise UserError(_('Too many Hikvision search pages. Use a shorter import period.'))
        except UserError:
            raise
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise UserError(_('Hikvision fetch failed: %s', str(exc))) from exc
        finally:
            session.close()

    def _process_attendance_logs(self, punches):
        """Import historical pairs and current check-ins in one replayable flow.

        Complete pairs are inserted atomically, allowing backfill beside a later
        open attendance. Only real device checkouts can close an existing open
        record. Incomplete/ambiguous/conflicting sequences are reported, not
        assigned invented times. Caller owns the transaction/savepoint.
        """
        self.ensure_one()
        Attendance = self.env['hr.attendance'].with_company(self.company_id).with_context(
            attendance_punching_import=True)
        employees = self.env['hr.employee'].with_company(self.company_id).search([
            ('active', '=', True), ('company_id', '=', self.company_id.id),
            ('device_user_id', 'in', list({p[0] for p in punches})),
        ])
        by_user = {employee.device_user_id: employee for employee in employees}
        if employees:
            self.env.cr.execute('SELECT id FROM hr_employee WHERE id IN %s ORDER BY id FOR UPDATE',
                                [tuple(employees.ids)])
        grouped = defaultdict(list)
        checkins = checkouts = duplicates = 0
        issues = []
        imported_times = []
        timezone = pytz.timezone(self.device_timezone)

        def local(timestamp):
            return pytz.utc.localize(timestamp).astimezone(timezone)

        today = local(fields.Datetime.now()).date()

        def describe(user_id, timestamp, reason):
            issues.append(_('ID %s at %s: %s', user_id,
                            local(timestamp).strftime('%Y-%m-%d %H:%M:%S %Z'), reason))

        def overlaps(employee, start, end=None, exclude=None):
            domain = [('employee_id', '=', employee.id),
                      '|', ('check_out', '=', False), ('check_out', '>', start)]
            if end is not None:
                domain.append(('check_in', '<', end))
            if exclude:
                domain.append(('id', '!=', exclude.id))
            return Attendance.search_count(domain)

        def may_stay_open(timestamp):
            # Today's check-in (or a recent overnight one) may still get a real checkout.
            return local(timestamp).date() == today or (
                self.allow_overnight and fields.Datetime.now() - timestamp <= timedelta(hours=24))

        def import_pair(employee, user_id, start, end):
            nonlocal checkins, checkouts, duplicates
            if start >= end:
                describe(user_id, end, _('Checkout must be later than check-in.'))
                return False
            # Apply before duplicate detection and before updating an open record:
            # a stale check-in must not consume a later day's unrelated checkout.
            if end - start > timedelta(hours=24):
                describe(user_id, start, _(
                    'Checkout at %s is more than 24 hours later; punches were not paired. Verify the missing check-in/checkout.',
                    local(end).strftime('%Y-%m-%d %H:%M:%S %Z')))
                return False
            if not self.allow_overnight and local(start).date() != local(end).date():
                describe(user_id, start, _(
                    'Checkout at %s is on a different date; punches were not paired. Verify the missing check-in/checkout.',
                    local(end).strftime('%Y-%m-%d %H:%M:%S %Z')))
                return False
            existing = Attendance.search([
                ('employee_id', '=', employee.id), ('check_in', '=', start),
            ], limit=1)
            if existing and existing.check_out == end:
                duplicates += 2
                imported_times.extend([start, end])
                return True
            if existing and existing.check_out:
                describe(user_id, start, _('Existing checkout differs from the device; existing record preserved.'))
                return False
            if overlaps(employee, start, end, existing):
                describe(user_id, start, _('Pair overlaps another attendance; existing records preserved.'))
                return False
            try:
                with self.env.cr.savepoint():
                    if existing:
                        existing.write({'check_out': end, 'out_mode': 'technical'})
                    else:
                        Attendance.create({
                            'employee_id': employee.id, 'check_in': start, 'check_out': end,
                            'in_mode': 'technical', 'out_mode': 'technical',
                        })
            except ValidationError as exc:
                describe(user_id, start, str(exc))
                return False
            checkins += int(not existing)
            checkouts += 1
            imported_times.extend([start, end])
            return True

        def import_single_punch(employee, user_id, timestamp, issue):
            """Show a day with one missing punch using its only real punch time
            as both check-in and checkout, flagged for the manager to correct."""
            nonlocal checkins, checkouts, duplicates
            field = 'check_in' if issue == 'missing_checkout' else 'check_out'
            existing = Attendance.search([
                ('employee_id', '=', employee.id), (field, '=', timestamp),
            ], limit=1)
            if existing and existing.check_out:
                duplicates += 1
                imported_times.append(timestamp)
                return
            if existing and existing.in_mode != 'technical':
                describe(user_id, timestamp, _('Open attendance was not created by the device; existing record preserved.'))
                return
            if not existing and overlaps(employee, timestamp, timestamp + timedelta(seconds=1)):
                describe(user_id, timestamp, _('Punch conflicts with another attendance; existing records preserved.'))
                return
            try:
                with self.env.cr.savepoint():
                    if existing:
                        # An earlier fetch saved this as today's open check-in.
                        existing.write({'check_out': timestamp, 'out_mode': 'technical', 'punch_issue': issue})
                    else:
                        Attendance.create({
                            'employee_id': employee.id, 'check_in': timestamp, 'check_out': timestamp,
                            'in_mode': 'technical', 'out_mode': 'technical', 'punch_issue': issue,
                        })
            except ValidationError as exc:
                describe(user_id, timestamp, str(exc))
                return
            if issue == 'missing_checkout':
                checkins += int(not existing)
                describe(user_id, timestamp, _('No checkout punch; shown as Missing Check-out with checkout = check-in.'))
            else:
                checkouts += 1
                describe(user_id, timestamp, _('No check-in punch; shown as Missing Check-in with check-in = checkout.'))
            imported_times.append(timestamp)

        for user_id, timestamp, direction in sorted(set(punches), key=lambda p: (p[1], p[0], p[2] or '')):
            if user_id not in by_user:
                continue
            else:
                grouped[user_id].append((timestamp, direction))

        for user_id, events in grouped.items():
            employee = by_user[user_id]
            pending = []
            for timestamp, direction in events:
                if direction != 'out':
                    if direction == 'in' and pending and local(pending[-1][0]).date() < local(timestamp).date():
                        if len(pending) == 1 and pending[0][1] == 'in':
                            import_single_punch(employee, user_id, pending[0][0], 'missing_checkout')
                        else:
                            describe(user_id, pending[0][0], _(
                                '%s earlier punches have no checkout before the next day check-in; left for review.',
                                len(pending)))
                        pending = []
                    # Repeated check-ins on the same day or undefined statuses
                    # make a sequence ambiguous; never choose a guessed start.
                    pending.append((timestamp, direction))
                    continue
                if not pending:
                    existing_out = Attendance.search([
                        ('employee_id', '=', employee.id), ('check_out', '=', timestamp),
                    ], limit=1)
                    if existing_out:
                        duplicates += 1
                        imported_times.append(timestamp)
                        continue
                    earlier_open = Attendance.search([
                        ('employee_id', '=', employee.id), ('check_out', '=', False),
                        ('check_in', '<', timestamp),
                    ], order='check_in desc', limit=1)
                    if not (earlier_open and import_pair(employee, user_id, earlier_open.check_in, timestamp)):
                        import_single_punch(employee, user_id, timestamp, 'missing_checkin')
                    continue
                if len(pending) != 1 or pending[0][1] != 'in':
                    describe(user_id, pending[0][0], _(
                        'Checkout at %s has %s preceding punches with no unambiguous check-in; left for review.',
                        local(timestamp).strftime('%Y-%m-%d %H:%M:%S %Z'), len(pending)))
                    pending = []
                    continue
                import_pair(employee, user_id, pending[0][0], timestamp)
                pending = []

            if not pending:
                continue
            timestamp, direction = pending[0]
            if len(pending) != 1 or direction != 'in':
                describe(user_id, timestamp, _('%s punches have no unambiguous checkout; left for review.', len(pending)))
                continue
            if not may_stay_open(timestamp):
                import_single_punch(employee, user_id, timestamp, 'missing_checkout')
                continue
            existing_in = Attendance.search([
                ('employee_id', '=', employee.id), ('check_in', '=', timestamp),
            ], limit=1)
            if existing_in:
                duplicates += 1
                imported_times.append(timestamp)
                continue
            if overlaps(employee, timestamp):
                describe(user_id, timestamp, _('Check-in conflicts with another attendance; existing records preserved.'))
                continue
            try:
                with self.env.cr.savepoint():
                    Attendance.create({'employee_id': employee.id, 'check_in': timestamp, 'in_mode': 'technical'})
            except ValidationError as exc:
                describe(user_id, timestamp, str(exc))
            else:
                checkins += 1
                imported_times.append(timestamp)

        if imported_times:
            self.latest_log_time = max(imported_times + ([self.latest_log_time] if self.latest_log_time else []))
        summary = [_('Imported %s check-ins, %s checkouts; %s existing punches ignored.',
                     checkins, checkouts, duplicates)]
        if issues:
            summary.append(_('Needs review (%s items):', len(issues)))
            summary.extend(issues)
        summary.append(_('Read from Import From up to now. Dates above use the device time zone.'))
        self.latest_log = '\n'.join(summary)
        return bool(issues)
