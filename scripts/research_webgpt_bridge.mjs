#!/usr/bin/env node

import { createRequire } from 'node:module';
import {
    isAbsolute,
    join,
    relative,
    resolve,
} from 'node:path';
import { pathToFileURL } from 'node:url';

const EXPECTED_MODEL = 'GPT-5.6 Sol Pro';
const EXPECTED_MODEL_BASE = 'GPT-5.6 Sol';
const EXPECTED_ACCESS_TIER = 'Pro';
const EXPECTED_REASONING = 'xhigh';
const REASONING_LABELS = new Set(['xhigh', 'very high', '매우 높음']);
const INTELLIGENCE_PICKER = '[data-testid="composer-intelligence-picker-content"]';
const INTELLIGENCE_TRIGGER = 'button.__composer-pill[aria-haspopup="menu"]';
const PROFILE_BUTTON = '[data-testid="accounts-profile-button"]';
const COMPOSER_PLUS_BUTTON = '[data-testid="composer-plus-btn"]';
const ACTIVE_WEB_SEARCH_PILL = [
    '[data-composer-surface]',
    '[data-inline-selection-pill][data-id="search"][data-system-hint-type="search"]',
].join(' ');
const USER_TURN_SELECTOR = [
    '[data-message-author-role="user"]',
    '[data-turn="user"]',
].join(',');
const ASSISTANT_TURN_SELECTOR = [
    '[data-message-author-role="assistant"]',
    '[data-turn="assistant"]',
].join(',');
const ALLOWED_ROLES = new Set(['WEB_SCOUT', 'RESEARCH_COMMANDER']);
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const CONVERSATION_PATH = /^\/c\/([A-Za-z0-9_-]+)\/?$/;
const MAX_STDIN_BYTES = 256 * 1024;
const MAX_NEW_CONVERSATIONS = 8;
const CHATGPT_HOST = 'chatgpt.com';

class BridgeFailure extends Error {
    constructor(code) {
        super(code);
        this.name = 'BridgeFailure';
        this.code = code;
    }
}

export function parseArgs(argv) {
    const [command, ...rest] = argv;
    const values = {};
    for (let index = 0; index < rest.length; index += 1) {
        const part = rest[index];
        if (!part.startsWith('--')) throw new BridgeFailure('invalid_argument');
        const key = part.slice(2);
        const next = rest[index + 1];
        if (next === undefined || next.startsWith('--')) {
            values[key] = true;
        } else {
            values[key] = next;
            index += 1;
        }
    }
    return { command, values };
}

function normalizeText(value) {
    return String(value || '').replace(/\s+/gu, ' ').trim();
}

export function normalizeReasoning(value) {
    const label = normalizeText(value).toLocaleLowerCase('en-US');
    return REASONING_LABELS.has(label) ? EXPECTED_REASONING : null;
}

export function conversationIdFromUrl(value) {
    try {
        const url = new URL(value);
        if (url.protocol !== 'https:' || url.hostname !== CHATGPT_HOST) return null;
        const match = CONVERSATION_PATH.exec(url.pathname);
        return match ? match[1] : null;
    } catch {
        return null;
    }
}

export function validateCdpEndpoint(value) {
    try {
        const endpoint = new URL(value);
        const loopback = new Set(['127.0.0.1', 'localhost', '[::1]']);
        if (
            !new Set(['http:', 'https:', 'ws:', 'wss:']).has(endpoint.protocol)
            || !loopback.has(endpoint.hostname)
            || endpoint.username
            || endpoint.password
            || endpoint.search
            || endpoint.hash
        ) {
            throw new BridgeFailure('cdp_endpoint_invalid');
        }
        return endpoint.toString();
    } catch (error) {
        if (error instanceof BridgeFailure) throw error;
        throw new BridgeFailure('cdp_endpoint_invalid');
    }
}

export function browserSessionIdFromWebSocketUrl(value) {
    try {
        const endpoint = new URL(value);
        const loopback = new Set(['127.0.0.1', 'localhost', '[::1]']);
        const match = /^\/devtools\/browser\/([A-Za-z0-9._:-]+)$/u.exec(endpoint.pathname);
        if (
            !new Set(['ws:', 'wss:']).has(endpoint.protocol)
            || !loopback.has(endpoint.hostname)
            || endpoint.username
            || endpoint.password
            || endpoint.search
            || endpoint.hash
            || !match
            || !IDENTIFIER.test(match[1])
        ) {
            throw new BridgeFailure('browser_session_binding_unavailable');
        }
        return match[1];
    } catch (error) {
        if (error instanceof BridgeFailure) throw error;
        throw new BridgeFailure('browser_session_binding_unavailable');
    }
}

