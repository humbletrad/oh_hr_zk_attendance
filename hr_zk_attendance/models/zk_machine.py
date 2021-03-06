import pytz
import sys
import datetime
import logging
import binascii
import pdb
import os
import re
from pprint import pprint, pformat
_logger = logging.getLogger(__name__)

from zk import ZK, const
from zk.attendance import Attendance
from struct import unpack
from odoo import api, fields, models
from odoo import _
from odoo.exceptions import UserError, ValidationError

from odoo.addons.base.models.res_partner import _tz_get

class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    machine_id = fields.Many2one('zk.machine', 'Biometric Device')
    device_id = fields.Char('Biometric Device ID')
    period_number = fields.Integer()

class ZkIssue(models.Model):
    _name = 'hr.zk.issue'
    _description = "Issues with attendance machine"

    machine_id = fields.Many2one('zk.machine', 'Biometric Device ID')
    employee_id = fields.Many2one('hr.employee', 'Employee')
    issue_type = fields.Selection([('missing_in', "Missing Check In"),
                                    ('missing_out', "Missing Check Out"),
                                    ('cross_period', "Check In and Check Out are from different periods"),
                                    ('missing_schedule', "Missing Work Schedule")])
    datetime = fields.Datetime('Related Time')

class ZkMachine(models.Model):
    _name = 'zk.machine'
    _description = 'ZK Machine Configuration'

    name = fields.Char('Machine IP', required=True)
    description = fields.Char()
    port_no = fields.Integer('Port No.', required=True)
    is_udp = fields.Boolean('Is using UDP', default=False)
    address_id = fields.Many2one('res.partner', 'Address')
    company_id = fields.Many2one('res.company', 'Company', default=lambda self: self.env.user.company_id.id)
    password = fields.Integer()
    ignore_time = fields.Integer('Ignore Period', help="Ignore attendance record when the duration is shorter than this.", default=120)
    issue_ids = fields.One2many('hr.zk.issue', 'machine_id', 'Issues')
    issue_count = fields.Integer('Issues Count', compute='_compute_issue_count')
    allow_expired_contracts = fields.Boolean(default=False)

    tz = fields.Selection(_tz_get, 'Timezone', default=lambda self: self._context.get('tz'), required=True)
    tz_offset = fields.Char('Timezone Offset', compute='_compute_tz_offset', invisible=True)
    tz_offset_number = fields.Float('Timezone Offset Numeric', compute='_compute_tz_offset')

    @api.depends('tz')
    def _compute_tz_offset(self):
        for r in self:
            r.tz_offset = datetime.datetime.now(pytz.timezone(r.tz or 'GMT')).strftime('%z')
            device_now = datetime.datetime.now(pytz.timezone(r.tz or 'GMT'))
            r.tz_offset_number = device_now.utcoffset().total_seconds()/60/60
    
    def get_utc_time(self, target_date):
        # why? the machine sends the time, in it's timezone
        # but Odoo inside the code here only deals with UTC time
        from_date = fields.Datetime.from_string(target_date)
        return fields.Datetime.to_string(pytz.timezone(self.tz).localize(from_date, is_dst=None).astimezone(pytz.utc))

    def _compute_issue_count(self):
        for r in self:
            r.issue_count = len(r.issue_ids)

    def test_connection(self):
        for info in self:
            try:
                zk = ZK(info.name, port=info.port_no, timeout=5, password=info.password, force_udp=info.is_udp, ommit_ping=True)
                conn = zk.connect()
                if conn:
                    title = _("Connection Test Succeeded!")
                    message = _("Everything seems properly set up!")
                    return {
                        'type': 'ir.actions.client',
                        'tag': 'display_notification',
                        'params': {
                            'title': title,
                            'message': message,
                            'type': 'info',
                            'sticky': False,
                        }
                    }
                else:
                    raise UserError(_('Unable to connect, please check the parameters and network connections.'))
            except:
                raise ValidationError(_('Warning !!! Machine is not connected'))

    @api.model
    def cron_download(self):
        _logger.info("++++++++++++ ZK Attendance Cron Executed ++++++++++++++++++++++")
        machines = self.env['zk.machine'].search([])
        for machine in machines :
            try:
                machine.download_attendance()
            except Exception as e:
                _logger.error("+++++++++++++++++++ ZK Attendance Machine Exception++++++++++++++++++++++\n{}".format(pformat(e)))

    def create_issue(self, issue_obj, issue_data):
        duplicate_id = issue_obj.search([('employee_id', '=', issue_data['employee_id']),
        ('datetime', '=', issue_data['datetime']),
        ('issue_type', '=', issue_data['issue_type'])
        ])
        if duplicate_id:
            return duplicate_id
        return issue_obj.create(issue_data)

    def download_attendance(self, date_from, date_to):
        zk_attendance = self.env['zk.machine.attendance']
        att_obj = self.env['hr.attendance']
        issue_obj = self.env['hr.zk.issue']
        for info in self:
            zk = ZK(info.name, port=info.port_no, timeout=5, password=info.password, force_udp=info.is_udp, ommit_ping=True)
            conn = zk.connect()
            if not conn:
                raise UserError(_('Unable to connect, please check the parameters and network connections.'))

            conn.disable_device()  # safe to use. The device will re-enable itself automatically if the connection is closed (or this method might not be working at all)
            try:
                attendance = conn.get_attendance()
            except Exception as e:
                _logger.info("+++++++++++++++++++ ZK Attendance Machine Exception++++++++++++++++++++++\n{}".format(pformat(e)))
                attendance = False
            if not attendance:
                raise UserError(_('Unable to get the attendance log (may be empty!), please try again later.'))

            for each in attendance:
                converted_time = info.get_utc_time(each.timestamp)
                datetime_obj = fields.Datetime.from_string(converted_time)
                date_obj = datetime_obj.date()
                if date_from and date_from > date_obj:
                    continue
                if date_to and date_to < date_obj:
                    continue

                biometric_employee_id = self.env['hr.biometric.employee'].search(
                    [('machine_id', '=', info.id), ('device_id', '=', each.user_id)])
                employee_id = biometric_employee_id and biometric_employee_id.employee_id or False
                if not employee_id:
                    continue

                duplicate_attendance_ids = zk_attendance.search(
                    [('machine_id', '=', info.id), ('device_id', '=', each.user_id) 
                    ,('punching_time', '=', converted_time)
                    ])
                if duplicate_attendance_ids:
                    continue

                contract_states = ['open']
                if info.allow_expired_contracts:
                    contract_states.append('close')
                closest_period = employee_id.get_time_period(converted_time, info.tz_offset_number, contract_states)
                if not closest_period['type']:
                    info.create_issue(issue_obj, {
                        'employee_id': employee_id.id,
                        'machine_id': info.id,
                        'datetime': converted_time,
                        'issue_type': 'missing_schedule',
                        })
                    continue

                zk_attendance.create({'employee_id': employee_id.id,
                                    'machine_id': info.id,
                                    'device_id': each.user_id,
                                    'attendance_type': '1',
                                    'punch_type': str(each.punch),
                                    'punching_time': converted_time,
                                    'period_number': closest_period['period'],
                                    'address_id': info.address_id.id})
                att_var = att_obj.search([('employee_id', '=', employee_id.id),
                                            ('check_out', '=', False)])
                if not att_var: # assume check-in
                    if closest_period['type'] == 'check_in':
                        # normal
                        att_obj.create({'employee_id': employee_id.id,
                                        'period_number': closest_period['period'],
                                        'check_in': converted_time})
                    else:
                        # problem: employee didn't check in
                        abnormal_record = att_obj.create({'employee_id': employee_id.id,
                                        'period_number': closest_period['period'],
                                        'check_in': converted_time})
                        abnormal_record.check_out = converted_time
                        info.create_issue(issue_obj, {
                            'employee_id': employee_id.id,
                            'machine_id': info.id,
                            'datetime': converted_time,
                            'issue_type': 'missing_in',
                            })
                else:  # assume check-out
                    time_diff = (fields.Datetime.from_string(converted_time) - att_var.check_in).total_seconds()
                    if time_diff < info.ignore_time:
                        continue
                    if closest_period['type'] == 'check_out' and closest_period['period'] == att_var.period_number and time_diff < 24 * 60 * 60:
                        # normal
                        att_var.write({'check_out': converted_time})
                        if not self.env['hr.work.entry'].search([('date_start', '=', att_var.check_in), ('date_stop', '=', att_var.check_out), ('employee_id', '=', employee_id.id)]):
                            self.env['hr.work.entry'].create({
                                'name': 'Attendance',
                                'employee_id': employee_id.id,
                                'work_entry_type_id': self.env.ref('hr_work_entry.work_entry_type_attendance').id,
                                'date_start': att_var.check_in,
                                'date_stop': att_var.check_out,
                            }).action_validate()
                    else:
                        att_var.check_out = att_var.check_in
                        abnormal_record = att_obj.create({'employee_id': employee_id.id,
                                        'period_number': closest_period['period'],
                                        'check_in': converted_time})
                        abnormal_record.check_out = converted_time
                        # problem: employee didn't check in
                        info.create_issue(issue_obj, {
                            'employee_id': employee_id.id,
                            'machine_id': info.id,
                            'datetime': converted_time,
                            'issue_type': 'missing_out' if closest_period['type'] == 'check_out' else 'cross_period',
                            })
            zk.enable_device()
            zk.disconnect()
            return True
