/** @odoo-module **/

/* global QUnit */

import {
    AttendanceReport,
    EPISODE_MODEL,
    REPORT_MODEL,
    coverageNotes,
    defaultPeriod,
    episodeDomain,
    formatPercentiles,
    formatResponsibleCoverage,
    formatWait,
    reportFilters,
} from "@contact_center_ui/js/attendance_report.esm";
import {getFixture, mount, nextTick} from "@web/../tests/helpers/utils";
import {makeTestEnv} from "@web/../tests/helpers/mock_env";
import {registry} from "@web/core/registry";

const {DateTime} = luxon;

function metric(measured, median, p90) {
    return {measured, median: measured ? median : null, p90: measured ? p90 : null};
}

function row(values) {
    return {
        episodes: 0,
        answered: 0,
        pending: 0,
        closed_unanswered: 0,
        before_history: 0,
        responsible_known: 0,
        responsible_known_ratio: null,
        first_response: metric(0),
        subsequent_response: metric(0),
        transfers: 0,
        reopenings: 0,
        inbound: 0,
        outbound: 0,
        unplaced_replies: 0,
        archived_inbound: 0,
        ...values,
    };
}

function reportPayload(overrides = {}) {
    return {
        schema_version: 1,
        filters: {group_by: "account"},
        period: {start: "2026-09-01 03:00:00", end: "2026-10-01 03:00:00"},
        time_basis: "elapsed",
        history_since: "2026-09-10 12:00:00",
        partial_history: true,
        groups: [
            row({
                key: 7,
                label: "Vendas",
                episodes: 3,
                answered: 2,
                pending: 1,
                responsible_known: 2,
                responsible_known_ratio: 2 / 3,
                first_response: metric(2, 180, 456),
                subsequent_response: metric(1, 60, 84),
                transfers: 1,
                reopenings: 2,
                inbound: 5,
                outbound: 4,
            }),
            row({key: 8, label: "Suporte"}),
        ],
        total: row({
            key: null,
            label: "Total",
            episodes: 3,
            answered: 2,
            pending: 1,
            before_history: 1,
            responsible_known: 2,
            responsible_known_ratio: 2 / 3,
            first_response: metric(2, 180, 456),
            subsequent_response: metric(1, 60, 84),
            unplaced_replies: 1,
            archived_inbound: 2,
        }),
        options: {
            accounts: [
                {id: 7, name: "Vendas"},
                {id: 8, name: "Suporte"},
            ],
            platforms: ["whatsapp"],
            responsibles: [{id: 3, name: "Ana"}],
        },
        ...overrides,
    };
}

async function settle() {
    for (let attempt = 0; attempt < 10; attempt += 1) {
        await Promise.resolve();
    }
    await nextTick();
}

async function mountReport(responses) {
    const calls = [];
    const actions = [];
    const services = registry.category("services");
    services.add(
        "orm",
        {
            start: () => ({
                call: (model, method, args) => {
                    calls.push({model, method, args});
                    const response = responses.length
                        ? responses.shift()
                        : reportPayload();
                    return response instanceof Error
                        ? Promise.reject(response)
                        : Promise.resolve(response);
                },
            }),
        },
        {force: true}
    );
    services.add(
        "action",
        {start: () => ({doAction: (action) => actions.push(action)})},
        {force: true}
    );
    const env = await makeTestEnv();
    const target = getFixture();
    const component = await mount(AttendanceReport, target, {env});
    await settle();
    return {calls, actions, target, component};
}