async function resolveCdpBrowserBinding(cdpEndpoint, fetchImpl = globalThis.fetch) {
    const endpoint = new URL(cdpEndpoint);
    if (new Set(['ws:', 'wss:']).has(endpoint.protocol)) {
        return {
            connectionEndpoint: endpoint.toString(),
            browserSessionId: browserSessionIdFromWebSocketUrl(endpoint.toString()),
        };
    }
    if (typeof fetchImpl !== 'function') {
        throw new BridgeFailure('browser_session_binding_unavailable');
    }
    const discoveryUrl = new URL('/json/version', endpoint);
    let response;
    try {
        response = await fetchImpl(discoveryUrl, {
            method: 'GET',
            signal: AbortSignal.timeout(10_000),
        });
    } catch {
        throw new BridgeFailure('browser_session_binding_unavailable');
    }
    if (!response?.ok) throw new BridgeFailure('browser_session_binding_unavailable');
    let payload;
    try {
        payload = await response.json();
    } catch {
        throw new BridgeFailure('browser_session_binding_unavailable');
    }
    const webSocketUrl = payload?.webSocketDebuggerUrl;
    if (typeof webSocketUrl !== 'string') {
        throw new BridgeFailure('browser_session_binding_unavailable');
    }
    const socket = new URL(webSocketUrl);
    if (socket.port !== endpoint.port) {
        throw new BridgeFailure('browser_session_binding_unavailable');
    }
    return {
        connectionEndpoint: socket.toString(),
        browserSessionId: browserSessionIdFromWebSocketUrl(socket.toString()),
    };
}

function requiredIdentifier(values, key) {
    const value = values[key];
    if (typeof value !== 'string' || !IDENTIFIER.test(value)) {
        throw new BridgeFailure(`invalid_${key.replaceAll('-', '_')}`);
    }
    return value;
}

function optionalIdentifier(values, key) {
    if (values[key] === undefined) return null;
    return requiredIdentifier(values, key);
}

function requiredRole(values) {
    const role = requiredIdentifier(values, 'role');
    if (!ALLOWED_ROLES.has(role)) throw new BridgeFailure('invalid_role');
    return role;
}

function containedPath(root, ...parts) {
    if (!isAbsolute(root)) throw new BridgeFailure('agbrowse_root_not_absolute');
    const normalizedRoot = resolve(root);
    const candidate = resolve(normalizedRoot, ...parts);
    const traversal = relative(normalizedRoot, candidate);
    if (traversal.startsWith('..') || isAbsolute(traversal)) {
        throw new BridgeFailure('agbrowse_path_escape');
    }
    return candidate;
}

async function readStdinJson() {
    const chunks = [];
    let total = 0;
    for await (const chunk of process.stdin) {
        total += chunk.length;
        if (total > MAX_STDIN_BYTES) throw new BridgeFailure('stdin_too_large');
        chunks.push(chunk);
    }
    const text = Buffer.concat(chunks).toString('utf8').trim();
    if (!text) throw new BridgeFailure('stdin_required');
    try {
        const payload = JSON.parse(text);
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
            throw new BridgeFailure('stdin_invalid');
        }
        return payload;
    } catch (error) {
        if (error instanceof BridgeFailure) throw error;
        throw new BridgeFailure('stdin_invalid');
    }
}

async function loadExternalPlaywright(agbrowseRoot) {
    const require = createRequire(import.meta.url);
    const candidates = [
        containedPath(agbrowseRoot, 'node_modules', 'playwright-core'),
        containedPath(agbrowseRoot, 'node_modules', 'playwright'),
    ];
    for (const candidate of candidates) {
        try {
            const loaded = require(candidate);
            if (loaded?.chromium?.connectOverCDP) return loaded.chromium;
        } catch {
            // Only the explicitly supplied AGBrowse runtime is searched.
        }
    }
    throw new BridgeFailure('agbrowse_playwright_unavailable');
}

function isChatGptPage(page) {
    try {
        const url = new URL(page.url());
        return url.protocol === 'https:' && url.hostname === CHATGPT_HOST;
    } catch {
        return false;
    }
}

async function allChatGptPages(browser) {
    return browser.contexts()
        .flatMap((context) => context.pages())
        .filter(isChatGptPage);
}

async function targetIdFor(page) {
    const session = await page.context().newCDPSession(page);
    try {
        const result = await session.send('Target.getTargetInfo');
        const targetId = result?.targetInfo?.targetId;
        if (typeof targetId !== 'string' || !IDENTIFIER.test(targetId)) {
            throw new BridgeFailure('target_binding_missing');
        }
        return targetId;
    } finally {
        await session.detach().catch(() => undefined);
    }
}

async function proveHeadedChrome(page) {
    const session = await page.context().newCDPSession(page);
    try {
        const version = await session.send('Browser.getVersion');
        const product = normalizeText(version?.product);
        const userAgent = normalizeText(version?.userAgent);
        if (
            !product.toLocaleLowerCase('en-US').includes('chrome')
            || userAgent.toLocaleLowerCase('en-US').includes('headlesschrome')
        ) {
            throw new BridgeFailure('headed_chrome_required');
        }
    } finally {
        await session.detach().catch(() => undefined);
    }
}

