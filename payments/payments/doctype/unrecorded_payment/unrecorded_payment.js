// Copyright (c) 2026, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Unrecorded Payment", {
  refresh(frm) {
    if (frm.doc.status === "Resolved" || !frm.perm[0]?.write) {
      return;
    }

    frm.add_custom_button(__("Retry"), () => {
      frm.call("retry_now").then(() => frm.reload_doc());
    });

    frm.add_custom_button(__("Mark Resolved"), () => {
      frappe.confirm(
        __("Mark this payment as recorded? Do this only after recording it by hand."),
        () => frm.call("mark_resolved").then(() => frm.reload_doc())
      );
    });
  },
});
