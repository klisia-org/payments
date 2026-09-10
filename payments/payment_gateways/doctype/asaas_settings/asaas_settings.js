// Copyright (c) 2026, Frappe Technologies and contributors
// For license information, please see license.txt

frappe.ui.form.on("Asaas Settings", {
  refresh(frm) {
    frm.add_custom_button(__("Generate Webhook Token"), () => {
      frm.trigger("generate_webhook_token");
    });

    frm.add_custom_button(__("Register Webhook"), () => {
      frm.trigger("register_webhook");
    });
  },

  generate_webhook_token: function (frm) {
    // Asaas requires the authToken to be 32-255 characters long
    const bytes = new Uint8Array(24);
    window.crypto.getRandomValues(bytes);
    const token = Array.from(bytes)
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");

    frm.set_value("webhook_auth_token", token);
    frappe.show_alert({
      message: __("Webhook token generated. Save, then register the webhook."),
      indicator: "green",
    });
  },

  register_webhook: function (frm) {
    if (frm.is_dirty()) {
      frappe.msgprint(
        __("Please save the settings before registering the webhook.")
      );
      return;
    }

    frm
      .call({
        method: "register_webhook",
        doc: frm.doc,
        freeze: true,
        freeze_message: __("Registering webhook in Asaas ..."),
      })
      .then((r) => {
        if (!r.exc && r.message) {
          frappe.show_alert({
            message: __("Asaas will now send payment events to {0}", [
              r.message,
            ]),
            indicator: "green",
          });
        }
      });
  },
});
