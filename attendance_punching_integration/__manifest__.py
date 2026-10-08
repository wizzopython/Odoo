{
    'name': 'Attendance Punching Integration',
    'version': '18.0.0.0.0',
    'summary': 'Module for integrating attendance punching with Odoo',
    'description': """
        This module integrates external attendance punching devices with the Odoo system. 
    """,
    'category': 'Human Resources',
    'license': 'LGPL-3',
    'depends': ['base', 'hr_attendance'],
    'data': [
        'security/ir.model.access.csv',
        'views/attendance_punching_view.xml',
        'data/fetch_attendance_cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