async function hasComposer(page) {
    return page.locator(
        '#prompt-textarea, [contenteditable="true"][data-virtualkeyboard="true"]',
    ).last().isVisible().catch(() => false);
}

async function findPageByTargetId(browser, requestedTargetId) {
    for (const page of await allChatGptPages(browser)) {
        if (await targetIdFor(page) === requestedTargetId) return page;
    }
    throw new BridgeFailure('bound_target_unavailable');
}

async function findPreflightPage(browser, requestedTargetId) {
    if (requestedTargetId) {
        const page = await findPageByTargetId(browser, requestedTargetId);
        await page.waitForLoadState('domcontentloaded', { timeout: 15_000 })
            .catch(() => undefined);
        if (!(await hasComposer(page))) throw new BridgeFailure('composer_unavailable');
        return page;
    }
    const pages = (await allChatGptPages(browser)).reverse();
    const rankedPages = [
        ...pages.filter((page) => conversationIdFromUrl(page.url()) === null),
        ...pages.filter((page) => conversationIdFromUrl(page.url()) !== null),
    ];
    for (const page of rankedPages) {
        if (
            await hasComposer(page)
            && await page.locator(INTELLIGENCE_TRIGGER).last()
                .isVisible()
                .catch(() => false)
            && await page.locator(PROFILE_BUTTON).first()
                .isVisible()
                .catch(() => false)
        ) {
            return page;
        }
    }
    for (const page of rankedPages) {
        if (await hasComposer(page)) return page;
    }
    throw new BridgeFailure('authenticated_composer_unavailable');
}

async function visibleCheckedRows(page) {
    return page.locator(
        [
            '[role="menuitemradio"][aria-checked="true"]',
            '[role="menuitemradio"][data-state="checked"]',
            '[role="menuitem"][aria-checked="true"]',
            '[role="menuitem"][data-state="checked"]',
            '[role="radio"][aria-checked="true"]',
        ].join(','),
    ).evaluateAll((rows) => rows
        .filter((row) => {
            const style = window.getComputedStyle(row);
            const rect = row.getBoundingClientRect();
            return style.visibility !== 'hidden'
                && style.display !== 'none'
                && rect.width > 0
                && rect.height > 0;
        })
        .map((row) => (row.innerText || row.textContent || '').replace(/\s+/gu, ' ').trim()))
        .catch(() => []);
}

async function visibleExactLocators(page, text) {
    const candidates = page.getByText(text, { exact: true });
    const visible = [];
    const count = await candidates.count().catch(() => 0);
    for (let index = 0; index < count; index += 1) {
        const locator = candidates.nth(index);
        if (await locator.isVisible().catch(() => false)) visible.push(locator);
    }
    return visible;
}

async function exactLabelIsChecked(locator) {
    return locator.evaluate((node) => {
        let current = node;
        while (current && current !== document.body) {
            const checked = current.getAttribute?.('aria-checked') === 'true';
            const state = current.getAttribute?.('data-state') === 'checked';
            if (checked || state) return true;
            current = current.parentElement;
        }
        return false;
    }).catch(() => false);
}

async function intelligencePickerVisible(page) {
    return page.locator(INTELLIGENCE_PICKER).evaluateAll((pickers) => pickers
        .some((picker) => {
            const style = window.getComputedStyle(picker);
            const rect = picker.getBoundingClientRect();
            return style.visibility !== 'hidden'
                && style.display !== 'none'
                && rect.width > 0
                && rect.height > 0;
        }))
        .catch(() => false);
}

async function closeIntelligencePicker(page) {
    for (let index = 0; index < 4; index += 1) {
        if (!(await intelligencePickerVisible(page))) return;
        await page.keyboard.press('Escape').catch(() => undefined);
        await page.waitForTimeout(100);
    }
    if (await intelligencePickerVisible(page)) {
        throw new BridgeFailure('selector_menu_stuck');
    }
}

export function validateCheckedSelections(rows) {
    const normalized = rows.map(normalizeText);
    const modelMatches = normalized.filter((value) => value === EXPECTED_MODEL_BASE);
    const reasoningMatches = normalized.filter((value) => normalizeReasoning(value) !== null);
    return {
        modelVerified: modelMatches.length === 1,
        reasoningVerified: reasoningMatches.length === 1,
    };
}

