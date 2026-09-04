/** @odoo-module **/

export const VOICE_RECORDING_MAX_SECONDS = 15 * 60;

const VOICE_RECORDING_FORMATS = Object.freeze([
    Object.freeze({
        mimeType: "audio/ogg;codecs=opus",
        fileMimeType: "audio/ogg",
        extension: "ogg",
    }),
    Object.freeze({mimeType: "audio/mp4", fileMimeType: "audio/mp4", extension: "m4a"}),
]);
const NOOP = () => true;

function stopStream(stream) {
    if (!stream || typeof stream.getTracks !== "function") {
        return;
    }
    for (const track of stream.getTracks()) {
        if (track && typeof track.stop === "function") {
            track.stop();
        }
    }
}

function recordingFormats(policy) {
    if (!policy || !Array.isArray(policy.recording_mimetypes)) {
        return [];
    }
    const requested = new Set(
        policy.recording_mimetypes
            .filter((value) => typeof value === "string")
            .map((value) => value.trim().toLocaleLowerCase())
    );
    const voiceNotes = new Set(
        (Array.isArray(policy.voice_note_mimetypes) ? policy.voice_note_mimetypes : [])
            .filter((value) => typeof value === "string")
            .map((value) => value.trim().toLocaleLowerCase())
    );
    return VOICE_RECORDING_FORMATS.filter((format) =>
        requested.has(format.mimeType)
    ).map((format) => ({
        ...format,
        isVoiceNote: voiceNotes.has(format.mimeType),
    }));
}

export function selectVoiceRecordingFormat(MediaRecorderClass, policy) {
    if (typeof MediaRecorderClass !== "function") {
        return false;
    }
    if (typeof MediaRecorderClass.isTypeSupported !== "function") {
        return false;
    }
    return (
        recordingFormats(policy).find((format) =>
            MediaRecorderClass.isTypeSupported(format.mimeType)
        ) || false
    );
}

export function voiceRecorderAvailable(scope = window, policy = false) {
    const navigatorValue = scope && scope.navigator;
    return Boolean(
        scope &&
            scope.isSecureContext !== false &&
            typeof scope.MediaRecorder === "function" &&
            navigatorValue &&
            navigatorValue.mediaDevices &&
            typeof navigatorValue.mediaDevices.getUserMedia === "function" &&
            selectVoiceRecordingFormat(scope.MediaRecorder, policy)
    );
}

export function voiceRecordingDescriptor(mimetype, policy) {
    const normalized =
        typeof mimetype === "string"
            ? mimetype.split(";", 1)[0].trim().toLocaleLowerCase()
            : "";
    const format = recordingFormats(policy).find(
        (candidate) => candidate.fileMimeType === normalized
    );
    if (!format) {
        return false;
    }
    return {
        mimeType: normalized,
        extension: format.extension,
        isVoiceNote: format.isVoiceNote,
    };
}

export function formatVoiceRecordingDuration(value) {
    const seconds = Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0;
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

export function voiceRecordingErrorMessage(error) {
    const name = error && error.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
        return "Permita o acesso ao microfone para gravar áudio.";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError") {
        return "Nenhum microfone foi encontrado neste dispositivo.";
    }
    if (name === "NotReadableError" || name === "TrackStartError") {
        return "O microfone está ocupado ou não pôde ser iniciado.";
    }
    return "Não foi possível gravar o áudio.";
}

function createMediaRecorder(MediaRecorderClass, stream, format) {
    if (!format) {
        return false;
    }
    const preferredOptions = {
        audioBitsPerSecond: 64000,
        mimeType: format.mimeType,
    };
    for (const options of [preferredOptions, {mimeType: format.mimeType}]) {
        try {
            return new MediaRecorderClass(stream, options);
        } catch (_error) {
            // Some engines advertise MediaRecorder but reject optional settings.
        }
    }
    return false;
}

export class VoiceRecorderSession {
    constructor({
        scope = window,
        maxSeconds = VOICE_RECORDING_MAX_SECONDS,
        now = () => Date.now(),
        onPhase = NOOP,
        onTick = NOOP,
        onComplete = NOOP,
        onError = NOOP,
    } = {}) {
        this.scope = scope;
        this.maxSeconds = maxSeconds;
        this.now = now;
        this.onPhase = onPhase;
        this.onTick = onTick;
        this.onComplete = onComplete;
        this.onError = onError;
        this.generation = 0;
        this.phase = "idle";
        this.recorder = null;
        this.stream = null;
        this.chunks = [];
        this.timer = null;
        this.stopFallbackTimer = null;
        this.startedAt = 0;
        this.elapsedMs = 0;
        this.recordingMimeType = "";
        this.recordingPolicy = false;
        this.activeMaxSeconds = maxSeconds;
    }

    _setPhase(phase) {
        this.phase = phase;
        this.onPhase(phase);
    }

    _clearTimer() {
        if (this.timer !== null) {
            this.scope.clearInterval(this.timer);
            this.timer = null;
        }
    }

    _clearStopFallback() {
        if (
            this.stopFallbackTimer !== null &&
            typeof this.scope.clearTimeout === "function"
        ) {
            this.scope.clearTimeout(this.stopFallbackTimer);
        }
        this.stopFallbackTimer = null;
    }

