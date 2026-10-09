{
    'name': 'Attendance Punching Integration',
    'version': '19.0.1.0.12',
    'summary': 'Module for integrating attendance punching with Odoo',
    'description': """
        This module integrates external attendance punching devices with the Odoo system. 
    """,
    'category': 'Human Resources',
    'license': 'LGPL-3',
    'author': 'Attendance Punching Integration contributors',
    'depends': ['base', 'hr_attendance'],
    'external_dependencies': {'python': ['requests', 'pytz', 'pyzk']},
    'assets': {
        'web.assets_backend': [
            'attendance_punching_integration/static/src/js/attendance_helper_lifecycle.js',
        ],
    },
    'data': [
        'security/ir.model.access.csv',
        'security/attendance_punching_security.xml',
        'views/attendance_punching_view.xml',
        'data/fetch_attendance_cron.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