async function verifyProAccessTier(page) {
    const candidates = page.locator(PROFILE_BUTTON);
    const count = await candidates.count().catch(() => 0);
    for (let index = 0; index < count; index += 1) {
        const candidate = candidates.nth(index);
        if (!(await candidate.isVisible().catch(() => false))) continue;
        const verified = await candidate.evaluate((node, expectedTier) => {
            const elements = [node, ...node.querySelectorAll('*')];
            return elements.some((element) => {
                if (element.children.length !== 0) return false;
                return (element.textContent || '').replace(/\s+/gu, ' ').trim()
                    === expectedTier;
            });
        }, EXPECTED_ACCESS_TIER).catch(() => false);
        if (verified) return;
    }
    throw new BridgeFailure('access_tier_mismatch');
}

async function openIntelligencePicker(page) {
    await closeIntelligencePicker(page);
    const triggers = page.locator(INTELLIGENCE_TRIGGER);
    const count = await triggers.count().catch(() => 0);
    for (let index = count - 1; index >= 0; index -= 1) {
        const trigger = triggers.nth(index);
        if (!(await trigger.isVisible().catch(() => false))) continue;
        await trigger.click({ timeout: 3_000, force: true }).catch(() => undefined);
        for (let attempt = 0; attempt < 10; attempt += 1) {
            await page.waitForTimeout(100);
            if (await intelligencePickerVisible(page)) return;
        }
    }
    throw new BridgeFailure('model_selector_unavailable');
}

async function verifyExactModelAndReasoning(page) {
    await verifyProAccessTier(page);
    await openIntelligencePicker(page);
    try {
        let rows = await visibleCheckedRows(page);
        let validated = validateCheckedSelections(rows);
        const reasoningLabel = rows.find((value) => normalizeReasoning(value) !== null);

        for (const exactModel of await visibleExactLocators(page, EXPECTED_MODEL_BASE)) {
            if (await exactLabelIsChecked(exactModel)) {
                validated = { ...validated, modelVerified: true };
                break;
            }
            await exactModel.hover({ timeout: 3_000 }).catch(() => undefined);
            await page.waitForTimeout(250);
            rows = await visibleCheckedRows(page);
            validated = validateCheckedSelections(rows);
            if (validated.modelVerified) break;
        }
        if (!validated.modelVerified) throw new BridgeFailure('model_mismatch');
        if (!validated.reasoningVerified || !reasoningLabel) {
            throw new BridgeFailure('reasoning_mismatch');
        }
        return {
            model_family: EXPECTED_MODEL,
            model_base: EXPECTED_MODEL_BASE,
            access_tier: EXPECTED_ACCESS_TIER,
            reasoning_profile: EXPECTED_REASONING,
            reasoning_ui_label: normalizeText(reasoningLabel),
            ui_tuple_verified: true,
            fallback_used: false,
        };
    } finally {
        await closeIntelligencePicker(page);
    }
}

async function collectConversationMetadata(browser) {
    const ids = new Set();
    const hrefs = new Set();
    for (const page of await allChatGptPages(browser)) {
        const currentId = conversationIdFromUrl(page.url());
        if (currentId) {
            ids.add(currentId);
            hrefs.add(`https://${CHATGPT_HOST}/c/${currentId}`);
        }
        const rows = await page.locator('a[href*="/c/"]').evaluateAll((anchors) => {
            const values = [];
            for (const anchor of anchors) {
                try {
                    const url = new URL(anchor.href, location.href);
                    if (
                        url.protocol === 'https:'
                        && url.hostname === 'chatgpt.com'
                        && /^\/c\/[A-Za-z0-9_-]+\/?$/.test(url.pathname)
                    ) {
                        values.push(`${url.origin}${url.pathname.replace(/\/$/u, '')}`);
                    }
                } catch {
                    // Ignore malformed or extension-owned links.
                }
            }
            return values;
        }).catch(() => []);
        for (const href of rows) {
            const conversationId = conversationIdFromUrl(href);
            if (conversationId) {
                ids.add(conversationId);
                hrefs.add(`https://${CHATGPT_HOST}/c/${conversationId}`);
            }
        }
    }
    return {
        conversation_ids: [...ids].sort(),
        conversation_hrefs: [...hrefs].sort(),
    };
}

async function pageContainsRequest(page, requestId) {
    const messages = await page.locator(USER_TURN_SELECTOR)
        .allInnerTexts()
        .catch(() => []);
    return messages.some((message) => message.includes(requestId));
}

function currentConversationId(page, targetId) {
    return conversationIdFromUrl(page.url()) || `new-chat:${targetId}`;
}

