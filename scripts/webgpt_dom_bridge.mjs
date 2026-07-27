#!/usr/bin/env node

import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const EXPECTED_FAMILY = 'GPT-5.6 Sol';
const EXPECTED_REASONING_LABELS = new Set([
    '매우 높음',
    'very high',
    'xhigh',
]);
const CONVERSATION_PATH = /^\/c\/[A-Za-z0-9_-]+\/?$/;

function parseArgs(argv) {
    const [command, ...rest] = argv;
    const values = {};
    for (let index = 0; index < rest.length; index += 1) {
        const part = rest[index];
        if (!part.startsWith('--')) continue;
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

async function readStdinJson() {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const text = Buffer.concat(chunks).toString('utf8').trim();
    if (!text) return {};
    const payload = JSON.parse(text);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('stdin JSON root must be an object');
    }
    return payload;
}

function normalizeText(value) {
    return String(value || '').replace(/\s+/gu, ' ').trim();
}

function normalizedReasoning(value) {
    const label = normalizeText(value).toLocaleLowerCase();
    for (const expected of EXPECTED_REASONING_LABELS) {
        if (label === expected || label.includes(expected)) return 'xhigh';
    }
    return null;
}

function isConversationUrl(value) {
    try {
        const url = new URL(value);
        return url.hostname === 'chatgpt.com' && CONVERSATION_PATH.test(url.pathname);
    } catch {
        return false;
    }
}

async function targetIdFor(page) {
    const session = await page.context().newCDPSession(page);
    try {
        const result = await session.send('Target.getTargetInfo');
        return result?.targetInfo?.targetId || null;
    } finally {
        await session.detach().catch(() => undefined);
    }
}

async function allChatGptPages(browser) {
    const pages = browser.contexts()
        .flatMap((context) => context.pages())
        .filter((page) => {
            try {
                return new URL(page.url()).hostname === 'chatgpt.com';
            } catch {
                return false;
            }
        });
    return pages;
}

async function findPageByTargetId(browser, requestedTargetId) {
    for (const page of await allChatGptPages(browser)) {
        if (await targetIdFor(page) === requestedTargetId) return page;
    }
    throw new Error('bound ChatGPT target is not available');
}

async function hasComposer(page) {
    return page.locator(
        '#prompt-textarea, [contenteditable="true"][data-virtualkeyboard="true"]',
    ).last().isVisible().catch(() => false);
}

async function findPreflightPage(browser, requestedTargetId) {
    if (requestedTargetId) {
        const page = await findPageByTargetId(browser, requestedTargetId);
        await page.waitForLoadState('domcontentloaded', { timeout: 15_000 })
            .catch(() => undefined);
        if (!(await hasComposer(page))) {
            throw new Error('bound ChatGPT target has no ready composer');
        }
        return page;
    }
    const pages = (await allChatGptPages(browser)).reverse();
    for (const page of pages) {
        if (await hasComposer(page)) return page;
    }
    throw new Error('no authenticated ChatGPT composer is available');
}

async function visibleCheckedRows(page) {
    return page.locator(
        [
            '[role="menuitemradio"][aria-checked="true"]',
            '[role="menuitemradio"][data-state="checked"]',
            '[role="menuitem"][aria-checked="true"]',
            '[role="menuitem"][data-state="checked"]',
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

async function visibleExactTextLocators(page, text) {
    const candidates = page.getByText(text, { exact: true });
    const result = [];
    const count = await candidates.count().catch(() => 0);
    for (let index = 0; index < count; index += 1) {
        const locator = candidates.nth(index);
        if (await locator.isVisible().catch(() => false)) result.push(locator);
    }
    return result;
}

async function closeVisibleMenus(page) {
    for (let index = 0; index < 4; index += 1) {
        const visibleMenus = await page.locator('[role="menu"]').evaluateAll((menus) => (
            menus.filter((menu) => {
                const style = window.getComputedStyle(menu);
                const rect = menu.getBoundingClientRect();
                return style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && rect.width > 0
                    && rect.height > 0;
            }).length
        )).catch(() => 0);
        if (visibleMenus === 0) return;
        await page.keyboard.press('Escape').catch(() => undefined);
        await page.waitForTimeout(150);
    }
    throw new Error('model menu did not close cleanly');
}

async function openReasoningMenu(page) {
    await closeVisibleMenus(page);
    const pills = page.locator(
        'button.__composer-pill[aria-haspopup="menu"], '
        + '[role="button"].__composer-pill[aria-haspopup="menu"]',
    );
    const count = await pills.count().catch(() => 0);
    let fallback = null;
    for (let index = count - 1; index >= 0; index -= 1) {
        const pill = pills.nth(index);
        if (!(await pill.isVisible().catch(() => false))) continue;
        const text = normalizeText(await pill.innerText().catch(() => ''));
        if (!fallback) fallback = pill;
        if (normalizedReasoning(text) === 'xhigh') {
            await pill.click({ timeout: 5_000 });
            await page.waitForTimeout(350);
            return;
        }
    }
    if (!fallback) throw new Error('reasoning selector pill is unavailable');
    await fallback.click({ timeout: 5_000 });
    await page.waitForTimeout(350);
}

async function exposeFamilyMenu(page) {
    let checked = await visibleCheckedRows(page);
    if (checked.some((text) => normalizeText(text) === EXPECTED_FAMILY)) return checked;
    const candidates = await visibleExactTextLocators(page, EXPECTED_FAMILY);
    for (const candidate of candidates) {
        await candidate.hover({ timeout: 3_000 }).catch(() => undefined);
        await page.waitForTimeout(300);
        checked = await visibleCheckedRows(page);
        if (checked.some((text) => normalizeText(text) === EXPECTED_FAMILY)) return checked;

        await candidate.focus().catch(() => undefined);
        await page.keyboard.press('ArrowRight').catch(() => undefined);
        await page.waitForTimeout(300);
        checked = await visibleCheckedRows(page);
        if (checked.some((text) => normalizeText(text) === EXPECTED_FAMILY)) return checked;
    }
    return checked;
}

async function collectConversationHrefs(browser) {
    const hrefs = new Set();
    for (const page of await allChatGptPages(browser)) {
        const rows = await page.locator('a[href*="/c/"]').evaluateAll((anchors) => {
            const values = [];
            for (const anchor of anchors) {
                try {
                    const url = new URL(anchor.href, location.href);
                    if (url.hostname === 'chatgpt.com' && /^\/c\/[A-Za-z0-9_-]+\/?$/.test(url.pathname)) {
                        values.push(`${url.origin}${url.pathname.replace(/\/$/, '')}`);
                    }
                } catch {
                    // Ignore malformed or extension-owned links.
                }
            }
            return values;
        }).catch(() => []);
        for (const href of rows) hrefs.add(href);
    }
    return [...hrefs].sort();
}

async function preflight(browser, requestedTargetId) {
    const page = await findPreflightPage(browser, requestedTargetId);
    let checked = [];
    try {
        await openReasoningMenu(page);
        checked = await visibleCheckedRows(page);
        const reasoningRow = checked.find((text) => normalizedReasoning(text) === 'xhigh');
        if (!reasoningRow) {
            throw new Error('checked reasoning level is not xhigh');
        }
        checked = await exposeFamilyMenu(page);
        const familyRow = checked.find(
            (text) => normalizeText(text) === EXPECTED_FAMILY,
        );
        if (!familyRow) {
            throw new Error('checked model family is not GPT-5.6 Sol');
        }
        return {
            ok: true,
            family: EXPECTED_FAMILY,
            reasoning: 'xhigh',
            target_id: await targetIdFor(page),
            conversation_hrefs: await collectConversationHrefs(browser),
        };
    } finally {
        await closeVisibleMenus(page);
    }
}

async function pageContainsRequest(page, requestId) {
    if (!requestId) return true;
    await page.locator('[data-message-author-role="user"]').first()
        .waitFor({ state: 'attached', timeout: 10_000 })
        .catch(() => undefined);
    const messages = await page.locator('[data-message-author-role="user"]')
        .allInnerTexts()
        .catch(() => []);
    return messages.some((message) => message.includes(requestId));
}

async function openConversationClone(sourcePage, href, requestId) {
    const candidatePage = await sourcePage.context().newPage();
    try {
        await candidatePage.goto(href, {
            waitUntil: 'domcontentloaded',
            timeout: 30_000,
        });
        if (!isConversationUrl(candidatePage.url())) {
            await candidatePage.close().catch(() => undefined);
            return null;
        }
        if (!(await pageContainsRequest(candidatePage, requestId))) {
            await candidatePage.close().catch(() => undefined);
            return null;
        }
        return candidatePage;
    } catch (error) {
        await candidatePage.close().catch(() => undefined);
        throw error;
    }
}

async function persistSessionBinding(
    agbrowseRoot,
    sessionId,
    targetId,
    conversationUrl,
) {
    if (!sessionId) throw new Error('rebind requires --session-id');
    const sessionModuleUrl = pathToFileURL(
        join(agbrowseRoot, 'web-ai', 'session.mjs'),
    ).href;
    const { bindSessionToTab, updateSession } = await import(sessionModuleUrl);
    const bound = bindSessionToTab(sessionId, targetId);
    if (!bound || bound.targetId !== targetId) {
        throw new Error('AGBrowse session target was not rebound');
    }
    const updated = updateSession(sessionId, { conversationUrl });
    if (
        !updated
        || updated.targetId !== targetId
        || updated.conversationUrl !== conversationUrl
    ) {
        throw new Error('AGBrowse session conversation URL was not persisted');
    }
}

async function rebind(browser, values, baselinePayload, agbrowseRoot) {
    const targetId = String(values['target-id'] || '');
    const sessionId = String(values['session-id'] || '');
    const timeoutSeconds = Number(values['timeout-seconds'] || 45);
    const requestId = String(baselinePayload.request_id || '');
    const baselineRows = Array.isArray(baselinePayload.conversation_hrefs)
        ? baselinePayload.conversation_hrefs.filter((value) => typeof value === 'string')
        : [];
    const baseline = new Set(baselineRows);
    if (!targetId) throw new Error('rebind requires --target-id');
    if (!Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0) {
        throw new Error('rebind timeout must be positive');
    }

    const page = await findPageByTargetId(browser, targetId);
    if (isConversationUrl(page.url()) && await pageContainsRequest(page, requestId)) {
        await persistSessionBinding(agbrowseRoot, sessionId, targetId, page.url());
        return {
            ok: true,
            rebound: false,
            target_id: targetId,
            conversation_url: page.url(),
        };
    }

    const deadline = Date.now() + timeoutSeconds * 1000;
    const rejected = new Set();
    while (Date.now() < deadline) {
        if (isConversationUrl(page.url()) && await pageContainsRequest(page, requestId)) {
            await persistSessionBinding(agbrowseRoot, sessionId, targetId, page.url());
            return {
                ok: true,
                rebound: false,
                target_id: targetId,
                conversation_url: page.url(),
            };
        }
        const current = await collectConversationHrefs(browser);
        const candidates = current.filter(
            (href) => !baseline.has(href) && !rejected.has(href),
        );
        if (candidates.length > 8) {
            throw new Error('new conversation set is ambiguous');
        }
        for (const href of candidates) {
            const candidatePage = await openConversationClone(page, href, requestId);
            if (candidatePage) {
                const reboundTargetId = await targetIdFor(candidatePage);
                await persistSessionBinding(
                    agbrowseRoot,
                    sessionId,
                    reboundTargetId,
                    candidatePage.url(),
                );
                return {
                    ok: true,
                    rebound: true,
                    target_id: reboundTargetId,
                    conversation_url: candidatePage.url(),
                };
            }
            rejected.add(href);
        }
        await page.waitForTimeout(500);
    }
    throw new Error('new conversation could not be bound to the AGBrowse target');
}

async function main() {
    const { command, values } = parseArgs(process.argv.slice(2));
    const agbrowseRoot = String(values['agbrowse-root'] || '');
    const cdpEndpoint = String(values['cdp-endpoint'] || '');
    if (!agbrowseRoot || !cdpEndpoint) {
        throw new Error('--agbrowse-root and --cdp-endpoint are required');
    }
    const require = createRequire(import.meta.url);
    const { chromium } = require(join(agbrowseRoot, 'node_modules', 'playwright-core'));
    const browser = await chromium.connectOverCDP(cdpEndpoint, { timeout: 20_000 });
    if (command === 'preflight') {
        return preflight(browser, values['target-id'] || null);
    }
    if (command === 'rebind') {
        const baseline = await readStdinJson();
        return rebind(browser, values, baseline, agbrowseRoot);
    }
    throw new Error(`unsupported bridge command: ${command || 'none'}`);
}

main()
    .then((result) => {
        process.stdout.write(`${JSON.stringify(result)}\n`, () => process.exit(0));
    })
    .catch((error) => {
        const message = String(error?.message || error).replace(/\s+/gu, ' ').slice(0, 1000);
        process.stdout.write(
            `${JSON.stringify({ ok: false, error: message })}\n`,
            () => process.exit(1),
        );
    });
