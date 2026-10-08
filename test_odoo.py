import sys
import os

sys.path.append(r"C:\Program Files\Odoo 18.0.20260811\server")
import odoo

odoo.tools.config.parse_config(['-c', r'C:\Program Files\Odoo 18.0.20260811\server\odoo.conf', '-d', 'my_company'])
registry = odoo.registry('my_company')

with registry.cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    
    employee = env['hr.employee'].search([('device_user_id', '=', '1017')])
    print(f"Employee found: {employee}")
    if employee:
        print(f"Employee name: {employee.name}")
        try:
            att = env['hr.attendance'].create({
                'employee_id': employee.id,
                'check_in': '2015-01-01 00:00:00'
            })
            print(f"Created attendance: {att.id}")
            cr.commit()
        except Exception as e:
            print(f"Error creating attendance: {e}")
