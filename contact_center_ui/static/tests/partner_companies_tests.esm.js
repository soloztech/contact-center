/** @odoo-module **/
/* global QUnit */

import {secondaryCompaniesForIdentity} from "@contact_center_ui/js/contact_center_model.esm";
import {ContactCenterStore} from "@contact_center_ui/js/contact_center_store.esm";
import {ContactPanel} from "@contact_center_ui/js/contact_panel.esm";

QUnit.module("contact_center_ui > partner companies", () => {
    QUnit.test(
        "secondary companies have valid distinct keys and never repeat the primary",
        (assert) => {
            const company = {id: 20, name: "Principal"};
            const secondary = {id: 30, name: "Secundária"};
            const items = secondaryCompaniesForIdentity({
                partner: {
                    company,
                    secondary_companies: [
                        company,
                        null,
                        {},
                        secondary,
                        secondary,
                        {id: 40},
                    ],
                },
            });
            assert.deepEqual(
                items.map((item) => item.id),
                [30]
            );
            assert.deepEqual(secondaryCompaniesForIdentity(false), []);
        }
    );

    QUnit.test(
        "normal unlink is shown after companies while correction preserves global relations",
        (assert) => {
            const getter = Object.getOwnPropertyDescriptor(
                ContactPanel.prototype,
                "canUnlinkIdentity"
            ).get;
            assert.notOk(
                getter.call({
                    partner: {id: 10},
                    isLinkedCompany: false,
                    hasCompanyRelationships: true,
                })
            );
            assert.ok(
                getter.call({
                    partner: {id: 10},
                    isLinkedCompany: false,
                    hasCompanyRelationships: false,
                })
            );
            let options = null;
            let unlinked = false;
            ContactPanel.prototype.correctLinkedContact.call({
                dialog: {
                    add: (_component, values) => {
                        options = values;
                    },
                },
                store: {
                    unlinkPartner: () => {
                        unlinked = true;
                    },
                },
            });
            assert.notOk(
                unlinked,
                "changing a wrong person requires the scoped correction confirmation"
            );
            assert.ok(options.body.includes("preservados"));
            options.confirm();
            assert.ok(unlinked);
        }
    );

    function companyStore() {
        const store = new ContactCenterStore({
            orm: {},
            busService: new EventTarget(),
            notification: false,
        });
        store.state.bootstrap = {
            capabilities: {link_company: true, create_company: true},
        };
        store.state.selectedChannelId = 10;
        store.state.conversations = [
            {
                channel_id: 10,
                state: "open",
                identity: {
                    id: 1,
                    link_kind: "person",
                    partner: {
                        id: 11,
                        name: "Pessoa",
                        is_company: false,
                        company: {id: 20, name: "Principal"},
                        company_linking_allowed: false,
                        secondary_company_linking_allowed: true,
                        company_management_allowed: true,
                        secondary_companies: [{id: 30, name: "Secundária"}],
                    },
                },
            },
        ];
        store.applyIdentityForPartner = (_channel, _person, identity) => {
            store.state.conversations[0].identity = identity;
            return true;
        };
        return store;
    }

    QUnit.test(
        "secondary saves use an explicit relation kind and leave the primary in the projection",
        async (assert) => {
            const store = companyStore();
            try {
                assert.ok(store.openCompanyLinker());
                assert.strictEqual(store.state.companyLinker.relationKind, "secondary");
                store.call = async (method, args) => {
                    assert.strictEqual(method, "link_partner_company");
                    assert.deepEqual(args, [10, 11, 30, "secondary"]);
                    return {
                        schema_version: 1,
                        company: {id: 30, name: "Secundária"},
                        identity: store.selectedConversation.identity,
                    };
                };
                assert.ok(await store.linkPartnerCompany(30));
                assert.strictEqual(
                    store.selectedConversation.identity.partner.company.id,
                    20
                );
            } finally {
                store.destroy();
            }
        }
    );

    QUnit.test(
        "removing a secondary relationship keeps the person and primary",
        async (assert) => {
            const store = companyStore();
            try {
                store.call = async (method, args) => {
                    assert.strictEqual(method, "unlink_partner_company");
                    assert.deepEqual(args, [10, 11, 30, "secondary"]);
                    const identity = {
                        ...store.selectedConversation.identity,
                        partner: {
                            ...store.selectedConversation.identity.partner,
                            secondary_companies: [],
                        },
                    };
                    return {schema_version: 1, identity};
                };
                assert.ok(await store.unlinkPartnerCompany(30, "secondary"));
                assert.strictEqual(store.selectedConversation.identity.partner.id, 11);
                assert.strictEqual(
                    store.selectedConversation.identity.partner.company.id,
                    20
                );
                assert.deepEqual(
                    store.selectedConversation.identity.partner.secondary_companies,
                    []
                );
                assert.notOk(
                    await store.unlinkPartnerCompany(30, "secondary"),
                    "stale removal is not submitted"
                );
            } finally {
                store.destroy();
            }
        }
    );
});
