/** @odoo-module **/

import {Component} from "@odoo/owl";
import {_lt} from "@web/core/l10n/translation";
import {registry} from "@web/core/registry";
import {standardFieldProps} from "@web/views/fields/standard_field_props";

export class ContactCenterOnboardingQrField extends Component {
    get hasQrCode() {
        return Boolean(this.props.value);
    }
}

ContactCenterOnboardingQrField.template =
    "contact_center_wuzapi.ContactCenterOnboardingQrField";
ContactCenterOnboardingQrField.props = {...standardFieldProps};
ContactCenterOnboardingQrField.displayName = _lt("QR de conexão WuzAPI");
ContactCenterOnboardingQrField.supportedTypes = ["char"];

registry
    .category("fields")
    .add("contact_center_onboarding_qr", ContactCenterOnboardingQrField);