async function preflight(browser, values, browserSessionId) {
    const expectedBrowserSessionId = optionalIdentifier(values, 'browser-session-id');
    const requestId = requiredIdentifier(values, 'request-id');
    const role = requiredRole(values);
    const requestedTarget = optionalIdentifier(values, 'target-id');
    const requestedConversation = optionalIdentifier(values, 'conversation-id');
    const page = await findPreflightPage(browser, requestedTarget);
    await proveHeadedChrome(page);
    if (
        expectedBrowserSessionId !== null
        && expectedBrowserSessionId !== browserSessionId
    ) {
        throw new BridgeFailure('browser_session_mismatch');
    }
    const targetId = await targetIdFor(page);
    const selected = await verifyExactModelAndReasoning(page);
    const conversationId = currentConversationId(page, targetId);
    if (requestedConversation) {
        if (
            conversationId !== requestedConversation
            || !(await pageContainsRequest(page, requestId))
        ) {
            throw new BridgeFailure('conversation_binding_mismatch');
        }
    }
    const conversations = await collectConversationMetadata(browser);
    return {
        ok: true,
        status: 'verified',
        headed: true,
        cdp_connected: true,
        ...selected,
        browser_session_id: browserSessionId,
        request_id: requestId,
        role,
        target_id: targetId,
        conversation_id: conversationId,
        ...conversations,
    };
}

async function activateWebSearch(page) {
    if (await anyVisible(page.locator(ACTIVE_WEB_SEARCH_PILL))) return;
    const plusButton = page.locator(COMPOSER_PLUS_BUTTON).first();
    if (!(await plusButton.isVisible().catch(() => false))) {
        throw new BridgeFailure('web_search_menu_unavailable');
    }
    await plusButton.click({ timeout: 3_000 });
    const deadline = Date.now() + 10_000;
    let clicked = false;
    while (Date.now() < deadline && !clicked) {
        const labels = page.getByText(/^(?:Web search|웹 검색)$/iu);
        const count = await labels.count().catch(() => 0);
        for (let index = 0; index < count; index += 1) {
            const label = labels.nth(index);
            if (!(await label.isVisible().catch(() => false))) continue;
            const candidate = label.locator('xpath=ancestor::div[@tabindex="0"][1]');
            if (!(await candidate.isVisible().catch(() => false))) continue;
            await candidate.click({ timeout: 3_000 });
            clicked = true;
            break;
        }
        if (!clicked) await page.waitForTimeout(200);
    }
    if (!clicked) {
        await page.keyboard.press('Escape').catch(() => undefined);
        throw new BridgeFailure('web_search_option_unavailable');
    }
    await page.waitForTimeout(250);
    if (!(await anyVisible(page.locator(ACTIVE_WEB_SEARCH_PILL)))) {
        throw new BridgeFailure('web_search_not_armed');
    }
}

async function prepareActiveBrowse(browser, values, browserSessionId) {
    const expectedBrowserSessionId = requiredIdentifier(values, 'browser-session-id');
    const requestId = requiredIdentifier(values, 'request-id');
    const role = requiredRole(values);
    if (expectedBrowserSessionId !== browserSessionId) {
        throw new BridgeFailure('browser_session_mismatch');
    }
    const sourcePage = await findPreflightPage(browser, null);
    const page = await sourcePage.context().newPage();
    try {
        await page.goto(`https://${CHATGPT_HOST}/`, {
            waitUntil: 'domcontentloaded',
            timeout: 30_000,
        });
        await page.locator('#prompt-textarea').first().waitFor({
            state: 'visible',
            timeout: 30_000,
        });
        await page.locator(INTELLIGENCE_TRIGGER).last().waitFor({
            state: 'visible',
            timeout: 30_000,
        });
        await page.locator(PROFILE_BUTTON).first().waitFor({
            state: 'visible',
            timeout: 30_000,
        });
        await proveHeadedChrome(page);
        const targetId = await targetIdFor(page);
        if (conversationIdFromUrl(page.url()) !== null) {
            throw new BridgeFailure('fresh_conversation_not_blank');
        }
        const selected = await verifyExactModelAndReasoning(page);
        await activateWebSearch(page);
        return {
            ok: true,
            status: 'armed',
            headed: true,
            cdp_connected: true,
            ...selected,
            browser_session_id: browserSessionId,
            request_id: requestId,
            role,
            target_id: targetId,
            conversation_id: `new-chat:${targetId}`,
            active_browse_mode: 'WEB_SEARCH',
            active_browse_armed: true,
        };
    } catch (error) {
        await page.close().catch(() => undefined);
        throw error;
    }
}

function baselineConversationIds(payload) {
    const result = new Set();
    for (const key of ['conversation_ids', 'conversationIds']) {
        const rows = payload[key];
        if (!Array.isArray(rows)) continue;
        for (const value of rows) {
            if (typeof value === 'string' && IDENTIFIER.test(value)) result.add(value);
        }
    }
    for (const href of payload.conversation_hrefs || []) {
        const conversationId = conversationIdFromUrl(href);
        if (conversationId) result.add(conversationId);
    }
    return result;
}

