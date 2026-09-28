/** @odoo-module **/

import {Component, onWillStart, useState} from "@odoo/owl";
import {deserializeDateTime, formatDateTime} from "@web/core/l10n/dates";
import {_t} from "@web/core/l10n/translation";
import {registry} from "@web/core/registry";
import {useService} from "@web/core/utils/hooks";

const {DateTime} = luxon;

export const REPORT_MODEL = "contact.center.attendance.report";
export const EPISODE_MODEL = "contact.center.attendance.episode";
export const DEFAULT_PERIOD_DAYS = 30;
export const GROUP_BY_OPTIONS = [
    {value: "account", label: "Caixa"},
    {value: "responsible", label: "Responsável no início"},
    {value: "platform", label: "Canal"},
    {value: "none", label: "Sem agrupamento"},
];

/**
 * A missing measurement is never shown as zero.
 *
 * @returns {String}
 */
export function noData() {
    return _t("sem dados");
}

/**
 * Elapsed seconds as text; 0 s is a real measurement, empty is "sem dados".
 *
 * @param {Number|null|undefined} seconds
 * @returns {String}
 */
export function formatWait(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) {
        return noData();
    }
    const total = Math.round(seconds);
    if (total < 60) {
        return `${total} s`;
    }
    const minutes = Math.floor(total / 60);
    const remainder = total % 60;
    if (minutes < 60) {
        return remainder ? `${minutes} min ${remainder} s` : `${minutes} min`;
    }
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? `${hours} h ${String(rest).padStart(2, "0")} min` : `${hours} h`;
}

/**
 * "Responsável conhecido em X% dos episódios", or "sem dados" without episodes.
 *
 * @param {Object} row
 * @returns {String}
 */
export function formatResponsibleCoverage(row) {
    if (!row.episodes || row.responsible_known_ratio === null) {
        return _t("Responsável conhecido: sem dados");
    }
    const percent = Math.round(row.responsible_known_ratio * 100);
    return _t("Responsável conhecido em %s% dos episódios").replace("%s", percent);
}

/**
 * @param {Object} metric {median, p90, measured}
 * @returns {String}
 */
export function formatPercentiles(metric) {
    if (!metric || !metric.measured) {
        return noData();
    }
    return `${formatWait(metric.median)} · p90 ${formatWait(metric.p90)}`;
}

/**
 * The last DEFAULT_PERIOD_DAYS whole days, ending today.
 *
 * @param {DateTime} today
 * @returns {Object}
 */
export function defaultPeriod(today) {
    return {
        date_from: today.minus({days: DEFAULT_PERIOD_DAYS - 1}).toISODate(),
        date_to: today.toISODate(),
    };
}

function idList(value) {
    const id = parseInt(value, 10);
    return id > 0 ? [id] : [];
}

/**
 * Server filters from the single-choice selectors of the form.
 *
 * @param {Object} filters
 * @returns {Object}
 */
export function reportFilters(filters) {
    return {
        date_from: filters.date_from,
        date_to: filters.date_to,
        account_ids: idList(filters.account_id),
        responsible_ids: idList(filters.responsible_id),
        platforms: filters.platform ? [filters.platform] : [],
        group_by: filters.group_by,
    };
}

/**
 * Episode list domain of a displayed report: the filters and the UTC period the
 * server answered with, never the form values that may have changed since.
 *
 * @param {Object} filters server-echoed report filters
 * @param {Object} period {start, end} in UTC, as answered by the server
 * @returns {Array}
 */
export function episodeDomain(filters, period) {
    const values = {
        account_ids: filters.account_ids || [],
        responsible_ids: filters.responsible_ids || [],
        platforms: filters.platforms || [],
    };
    const domain = [
        ["start_at", ">=", period.start],
        ["start_at", "<", period.end],
    ];
    if (values.account_ids.length) {
        domain.push(["account_id", "in", values.account_ids]);
    }
    if (values.responsible_ids.length) {
        // The historic key, as the report: a deleted responsible keeps it.
        domain.push(["responsible_ref", "in", values.responsible_ids]);
    }
    if (values.platforms.length) {
        domain.push(["platform", "in", values.platforms]);
    }
    return domain;
}

