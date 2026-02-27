import { createStore } from "/js/AlpineStore.js";

export const store = createStore("codexProvider", {
    // Connection state
    connected: false,
    expired: false,
    accountInfo: null,
    hasRefreshToken: false,

    // Proxy state
    proxyRunning: false,
    proxyUrl: "",

    // Device code flow state
    pairing: false,
    userCode: "",
    verificationUri: "",
    sessionId: "",
    pollInterval: 5,
    pollTimer: null,
    pairingError: "",

    // Model selection
    chatModel: "gpt-5.3-codex",
    utilModel: "gpt-5.1-codex-mini",
    browserModel: "gpt-5.1-codex-mini",
    availableModels: [],
    loadingModels: false,

    // UI state
    applying: false,
    refreshing: false,
    showImport: false,
    importJson: "",
    message: null,

    init() {},

    async onOpen() {
        await this.checkConnection();
        await this.checkProxy();
        if (this.connected) {
            await this.fetchModels();
        }
    },

    cleanup() {
        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
            this.pollTimer = null;
        }
    },

    // ── Connection Status ──

    async checkConnection() {
        try {
            const resp = await this._api("oauth", { action: "connection_status" });
            this.connected = resp.connected || false;
            this.expired = resp.expired || false;
            this.accountInfo = resp.account_info || null;
            this.hasRefreshToken = resp.has_refresh_token || false;
        } catch (e) {
            console.error("Connection check failed:", e);
        }
    },

    async checkProxy() {
        try {
            const resp = await this._api("status", { action: "status" });
            this.proxyRunning = resp.proxy_running || false;
            this.proxyUrl = resp.proxy_url || "";
        } catch (e) {
            console.error("Proxy check failed:", e);
        }
    },

    async fetchModels() {
        this.loadingModels = true;
        try {
            const resp = await this._api("configure", { action: "get_models" });
            if (resp.ok && resp.models) {
                this.availableModels = resp.models;
            }
        } catch (e) {
            console.error("Failed to fetch models:", e);
        }
        this.loadingModels = false;
    },

    // ── Device Code Flow ──

    async startDeviceFlow() {
        this.pairing = true;
        this.pairingError = "";
        this.message = null;

        try {
            const resp = await this._api("oauth", { action: "start_device_flow" });
            if (!resp.ok) {
                this.pairingError = resp.error || "Failed to start auth flow";
                this.pairing = false;
                return;
            }
            this.sessionId = resp.session_id;
            this.userCode = resp.user_code;
            this.verificationUri = resp.verification_uri;
            this.pollInterval = resp.interval || 5;

            // Open verification URL in new tab
            window.open(resp.verification_uri, "_blank");

            // Start polling
            this._pollForCompletion();
        } catch (e) {
            this.pairingError = e.message;
            this.pairing = false;
        }
    },

    async _pollForCompletion() {
        if (!this.pairing || !this.sessionId) return;

        try {
            const resp = await this._api("oauth", {
                action: "poll",
                session_id: this.sessionId,
            });
            console.log("[codex] poll:", resp.status, resp);

            if (resp.status === "complete") {
                this.connected = true;
                this.accountInfo = resp.account_info;
                this.pairing = false;
                this.message = { type: "success", text: "Connected!" };
                await this.checkProxy();
                await this.fetchModels();
                return;
            }

            if (resp.status === "error") {
                // Show error but keep pairing UI visible so user can see it
                this.pairingError = resp.error || "Authentication failed";
                return;
            }

            if (resp.status === "expired") {
                this.pairingError = resp.error || "Session expired. Please try again.";
                this.pairing = false;
                return;
            }

            // Still pending — schedule next poll
            this.pollTimer = setTimeout(
                () => this._pollForCompletion(),
                (resp.interval || this.pollInterval) * 1000,
            );
        } catch (e) {
            console.error("[codex] poll error:", e);
            // Network error — retry instead of giving up
            this.pollTimer = setTimeout(
                () => this._pollForCompletion(),
                this.pollInterval * 1000,
            );
        }
    },

    cancelPairing() {
        this.pairing = false;
        this.pairingError = "";
        this.userCode = "";
        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
            this.pollTimer = null;
        }
    },

    // ── Import Tokens ──

    async importTokens() {
        this.message = null;
        try {
            const resp = await this._api("oauth", {
                action: "save_tokens",
                auth_json: this.importJson,
            });
            if (resp.ok) {
                this.connected = true;
                this.accountInfo = resp.account_info;
                this.showImport = false;
                this.importJson = "";
                this.message = { type: "success", text: "Tokens imported!" };
                await this.checkProxy();
            } else {
                this.message = { type: "error", text: resp.error || "Import failed" };
            }
        } catch (e) {
            this.message = { type: "error", text: e.message };
        }
    },

    // ── Actions ──

    async applyToAgent() {
        this.applying = true;
        this.message = null;
        try {
            const resp = await this._api("configure", {
                action: "apply",
                chat_model: this.chatModel,
                util_model: this.utilModel,
                browser_model: this.browserModel,
            });
            if (resp.ok) {
                this.proxyRunning = true;
                this.proxyUrl = resp.proxy_url;
                this.message = { type: "success", text: "Agent configured! Reload the page to apply." };
            } else {
                this.message = { type: "error", text: resp.error || "Failed to apply" };
            }
        } catch (e) {
            this.message = { type: "error", text: e.message };
        }
        this.applying = false;
    },

    async refreshToken() {
        this.refreshing = true;
        try {
            const resp = await this._api("oauth", { action: "refresh" });
            if (resp.ok) {
                this.expired = false;
                this.message = { type: "success", text: "Token refreshed" };
            } else {
                this.message = { type: "error", text: resp.error || "Refresh failed" };
            }
        } catch (e) {
            this.message = { type: "error", text: e.message };
        }
        this.refreshing = false;
    },

    async disconnect() {
        if (!confirm("Disconnect your Codex subscription? Tokens will be revoked.")) return;
        try {
            const resp = await this._api("oauth", { action: "disconnect" });
            if (resp.ok) {
                this.connected = false;
                this.accountInfo = null;
                this.proxyRunning = false;
                this.message = { type: "info", text: "Disconnected" };
            }
        } catch (e) {
            this.message = { type: "error", text: e.message };
        }
    },

    // ── Helpers ──

    async _api(endpoint, body) {
        const { callJsonApi } = await import("/js/api.js");
        return await callJsonApi(`plugins/codex-provider/${endpoint}`, body);
    },
});