async function openConversationClone(sourcePage, conversationId, requestId) {
    const page = await sourcePage.context().newPage();
    try {
        await page.goto(`https://${CHATGPT_HOST}/c/${conversationId}`, {
            waitUntil: 'domcontentloaded',
            timeout: 30_000,
        });
        if (
            conversationIdFromUrl(page.url()) !== conversationId
            || !(await pageContainsRequest(page, requestId))
        ) {
            await page.close().catch(() => undefined);
            return null;
        }
        return page;
    } catch {
        await page.close().catch(() => undefined);
        return null;
    }
}

async function persistSessionBinding(agbrowseRoot, sessionId, targetId, conversationId) {
    const sessionModuleUrl = pathToFileURL(
        containedPath(agbrowseRoot, 'web-ai', 'session.mjs'),
    ).href;
    let module;
    try {
        module = await import(sessionModuleUrl);
    } catch {
        throw new BridgeFailure('agbrowse_session_module_unavailable');
    }
    if (
        typeof module.bindSessionToTab !== 'function'
        || typeof module.updateSession !== 'function'
    ) {
        throw new BridgeFailure('agbrowse_session_contract_invalid');
    }
    const conversationUrl = `https://${CHATGPT_HOST}/c/${conversationId}`;
    const bound = module.bindSessionToTab(sessionId, targetId);
    const updated = module.updateSession(sessionId, { conversationUrl });
    if (
        bound?.targetId !== targetId
        || updated?.targetId !== targetId
        || updated?.conversationUrl !== conversationUrl
    ) {
        throw new BridgeFailure('agbrowse_session_rebind_failed');
    }
}

async function rebind(
    browser,
    values,
    baselinePayload,
    agbrowseRoot,
    browserSessionId,
) {
    const expectedBrowserSessionId = requiredIdentifier(values, 'browser-session-id');
    if (expectedBrowserSessionId !== browserSessionId) {
        throw new BridgeFailure('browser_session_mismatch');
    }
    const requestId = requiredIdentifier(values, 'request-id');
    const role = requiredRole(values);
    const targetId = requiredIdentifier(values, 'target-id');
    const sessionId = requiredIdentifier(values, 'session-id');
    const timeoutSeconds = Number(values['timeout-seconds'] || 45);
    if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0 || timeoutSeconds > 300) {
        throw new BridgeFailure('rebind_timeout_invalid');
    }
    if (
        baselinePayload.request_id !== requestId
        || baselinePayload.role !== role
        || baselinePayload.browser_session_id !== browserSessionId
    ) {
        throw new BridgeFailure('rebind_input_binding_mismatch');
    }
    const baseline = baselineConversationIds(baselinePayload);
    const sourcePage = await findPageByTargetId(browser, targetId);
    await proveHeadedChrome(sourcePage);
    const deadline = Date.now() + timeoutSeconds * 1000;
    const rejected = new Set();

    while (Date.now() < deadline) {
        const directId = conversationIdFromUrl(sourcePage.url());
        if (
            directId
            && !baseline.has(directId)
            && await pageContainsRequest(sourcePage, requestId)
        ) {
            await persistSessionBinding(agbrowseRoot, sessionId, targetId, directId);
            return {
                ok: true,
                status: 'rebound',
                rebound: false,
                browser_session_id: browserSessionId,
                request_id: requestId,
                role,
                session_id: sessionId,
                target_id: targetId,
                conversation_id: directId,
                conversation_url: `https://${CHATGPT_HOST}/c/${directId}`,
            };
        }

        const current = await collectConversationMetadata(browser);
        const candidates = current.conversation_ids.filter(
            (value) => !baseline.has(value) && !rejected.has(value),
        );
        if (candidates.length > MAX_NEW_CONVERSATIONS) {
            throw new BridgeFailure('conversation_set_ambiguous');
        }
        for (const conversationId of candidates) {
            const page = await openConversationClone(sourcePage, conversationId, requestId);
            if (!page) {
                rejected.add(conversationId);
                continue;
            }
            await proveHeadedChrome(page);
            const reboundTargetId = await targetIdFor(page);
            await persistSessionBinding(
                agbrowseRoot,
                sessionId,
                reboundTargetId,
                conversationId,
            );
            return {
                ok: true,
                status: 'rebound',
                rebound: true,
                browser_session_id: browserSessionId,
                request_id: requestId,
                role,
                session_id: sessionId,
                target_id: reboundTargetId,
                conversation_id: conversationId,
                conversation_url: `https://${CHATGPT_HOST}/c/${conversationId}`,
            };
        }
        await sourcePage.waitForTimeout(250);
    }
    throw new BridgeFailure('fresh_conversation_not_found');
}