    _releaseStream() {
        stopStream(this.stream);
        this.stream = null;
    }

    _tick(generation, autoStop = true) {
        if (generation !== this.generation || this.phase !== "recording") {
            return;
        }
        this.elapsedMs = Math.min(
            this.activeMaxSeconds * 1000,
            Math.max(0, this.now() - this.startedAt)
        );
        this.onTick(this.elapsedMs);
        if (autoStop && this.elapsedMs >= this.activeMaxSeconds * 1000) {
            this.stop();
        }
    }

    _fail(error) {
        this.cancel();
        this.onError(voiceRecordingErrorMessage(error));
    }

    async start(policy = false) {
        if (!voiceRecorderAvailable(this.scope, policy)) {
            this.onError("A gravação de áudio não está disponível neste navegador.");
            return false;
        }
        this.cancel();
        const generation = ++this.generation;
        this._setPhase("requesting");
        try {
            const stream = await this.scope.navigator.mediaDevices.getUserMedia({
                audio: {
                    autoGainControl: true,
                    echoCancellation: true,
                    noiseSuppression: true,
                },
            });
            if (generation !== this.generation) {
                stopStream(stream);
                return false;
            }
            const format = selectVoiceRecordingFormat(this.scope.MediaRecorder, policy);
            const recorder = createMediaRecorder(
                this.scope.MediaRecorder,
                stream,
                format
            );
            if (!recorder) {
                stopStream(stream);
                throw new Error("media_recorder_unavailable");
            }
            this.stream = stream;
            this.recorder = recorder;
            this.chunks = [];
            this.recordingPolicy = policy;
            const providerMaxSeconds = policy.max_duration_seconds;
            this.activeMaxSeconds =
                Number.isSafeInteger(providerMaxSeconds) && providerMaxSeconds > 0
                    ? Math.min(this.maxSeconds, providerMaxSeconds)
                    : this.maxSeconds;
            this.recordingMimeType = recorder.mimeType || format.mimeType;
            recorder.ondataavailable = (event) => {
                if (
                    generation === this.generation &&
                    event.data &&
                    event.data.size > 0
                ) {
                    this.chunks.push(event.data);
                }
            };
            recorder.onerror = (event) => {
                if (generation === this.generation) {
                    this._fail(event.error || event);
                }
            };
            recorder.onstop = () => this._complete(generation);
            recorder.start(1000);
            this.startedAt = this.now();
            this.elapsedMs = 0;
            this.onTick(0);
            this._setPhase("recording");
            this.timer = this.scope.setInterval(() => this._tick(generation), 250);
            return true;
        } catch (error) {
            if (generation === this.generation) {
                this._fail(error);
            }
            return false;
        }
    }

    stop() {
        if (
            this.phase !== "recording" ||
            !this.recorder ||
            this.recorder.state === "inactive"
        ) {
            return false;
        }
        this._tick(this.generation, false);
        this._setPhase("processing");
        this._clearTimer();
        try {
            const generation = this.generation;
            if (typeof this.scope.setTimeout === "function") {
                this.stopFallbackTimer = this.scope.setTimeout(() => {
                    this.stopFallbackTimer = null;
                    if (generation === this.generation && this.phase === "processing") {
                        this._fail(new Error("recorder_stop_timeout"));
                    }
                }, 3000);
            }
            this.recorder.stop();
            return true;
        } catch (error) {
            this._fail(error);
            return false;
        }
    }

    _complete(generation) {
        if (generation !== this.generation) {
            return;
        }
        const chunks = this.chunks.slice();
        const elapsedMs = this.elapsedMs || Math.max(0, this.now() - this.startedAt);
        const descriptor = voiceRecordingDescriptor(
            this.recordingMimeType,
            this.recordingPolicy
        );
        this._clearTimer();
        this._clearStopFallback();
        this._releaseStream();
        this.recorder = null;
        this.chunks = [];
        this.recordingPolicy = false;
        if (!descriptor || !chunks.length) {
            this._fail(new Error("empty_or_unsupported_recording"));
            return;
        }
        const blob = new this.scope.Blob(chunks, {type: descriptor.mimeType});
        if (!blob.size || typeof this.scope.File !== "function") {
            this._fail(new Error("empty_or_unsupported_recording"));
            return;
        }
        const durationSeconds = Math.max(1, Math.ceil(elapsedMs / 1000));
        const file = new this.scope.File(
            [blob],
            `gravacao-de-audio-${this.now()}.${descriptor.extension}`,
            {type: descriptor.mimeType, lastModified: this.now()}
        );
        this._setPhase("idle");
        this.onComplete({
            file,
            durationSeconds,
            isVoiceNote: descriptor.isVoiceNote,
        });
    }

    cancel() {
        this.generation += 1;
        const recorder = this.recorder;
        this.recorder = null;
        this._clearTimer();
        this._clearStopFallback();
        this._releaseStream();
        this.chunks = [];
        this.elapsedMs = 0;
        this.recordingPolicy = false;
        this.activeMaxSeconds = this.maxSeconds;
        this.onTick(0);
        this._setPhase("idle");
        if (recorder && recorder.state !== "inactive") {
            try {
                recorder.stop();
            } catch (_error) {
                // Tracks are already stopped and the stale callback is ignored.
            }
        }
    }

    destroy() {
        this.cancel();
    }
}
