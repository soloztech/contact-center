/** @odoo-module **/

import {
    Component,
    onMounted,
    onPatched,
    onWillUnmount,
    useRef,
    useState,
} from "@odoo/owl";

export const DEFERRED_IMAGE_CONCURRENCY = 2;

const IMAGE_LOAD_TIMEOUT_MS = 30000;
const IMAGE_OBSERVER_OPTIONS = Object.freeze({rootMargin: "200px 0px"});

export class BoundedImageQueue {
    constructor(limit = DEFERRED_IMAGE_CONCURRENCY) {
        if (!Number.isSafeInteger(limit) || limit < 1) {
            throw new TypeError("The image queue limit must be a positive integer.");
        }
        this.limit = limit;
        this.activeCount = 0;
        this.pending = [];
        this.sequence = 0;
    }

    schedule(start, {priority = 0} = {}) {
        if (typeof start !== "function") {
            throw new TypeError("An image loader function is required.");
        }
        const task = {
            abort: null,
            active: false,
            cancelled: false,
            finished: false,
            priority: Number.isFinite(priority) ? priority : 0,
            sequence: this.sequence++,
            start,
        };
        this.pending.push(task);
        this.pending.sort(
            (left, right) =>
                right.priority - left.priority || left.sequence - right.sequence
        );
        this._drain();
        return () => this._cancel(task);
    }

    get pendingCount() {
        return this.pending.filter((task) => !task.cancelled && !task.finished).length;
    }

    _cancel(task) {
        if (task.finished || task.cancelled) {
            return false;
        }
        task.cancelled = true;
        if (!task.active) {
            task.finished = true;
            this._drain();
            return true;
        }
        try {
            if (typeof task.abort === "function") {
                task.abort();
            }
        } finally {
            this._finish(task);
        }
        return true;
    }

    _finish(task) {
        if (task.finished) {
            return;
        }
        task.finished = true;
        if (task.active) {
            task.active = false;
            this.activeCount -= 1;
        }
        this._drain();
    }

    _drain() {
        while (this.activeCount < this.limit && this.pending.length) {
            const task = this.pending.shift();
            if (task.cancelled || task.finished) {
                task.finished = true;
                continue;
            }
            task.active = true;
            this.activeCount += 1;
            const finish = () => this._finish(task);
            try {
                const abort = task.start(finish);
                if (!task.finished && typeof abort === "function") {
                    task.abort = abort;
                }
            } catch (_error) {
                finish();
            }
        }
    }
}

const imageQueue = new BoundedImageQueue();

function normalizedSource(value) {
    return typeof value === "string" && value.trim() ? value.trim() : false;
}

function positiveDimension(value) {
    return Number.isSafeInteger(value) && value > 0 ? value : false;
}

export class DeferredImage extends Component {
    setup() {
        this.imageRef = useRef("image");
        this.state = useState({activeSrc: false, status: "waiting"});
        this.observer = null;
        this.cancelLoad = null;
        this.requestedSource = false;
        this.loadSuccess = null;
        this.loadFailure = null;
        this.destroyed = false;
        onMounted(() => this._syncSource());
        onPatched(() => this._syncSource());
        onWillUnmount(() => this._teardown());
    }

    get imageClass() {
        return ["cc-deferred-image", this.props.className || ""]
            .filter(Boolean)
            .join(" ");
    }

    get altText() {
        return typeof this.props.alt === "string" ? this.props.alt : "";
    }

    get ariaHidden() {
        return this.props.ariaHidden ? "true" : false;
    }

    get imageWidth() {
        return positiveDimension(this.props.width);
    }

    get imageHeight() {
        return positiveDimension(this.props.height);
    }

    _syncSource() {
        const source = normalizedSource(this.props.src);
        if (source === this.requestedSource) {
            return;
        }
        this._stopCurrent();
        this.requestedSource = source;
        this.state.activeSrc = false;
        this.state.status = source ? "waiting" : "empty";
        if (source) {
            this._observe(source);
        }
    }

    _observe(source) {
        const image = this.imageRef.el;
        if (!image || typeof window.IntersectionObserver !== "function") {
            this._schedule(source);
            return;
        }
        this.observer = new window.IntersectionObserver((entries) => {
            if (!entries.some((entry) => entry.isIntersecting)) {
                return;
            }
            if (this.observer) {
                this.observer.disconnect();
                this.observer = null;
            }
            this._schedule(source);
        }, IMAGE_OBSERVER_OPTIONS);
        this.observer.observe(image);
    }

    _schedule(source) {
        if (this.destroyed || source !== this.requestedSource || this.cancelLoad) {
            return;
        }
        this.state.status = "queued";
        this.cancelLoad = imageQueue.schedule((finish) => this._start(source, finish), {
            priority: this.props.priority || 0,
        });
    }

    _start(source, finish) {
        if (this.destroyed || source !== this.requestedSource) {
            finish();
            return null;
        }
        let settled = false;
        let timer = null;
        const settle = (status) => {
            if (settled) {
                return;
            }
            settled = true;
            window.clearTimeout(timer);
            this.loadSuccess = null;
            this.loadFailure = null;
            const isCurrent = !this.destroyed && source === this.requestedSource;
            if (isCurrent) {
                if (status === "error") {
                    this._removeSource();
                    this.state.activeSrc = false;
                }
                this.state.status = status;
            }
            finish();
            // Release the bounded queue before notifying the owner.  The callback
            // may immediately replace this component with an error/retry surface.
            if (
                isCurrent &&
                status === "error" &&
                typeof this.props.onLoadError === "function"
            ) {
                this.props.onLoadError(this.props.loadGeneration || 0);
            }
        };
        timer = window.setTimeout(() => settle("error"), IMAGE_LOAD_TIMEOUT_MS);
        this.loadSuccess = () => settle("loaded");
        this.loadFailure = () => settle("error");
        this.state.status = "loading";
        this.state.activeSrc = source;
        return () => {
            if (settled) {
                return;
            }
            settled = true;
            window.clearTimeout(timer);
            this.loadSuccess = null;
            this.loadFailure = null;
            this._removeSource();
            if (!this.destroyed && source === this.requestedSource) {
                this.state.activeSrc = false;
                this.state.status = "waiting";
            }
        };
    }

    _removeSource() {
        if (this.imageRef.el) {
            this.imageRef.el.removeAttribute("src");
        }
    }

    _stopCurrent() {
        if (this.observer) {
            this.observer.disconnect();
            this.observer = null;
        }
        if (this.cancelLoad) {
            this.cancelLoad();
            this.cancelLoad = null;
        }
        this.loadSuccess = null;
        this.loadFailure = null;
    }

    _teardown() {
        this.destroyed = true;
        this._stopCurrent();
    }

    onLoad() {
        if (this.loadSuccess) {
            this.loadSuccess();
        }
    }

    onError() {
        if (this.loadFailure) {
            this.loadFailure();
        }
    }
}

DeferredImage.props = {
    alt: {type: String, optional: true},
    ariaHidden: {type: Boolean, optional: true},
    className: {type: String, optional: true},
    height: {type: Number, optional: true},
    loadGeneration: {type: Number, optional: true},
    onLoadError: {type: Function, optional: true},
    priority: {type: Number, optional: true},
    src: String,
    width: {type: Number, optional: true},
};
DeferredImage.template = "contact_center_ui.DeferredImage";