async function awaitAssistant(browser, values, browserSessionId) {
    const expectedBrowserSessionId = requiredIdentifier(values, 'browser-session-id');
    const requestId = requiredIdentifier(values, 'request-id');
    const role = requiredRole(values);
    const targetId = requiredIdentifier(values, 'target-id');
    const conversationId = requiredIdentifier(values, 'conversation-id');
    const timeoutSeconds = Number(values['timeout-seconds'] || 1_800);
    if (
        !Number.isFinite(timeoutSeconds)
        || timeoutSeconds <= 0
        || timeoutSeconds > 3_600
    ) {
        throw new BridgeFailure('assistant_wait_timeout_invalid');
    }
    if (expectedBrowserSessionId !== browserSessionId) {
        throw new BridgeFailure('browser_session_mismatch');
    }
    const page = await findPageByTargetId(browser, targetId);
    await proveHeadedChrome(page);
    if (
        conversationIdFromUrl(page.url()) !== conversationId
        || !(await pageContainsRequest(page, requestId))
    ) {
        throw new BridgeFailure('conversation_binding_mismatch');
    }

    const deadline = Date.now() + timeoutSeconds * 1_000;
    while (Date.now() < deadline) {
        const assistantCount = await page.locator(
            ASSISTANT_TURN_SELECTOR,
        ).count().catch(() => 0);
        if (assistantCount > 0) {
            return {
                ok: true,
                status: 'assistant-detected',
                browser_session_id: browserSessionId,
                request_id: requestId,
                role,
                target_id: targetId,
                conversation_id: conversationId,
                assistant_count: assistantCount,
            };
        }
        const failed = await anyVisible(
            page.locator(
                [
                    '[data-testid="conversation-turn-error"]',
                    '[data-message-status="error"]',
                    '[data-testid="thinking-stopped"]',
                    '[data-testid="generation-stopped"]',
                    '[data-message-status="stopped"]',
                    '[data-testid="generation-interrupted"]',
                    '[data-message-status="interrupted"]',
                ].join(','),
            ),
        );
        if (failed) throw new BridgeFailure('response_interrupted_before_assistant');
        await page.waitForTimeout(500);
    }
    throw new BridgeFailure('assistant_wait_timeout');
}

async function anyVisible(locator) {
    const count = await locator.count().catch(() => 0);
    for (let index = 0; index < count; index += 1) {
        if (await locator.nth(index).isVisible().catch(() => false)) return true;
    }
    return false;
}

export function validateCompletionSignals(signals) {
    const responseComplete = (
        signals.assistantPresent === true
        && signals.composerVisible === true
        && signals.streamingActive === false
        && signals.providerError === false
    );
    return {
        response_complete: responseComplete,
        thinking_stopped: signals.thinkingStopped === true,
        interrupted: signals.interrupted === true || signals.providerError === true,
    };
}

export function validateActiveBrowseSignals(signals) {
    const requestSearchHintCount = Number(signals.requestSearchHintCount || 0);
    const assistantCitationCount = Number(signals.assistantCitationCount || 0);
    const activeBrowseVerified = (
        Number.isInteger(requestSearchHintCount)
        && requestSearchHintCount > 0
    ) || (
        Number.isInteger(assistantCitationCount)
        && assistantCitationCount > 0
    );
    return {
        active_browse_verified: activeBrowseVerified,
        active_browse_evidence_count: (
            Math.max(0, requestSearchHintCount)
            + Math.max(0, assistantCitationCount)
        ),
        request_search_hint_count: Math.max(0, requestSearchHintCount),
        assistant_citation_count: Math.max(0, assistantCitationCount),
    };
}

async function inspectActiveBrowse(page, requestId) {
    const requestTurns = page.locator(USER_TURN_SELECTOR).filter({
        hasText: requestId,
    });
    let requestSearchHintCount = 0;
    const requestTurnCount = await requestTurns.count().catch(() => 0);
    for (let index = 0; index < requestTurnCount; index += 1) {
        const turn = requestTurns.nth(index);
        const directCount = await turn.locator(
            [
                '[data-system-hint-type="search"]',
                '[data-inline-selection-pill][data-id="search"]',
            ].join(','),
        ).count().catch(() => 0);
        if (directCount > 0) {
            requestSearchHintCount += directCount;
            continue;
        }
        const article = turn.locator('xpath=ancestor::*[self::article or starts-with(@data-testid, "conversation-turn")][1]');
        if (await article.count().catch(() => 0)) {
            requestSearchHintCount += await article.locator(
                [
                    '[data-system-hint-type="search"]',
                    '[data-inline-selection-pill][data-id="search"]',
                ].join(','),
            ).count().catch(() => 0);
        }
    }

    const assistantTurns = page.locator(ASSISTANT_TURN_SELECTOR);
    const assistantCount = await assistantTurns.count().catch(() => 0);
    let assistantCitationCount = 0;
    if (assistantCount > 0) {
        const assistant = assistantTurns.nth(assistantCount - 1);
        assistantCitationCount = await assistant.locator(
            [
                'a[href^="http://"]',
                'a[href^="https://"]',
                '[data-testid*="citation" i]',
                '[data-testid*="source" i]',
                'button[aria-label*="source" i]',
                'button[aria-label*="citation" i]',
                'button[aria-label*="출처" i]',
            ].join(','),
        ).count().catch(() => 0);
    }
    return validateActiveBrowseSignals({
        requestSearchHintCount,
        assistantCitationCount,
    });
}

