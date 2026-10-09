/** @odoo-module **/

import { onWillStart, status, useState } from "@odoo/owl";
import { AttendanceActionHelper } from "@hr_attendance/views/attendance_helper_view";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";

// Odoo 19's helper may disappear while permissions are loading (for example
// when attendance rows arrive or the user changes grouping). Its original
// startup then calls a protected ORM service on a destroyed component.
patch(AttendanceActionHelper.prototype, {
    setup() {
        // Replace the original setup: calling super would also register the
        // original, unguarded onWillStart callback.
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.state = useState({ hasDemoData: false });

        onWillStart(async () => {
            const [isHrUser, hasAttendanceRight] = await Promise.all([
                user.hasGroup("hr.group_hr_user"),
                user.hasGroup("hr_attendance.group_hr_attendance_user"),
            ]);
            if (status(this) === "destroyed") {
                return;
            }
            this.isHrUser = isHrUser;
            this.hasAttendanceRight = hasAttendanceRight;
            if (isHrUser && hasAttendanceRight) {
                const hasDemoData = await this.orm.call("hr.attendance", "has_demo_data", []);
                if (status(this) !== "destroyed") {
                    this.state.hasDemoData = hasDemoData;
                }
            }
        });
    },
});
