import sys
sys.path.append(r"C:\Program Files\Odoo 18.0.20260811\server")
import odoo
odoo.tools.config.parse_config(['-c', r'C:\Program Files\Odoo 18.0.20260811\server\odoo.conf', '-d', 'my_company'])
registry = odoo.registry('my_company')
with registry.cursor() as cr:
    env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
    try:
        recs = env['attendance.punching'].search([])
        for rec in recs:
            print(f"Fetching for {rec.name}...")
            rec.action_fetch_attendance()
        print("Success!")
        cr.commit()
    except Exception as e:
        print(f"ERROR: {e}")