async function inspectCompletion(page) {
    const assistantPresent = await anyVisible(
        page.locator(ASSISTANT_TURN_SELECTOR),
    );
    const streamingActive = await anyVisible(
        page.locator(
            [
                'button[data-testid="stop-button"]',
                'button[aria-label="Stop generating"]',
                'button[aria-label="응답 생성 중지"]',
                '[data-message-author-role="assistant"][data-is-streaming="true"]',
                '[data-turn="assistant"][data-is-streaming="true"]',
                '[data-streaming="true"]',
            ].join(','),
        ),
    );
    const thinkingStopped = await anyVisible(
        page.locator(
            [
                '[data-testid="thinking-stopped"]',
                '[data-testid="generation-stopped"]',
                '[data-message-status="stopped"]',
            ].join(','),
        ),
    );
    const providerError = await anyVisible(
        page.locator(
            [
                '[data-testid="conversation-turn-error"]',
                '[data-message-status="error"]',
                '.text-token-text-error',
            ].join(','),
        ),
    );
    const interrupted = thinkingStopped || await anyVisible(
        page.locator(
            [
                '[data-testid="generation-interrupted"]',
                '[data-message-status="interrupted"]',
            ].join(','),
        ),
    );
    return validateCompletionSignals({
        assistantPresent,
        composerVisible: await hasComposer(page),
        streamingActive,
        thinkingStopped,
        providerError,
        interrupted,
    });
}

async function postflight(browser, values, browserSessionId) {
    const verified = await preflight(browser, values, browserSessionId);
    const page = await findPageByTargetId(browser, verified.target_id);
    const completion = await inspectCompletion(page);
    const activeBrowse = await inspectActiveBrowse(page, verified.request_id);
    if (
        completion.response_complete !== true
        || completion.thinking_stopped !== false
        || completion.interrupted !== false
    ) {
        throw new BridgeFailure('response_not_complete');
    }
    if (activeBrowse.active_browse_verified !== true) {
        throw new BridgeFailure('active_browse_not_verified');
    }
    return {
        ...verified,
        status: 'complete',
        observed_at: new Date().toISOString(),
        ...completion,
        ...activeBrowse,
    };
}

export async function runBridgeCommand(command, values, dependencies = {}) {
    if (!new Set([
        'preflight',
        'prepare-active-browse',
        'rebind',
        'await-assistant',
        'postflight',
    ]).has(command)) {
        throw new BridgeFailure('unsupported_command');
    }
    const agbrowseRoot = values['agbrowse-root'];
    if (typeof agbrowseRoot !== 'string' || !isAbsolute(agbrowseRoot)) {
        throw new BridgeFailure('agbrowse_root_not_absolute');
    }
    const cdpEndpoint = validateCdpEndpoint(values['cdp-endpoint']);
    const cdpBinding = dependencies.cdpBinding
        || await resolveCdpBrowserBinding(cdpEndpoint, dependencies.fetch);
    const chromium = dependencies.chromium || await loadExternalPlaywright(agbrowseRoot);
    let browser;
    try {
        browser = await chromium.connectOverCDP(
            cdpBinding.connectionEndpoint,
            { timeout: 20_000 },
        );
    } catch {
        throw new BridgeFailure('cdp_connection_failed');
    }
    if (command === 'preflight') {
        return preflight(browser, values, cdpBinding.browserSessionId);
    }
    if (command === 'prepare-active-browse') {
        return prepareActiveBrowse(browser, values, cdpBinding.browserSessionId);
    }
    if (command === 'await-assistant') {
        return awaitAssistant(browser, values, cdpBinding.browserSessionId);
    }
    if (command === 'postflight') {
        return postflight(browser, values, cdpBinding.browserSessionId);
    }
    return rebind(
        browser,
        values,
        dependencies.stdinPayload || await readStdinJson(),
        agbrowseRoot,
        cdpBinding.browserSessionId,
    );
}

function safeFailure(error) {
    return error instanceof BridgeFailure ? error.code : 'bridge_internal_error';
}

async function main() {
    const { command, values } = parseArgs(process.argv.slice(2));
    return runBridgeCommand(command, values);
}

const invokedPath = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : '';
if (invokedPath === import.meta.url) {
    main()
        .then((result) => {
            process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));
        })
        .catch((error) => {
            process.stdout.write(
                `${JSON.stringify({ ok: false, error: safeFailure(error) })}\n`,
                () => process.exit(1),
            );
        });
}
