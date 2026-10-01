import { openConfirmDialog } from './confirm_dialog.js';
import { apiFetch } from './api_client.js';
import { setInlineStatus } from './ui_helpers.js';

// GPU-safe mode (Intel i915 hang workaround, user-space only). Named exception
// to the client-local Appearance rule at the owner's request: the launcher must
// read OUROBOROS_GPU_SAFE before the desktop window starts, so it is persisted
// server-side via the existing settings writer (partial single-key POST, never
// the s- collector). Toggling confirms, saves, then restarts the whole app via
// the desktop bridge when present (recreates the WebKit view with the vars);
// without a bridge it persists and explains the desktop relaunch (a server-only
// /restart cannot re-create the WebKit view).
export function mountGpuSafe(page, { ws } = {}) {
    const toggle = page.querySelector?.('[data-gpu-safe-toggle]');
    const status = page.querySelector?.('[data-gpu-safe-status]');
    if (!toggle) return () => {};
    const say = (text, tone = 'muted') => {
        if (status) setInlineStatus(status, text, tone);
    };
    const toBool = (v) => v === true || ['1', 'true', 'yes', 'on'].includes(String(v ?? '').trim().toLowerCase());
    let current = false;
    let busy = false;
    let loadGen = 0;
    const paint = (value, gen) => {
        if (gen !== undefined && gen !== loadGen) return;
        current = toBool(value);
        toggle.checked = current;
    };
    const myGen = ++loadGen;
    const loadCancelled = { value: false };
    apiFetch('/api/settings')
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => { if (!loadCancelled.value) paint(d?.settings?.OUROBOROS_GPU_SAFE ?? d?.OUROBOROS_GPU_SAFE ?? false, myGen); })
        .catch(() => { if (!loadCancelled.value) say('GPU-safe state unavailable — reload Settings to retry.', 'warn'); });
    const onChange = async () => {
        const next = toggle.checked;
        if (busy) { toggle.checked = current; return; }
        const myChange = ++loadGen;
        const confirmed = await openConfirmDialog({
            title: next ? 'Enable GPU-safe mode?' : 'Disable GPU-safe mode?',
            body: next
                ? 'The app will save GPU-safe mode and restart with software compositing (WEBKIT_DISABLE_COMPOSITING_MODE=1, WEBKIT_DISABLE_DMABUF_RENDERER=1). All running and queued tasks stop. No kernel changes.'
                : 'The app will save the change and restart without the software compositing vars. All running and queued tasks stop.',
            confirmLabel: 'Save and restart',
            danger: true,
        });
        if (!confirmed) { toggle.checked = current; return; }
        busy = true;
        say('Saving…');
        try {
            const saveRes = await apiFetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ OUROBOROS_GPU_SAFE: next }),
            });
            if (!saveRes.ok) throw new Error(`save failed (HTTP ${saveRes.status})`);
            paint(next, myChange);
            const bridge = window.pywebview?.api?.restart_desktop_app;
            if (typeof bridge === 'function') {
                say('Restarting app…');
                const out = await bridge();
                if (out?.ok === false) throw new Error(out?.error || 'desktop restart refused');
                return;
            }
            say('Saved. Restarting agent — then quit and reopen the desktop app to apply it to the window.', 'warn');
            const result = ws?.send?.({ type: 'command', cmd: '/restart' }, { queue: false });
            if (result?.status === 'sent') say('Restart requested. Relaunch the desktop app to apply GPU-safe to the window.', 'muted');
            else say('Saved, but restart was not sent (offline). Relaunch the desktop app to apply.', 'warn');
        } catch (e) {
            toggle.checked = current;
            say(`GPU-safe change failed: ${e.message}.`, 'warn');
        } finally {
            busy = false;
        }
    };
    toggle.addEventListener('change', onChange);
    return () => {
        loadCancelled.value = true;
        toggle.removeEventListener('change', onChange);
    };
}
