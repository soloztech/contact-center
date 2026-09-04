/** @odoo-module **/

const MAX_SEEN_EVENTS = 256;

function notificationPermission(scope) {
    const notification = scope && scope.Notification;
    return notification && typeof notification.permission === "string"
        ? notification.permission
        : "unsupported";
}

function documentHasFocus(documentScope) {
    return Boolean(
        documentScope &&
            !documentScope.hidden &&
            (typeof documentScope.hasFocus !== "function" || documentScope.hasFocus())
    );
}

function isInboundMessageNotification(payload) {
    return Boolean(
        payload &&
            payload.event_type === "message_created" &&
            payload.direction === "inbound" &&
            Number.isSafeInteger(payload.channel_id) &&
            payload.channel_id > 0 &&
            Number.isSafeInteger(payload.message_id) &&
            payload.message_id > 0
    );
}

/**
 * Keep out-of-focus attention separate from the Contact Center data contract.
 *
 * Notifications deliberately contain no customer name or message body: those
 * values could otherwise leak on a locked screen. The bus remains an invalidation
 * transport and only carries IDs plus the provider-neutral message direction.
 */
export class BrowserAttention {
    constructor({scope = window, documentScope = document, onChange = false} = {}) {
        this.scope = scope;
        this.document = documentScope;
        this.onChange = typeof onChange === "function" ? onChange : false;
        this.baseTitle = (documentScope && documentScope.title) || "Odoo";
        this.unseen = 0;
        this.soundEnabled = false;
        this.audioContext = null;
        this.seenEvents = new Set();
        this.seenOrder = [];
        this.destroyed = false;
        this.onVisibilityChange = this.onVisibilityChange.bind(this);
        this.onFocus = this.onFocus.bind(this);
        if (this.document && typeof this.document.addEventListener === "function") {
            this.document.addEventListener("visibilitychange", this.onVisibilityChange);
        }
        if (this.scope && typeof this.scope.addEventListener === "function") {
            this.scope.addEventListener("focus", this.onFocus);
        }
    }

    snapshot() {
        return {
            available: Boolean(this.document),
            permission: notificationPermission(this.scope),
            sound_enabled: this.soundEnabled,
            unseen: this.unseen,
        };
    }

    setStateListener(listener) {
        this.onChange = typeof listener === "function" ? listener : false;
        this._emitChange();
    }

    _emitChange() {
        if (this.onChange) {
            this.onChange(this.snapshot());
        }
    }

    _remember(key) {
        if (this.seenEvents.has(key)) {
            return false;
        }
        this.seenEvents.add(key);
        this.seenOrder.push(key);
        while (this.seenOrder.length > MAX_SEEN_EVENTS) {
            this.seenEvents.delete(this.seenOrder.shift());
        }
        return true;
    }

    _audioConstructor() {
        return this.scope && (this.scope.AudioContext || this.scope.webkitAudioContext);
    }

    async _enableSound() {
        const AudioContext = this._audioConstructor();
        if (!AudioContext) {
            this.soundEnabled = false;
            return false;
        }
        try {
            if (!this.audioContext) {
                this.audioContext = new AudioContext();
            }
            if (
                this.audioContext.state === "suspended" &&
                typeof this.audioContext.resume === "function"
            ) {
                await this.audioContext.resume();
            }
            this.soundEnabled = this.audioContext.state !== "closed";
        } catch (_error) {
            this.soundEnabled = false;
        }
        return this.soundEnabled;
    }

    async enable() {
        const NotificationApi = this.scope && this.scope.Notification;
        let permissionRequest = Promise.resolve();
        if (
            NotificationApi &&
            NotificationApi.permission === "default" &&
            typeof NotificationApi.requestPermission === "function"
        ) {
            try {
                // Invoke while the click still owns browser user activation.
                permissionRequest = Promise.resolve(
                    NotificationApi.requestPermission()
                );
            } catch (_error) {
                // Title attention and the optional sound still work when the
                // browser declines or cannot present the permission prompt.
            }
        }
        const soundRequest = this._enableSound();
        await Promise.allSettled([permissionRequest, soundRequest]);
        this._emitChange();
        return this.snapshot();
    }

    _playSignal() {
        const context = this.audioContext;
        if (!this.soundEnabled || !context || context.state === "closed") {
            return false;
        }
        try {
            const start = context.currentTime;
            const gain = context.createGain();
            gain.gain.setValueAtTime(0.0001, start);
            gain.gain.exponentialRampToValueAtTime(0.08, start + 0.015);
            gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.24);
            gain.connect(context.destination);
            for (const [offset, frequency] of [
                [0, 740],
                [0.09, 940],
            ]) {
                const oscillator = context.createOscillator();
                oscillator.type = "sine";
                oscillator.frequency.setValueAtTime(frequency, start + offset);
                oscillator.connect(gain);
                oscillator.start(start + offset);
                oscillator.stop(start + offset + 0.14);
            }
            return true;
        } catch (_error) {
            return false;
        }
    }

    _showNotification(channelId) {
        const NotificationApi = this.scope && this.scope.Notification;
        if (!NotificationApi || NotificationApi.permission !== "granted") {
            return false;
        }
        try {
            const notification = new NotificationApi("Nova mensagem no atendimento", {
                body: "Abra a Central de Atendimento para visualizar a conversa.",
                tag: `contact-center:${channelId}`,
                renotify: false,
                silent: true,
            });
            notification.onclick = () => {
                if (this.scope && typeof this.scope.focus === "function") {
                    this.scope.focus();
                }
                if (typeof notification.close === "function") {
                    notification.close();
                }
            };
            return true;
        } catch (_error) {
            return false;
        }
    }

    receive(payload) {
        if (this.destroyed || !isInboundMessageNotification(payload)) {
            return false;
        }
        const key = `${payload.channel_id}:${payload.message_id}`;
        if (!this._remember(key) || documentHasFocus(this.document)) {
            return false;
        }
        if (!this.unseen && this.document && this.document.title) {
            this.baseTitle = this.document.title.replace(/^\(\d+\)\s*/, "") || "Odoo";
        }
        this.unseen += 1;
        if (this.document) {
            this.document.title = `(${this.unseen}) ${this.baseTitle}`;
        }
        this._playSignal();
        this._showNotification(payload.channel_id);
        this._emitChange();
        return true;
    }

    clear() {
        if (!this.unseen) {
            return false;
        }
        this.unseen = 0;
        if (this.document) {
            this.document.title = this.baseTitle;
        }
        this._emitChange();
        return true;
    }

    onVisibilityChange() {
        if (documentHasFocus(this.document)) {
            this.clear();
        }
    }

    onFocus() {
        if (this.document && !this.document.hidden) {
            this.clear();
        }
    }

    destroy() {
        if (this.destroyed) {
            return;
        }
        this.destroyed = true;
        if (this.document && typeof this.document.removeEventListener === "function") {
            this.document.removeEventListener(
                "visibilitychange",
                this.onVisibilityChange
            );
        }
        if (this.scope && typeof this.scope.removeEventListener === "function") {
            this.scope.removeEventListener("focus", this.onFocus);
        }
        if (this.unseen && this.document) {
            this.document.title = this.baseTitle;
        }
        this.unseen = 0;
        if (this.audioContext && typeof this.audioContext.close === "function") {
            this.audioContext.close();
        }
        this.audioContext = null;
        this.onChange = false;
    }
}