QUnit.module("contact_center_ui > attendance report", () => {
    QUnit.test("an absent measurement is 'sem dados', never zero", (assert) => {
        assert.strictEqual(formatWait(null), "sem dados");
        assert.strictEqual(formatWait(undefined), "sem dados");
        assert.strictEqual(formatWait(0), "0 s");
        assert.strictEqual(formatWait(59), "59 s");
        assert.strictEqual(formatWait(60), "1 min");
        assert.strictEqual(formatWait(200), "3 min 20 s");
        assert.strictEqual(formatWait(456), "7 min 36 s");
        assert.strictEqual(formatWait(3900), "1 h 05 min");
        assert.strictEqual(formatWait(7200), "2 h");
        assert.strictEqual(formatPercentiles(metric(0)), "sem dados");
        assert.strictEqual(
            formatPercentiles(metric(4, 180, 456)),
            "3 min · p90 7 min 36 s"
        );
        assert.strictEqual(
            formatResponsibleCoverage(row({episodes: 0})),
            "Responsável conhecido: sem dados"
        );
        assert.strictEqual(
            formatResponsibleCoverage(
                row({episodes: 3, responsible_known_ratio: 2 / 3})
            ),
            "Responsável conhecido em 67% dos episódios"
        );
    });

    QUnit.test("filters map to the server contract and the episode list", (assert) => {
        const period = defaultPeriod(DateTime.fromISO("2026-09-28"));
        assert.deepEqual(period, {date_from: "2026-08-30", date_to: "2026-09-28"});
        const filters = {
            ...period,
            account_id: "7",
            responsible_id: "",
            platform: "whatsapp",
            group_by: "responsible",
        };
        assert.deepEqual(reportFilters(filters), {
            date_from: "2026-08-30",
            date_to: "2026-09-28",
            account_ids: [7],
            responsible_ids: [],
            platforms: ["whatsapp"],
            group_by: "responsible",
        });
        // The episode list follows the filters the server echoed, not the form.
        assert.deepEqual(
            episodeDomain(
                {account_ids: [7], responsible_ids: [3], platforms: ["whatsapp"]},
                {start: "2026-08-30 03:00:00", end: "x"}
            ),
            [
                ["start_at", ">=", "2026-08-30 03:00:00"],
                ["start_at", "<", "x"],
                ["account_id", "in", [7]],
                ["responsible_ref", "in", [3]],
                ["platform", "in", ["whatsapp"]],
            ]
        );
    });

    QUnit.test("coverage notes state the basis of every number", (assert) => {
        const notes = coverageNotes(reportPayload());
        assert.strictEqual(notes[0], "Tempo corrido (horário útil não configurado).");
        assert.ok(notes[1].includes("cobertura parcial"));
        assert.strictEqual(notes[2], "Responsável conhecido em 67% dos episódios.");
        assert.ok(notes[3].includes("entrega confirmada"));
        assert.ok(notes.some((note) => note.includes("anterior ao histórico: 1")));
        assert.ok(notes.some((note) => note.includes("não posicionadas: 1")));
        assert.ok(notes.some((note) => note.includes("conversa arquivada")));
        const empty = coverageNotes(
            reportPayload({history_since: false, total: row({key: null})})
        );
        assert.ok(empty[1].includes("completo"));
        assert.strictEqual(empty[2], "Responsável conhecido: sem dados.");
        assert.strictEqual(empty.length, 4);
    });

    QUnit.test(
        "the action renders server percentiles and reloads on a filter change",
        async (assert) => {
            const {calls, actions, target, component} = await mountReport([
                reportPayload(),
                reportPayload({groups: []}),
            ]);
            assert.strictEqual(calls.length, 1);
            assert.strictEqual(calls[0].model, REPORT_MODEL);
            assert.strictEqual(calls[0].method, "get_report");
            assert.strictEqual(calls[0].args[0].group_by, "account");
            const rows = target.querySelectorAll("tbody tr");
            assert.strictEqual(rows.length, 2);
            assert.strictEqual(
                rows[0]
                    .querySelector(".cc-attendance-report__first span")
                    .textContent.trim(),
                "3 min · p90 7 min 36 s"
            );
            assert.strictEqual(
                rows[1]
                    .querySelector(".cc-attendance-report__first span")
                    .textContent.trim(),
                "sem dados"
            );
            assert.strictEqual(
                rows[1]
                    .querySelector(".cc-attendance-report__coverage")
                    .textContent.trim(),
                "Responsável conhecido: sem dados"
            );
            assert.strictEqual(
                rows[0]
                    .querySelector(".cc-attendance-report__coverage")
                    .textContent.trim(),
                "Responsável conhecido em 67% dos episódios"
            );
            assert.ok(
                target
                    .querySelector(".cc-attendance-report__notes")
                    .textContent.includes("horário útil não configurado")
            );
            assert.strictEqual(
                target
                    .querySelector(
                        ".cc-attendance-report__total .cc-attendance-report__episodes-count"
                    )
                    .textContent.trim(),
                "3"
            );

            const select = target.querySelector(".cc-attendance-report__group-by");
            select.value = "responsible";
            select.dispatchEvent(new Event("change"));
            await settle();
            assert.strictEqual(calls.length, 2);
            assert.strictEqual(calls[1].args[0].group_by, "responsible");
            assert.ok(
                target
                    .querySelector(".cc-attendance-report__empty")
                    .textContent.includes("sem dados")
            );

            target.querySelector(".cc-attendance-report__episodes").click();
            assert.strictEqual(actions.length, 1);
            assert.strictEqual(actions[0].res_model, EPISODE_MODEL);
            assert.deepEqual(actions[0].domain.slice(0, 2), [
                ["start_at", ">=", "2026-09-01 03:00:00"],
                ["start_at", "<", "2026-10-01 03:00:00"],
            ]);
            component.__owl__.app.destroy();
        }
    );

    QUnit.test(
        "former responsibles and archived inboxes can be selected",
        async (assert) => {
            const options = {
                accounts: [
                    {id: 7, name: "Vendas"},
                    {id: 9, name: "Antiga (arquivada)"},
                ],
                platforms: ["whatsapp"],
                responsibles: [
                    {id: 3, name: "Ana"},
                    {id: 41, name: "Usuário removido (#41)"},
                ],
            };
            const {calls, actions, target, component} = await mountReport([
                reportPayload({options}),
                reportPayload({options}),
                reportPayload({
                    options,
                    filters: {
                        account_ids: [9],
                        responsible_ids: [41],
                        platforms: [],
                        group_by: "account",
                    },
                }),
            ]);
            const responsible = target.querySelector(
                ".cc-attendance-report__responsible"
            );
            assert.deepEqual(
                [...responsible.options].map((option) => option.textContent),
                ["Todos", "Ana", "Usuário removido (#41)"]
            );
            responsible.value = "41";
            responsible.dispatchEvent(new Event("change"));
            await settle();
            assert.deepEqual(calls[1].args[0].responsible_ids, [41]);
            const account = target.querySelector(".cc-attendance-report__account");
            assert.strictEqual(
                account.options[account.options.length - 1].textContent,
                "Antiga (arquivada)"
            );
            account.value = "9";
            account.dispatchEvent(new Event("change"));
            await settle();
            assert.deepEqual(calls[2].args[0].account_ids, [9]);
            assert.deepEqual(calls[2].args[0].responsible_ids, [41]);
            assert.strictEqual(responsible.value, "41");
            target.querySelector(".cc-attendance-report__episodes").click();
            assert.deepEqual(actions[0].domain.slice(2), [
                ["account_id", "in", [9]],
                ["responsible_ref", "in", [41]],
            ]);
            component.__owl__.app.destroy();
        }
    );

    QUnit.test("a server error is shown instead of stale numbers", async (assert) => {
        const error = new Error("rejected");
        error.data = {message: "Escolha um período de até um ano."};
        const {target, component} = await mountReport([error]);
        assert.strictEqual(
            target.querySelector(".cc-attendance-report__error").textContent,
            "Escolha um período de até um ano."
        );
        assert.notOk(target.querySelector(".cc-attendance-report__table"));
        component.__owl__.app.destroy();
    });

    QUnit.test(
        "a rejected reload removes the numbers and the episode shortcut",
        async (assert) => {
            const error = new Error("rejected");
            error.data = {message: "Escolha um período de até um ano."};
            const {calls, actions, target, component} = await mountReport([
                reportPayload({
                    filters: {
                        account_ids: [8],
                        responsible_ids: [],
                        platforms: [],
                        group_by: "account",
                    },
                }),
                error,
            ]);
            const button = target.querySelector(".cc-attendance-report__episodes");
            assert.notOk(button.disabled);
            // The shortcut follows the filters the server answered with.
            button.click();
            assert.deepEqual(actions[0].domain[2], ["account_id", "in", [8]]);

            const dateFrom = target.querySelector(".cc-attendance-report__date-from");
            dateFrom.value = "2020-01-01";
            dateFrom.dispatchEvent(new Event("change"));
            await settle();
            assert.strictEqual(calls.length, 2);
            assert.strictEqual(
                target.querySelector(".cc-attendance-report__error").textContent,
                "Escolha um período de até um ano."
            );
            assert.notOk(target.querySelector(".cc-attendance-report__table"));
            assert.ok(button.disabled);
            button.click();
            assert.strictEqual(actions.length, 1);
            // The known filter choices stay available to correct the period.
            assert.strictEqual(
                target.querySelectorAll(".cc-attendance-report__account option").length,
                3
            );
            component.__owl__.app.destroy();
        }
    );

    QUnit.test("the episode shortcut waits for a reload in flight", async (assert) => {
        let release = null;
        const pending = new Promise((resolve) => {
            release = resolve;
        });
        const {actions, target, component} = await mountReport([
            reportPayload(),
            pending,
        ]);
        const select = target.querySelector(".cc-attendance-report__group-by");
        select.value = "platform";
        select.dispatchEvent(new Event("change"));
        await settle();
        const button = target.querySelector(".cc-attendance-report__episodes");
        assert.ok(button.disabled);
        button.click();
        assert.strictEqual(actions.length, 0);
        release(reportPayload());
        await settle();
        assert.notOk(button.disabled);
        component.__owl__.app.destroy();
    });
});