/**
 * Coverage lines shown under the table. Every number states its basis.
 *
 * @param {Object} report
 * @returns {String[]}
 */
export function coverageNotes(report) {
    const total = report.total;
    const notes = [_t("Tempo corrido (horário útil não configurado).")];
    if (report.history_since) {
        const since = formatDateTime(deserializeDateTime(report.history_since));
        notes.push(
            report.partial_history
                ? _t(
                      "Histórico de atendimento desde %s: o período começa antes, cobertura parcial."
                  ).replace("%s", since)
                : _t("Histórico de atendimento desde %s.").replace("%s", since)
        );
    } else {
        notes.push(_t("Histórico de atendimento completo desde a instalação."));
    }
    notes.push(formatResponsibleCoverage(total) + ".");
    notes.push(
        _t(
            "Saídas: mensagens de agente e automação com entrega confirmada e respostas pelo celular."
        )
    );
    if (total.before_history) {
        notes.push(
            _t(
                "Episódios em ciclo anterior ao histórico: %s (fora das medianas de 1ª resposta)."
            ).replace("%s", total.before_history)
        );
    }
    if (total.unplaced_replies) {
        notes.push(
            _t("Respostas pelo celular não posicionadas: %s.").replace(
                "%s",
                total.unplaced_replies
            )
        );
    }
    if (total.archived_inbound) {
        notes.push(
            _t("Entradas em conversa arquivada (fora do atendimento): %s.").replace(
                "%s",
                total.archived_inbound
            )
        );
    }
    return notes;
}

export class AttendanceReport extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.groupByOptions = GROUP_BY_OPTIONS;
        this.state = useState({
            filters: {
                ...defaultPeriod(DateTime.local()),
                account_id: "",
                responsible_id: "",
                platform: "",
                group_by: "account",
            },
            loading: false,
            error: false,
            report: null,
            options: {accounts: [], platforms: [], responsibles: []},
        });
        this.requestSequence = 0;
        onWillStart(() => this.load());
    }

    get report() {
        return this.state.report;
    }

    get options() {
        return this.state.options;
    }

    get canOpenEpisodes() {
        return Boolean(this.report) && !this.state.loading;
    }

    get rows() {
        if (!this.report) {
            return [];
        }
        return this.report.groups;
    }

    get notes() {
        return this.report ? coverageNotes(this.report) : [];
    }

    async load() {
        const sequence = ++this.requestSequence;
        this.state.loading = true;
        this.state.error = false;
        try {
            const report = await this.orm.call(REPORT_MODEL, "get_report", [
                reportFilters(this.state.filters),
            ]);
            if (sequence === this.requestSequence) {
                this.state.report = report;
                this.state.options = report.options || this.state.options;
            }
        } catch (error) {
            if (sequence === this.requestSequence) {
                // Numbers of another request must never stay on screen.
                this.state.report = null;
                this.state.error =
                    (error && error.data && error.data.message) ||
                    _t("Não foi possível carregar o relatório.");
            }
        } finally {
            if (sequence === this.requestSequence) {
                this.state.loading = false;
            }
        }
    }

    onFilterChange(name, event) {
        this.state.filters[name] = event.target.value;
        return this.load();
    }

    isSelected(value, current) {
        return String(value) === String(current);
    }

    formatWait(seconds) {
        return formatWait(seconds);
    }

    formatPercentiles(metric) {
        return formatPercentiles(metric);
    }

    formatCoverage(row) {
        return formatResponsibleCoverage(row);
    }

    openEpisodes() {
        if (!this.canOpenEpisodes) {
            return false;
        }
        return this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("Episódios de atendimento"),
            res_model: EPISODE_MODEL,
            views: [
                [false, "list"],
                [false, "pivot"],
            ],
            domain: episodeDomain(this.report.filters, this.report.period),
            target: "current",
        });
    }
}

AttendanceReport.template = "contact_center_ui.AttendanceReport";

registry
    .category("actions")
    .add("contact_center_ui.attendance_report", AttendanceReport);
