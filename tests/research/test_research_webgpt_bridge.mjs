import assert from 'node:assert/strict';
import {
    mkdirSync,
    mkdtempSync,
    rmSync,
    writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import {
    browserSessionIdFromWebSocketUrl,
    conversationIdFromUrl,
    normalizeReasoning,
    parseArgs,
    runBridgeCommand,
    validateCdpEndpoint,
    validateActiveBrowseSignals,
    validateCheckedSelections,
    validateCompletionSignals,
} from '../../scripts/research_webgpt_bridge.mjs';

const TEST_CDP_BINDING = {
    connectionEndpoint: 'ws://127.0.0.1:9222/devtools/browser/browser-cdp-001',
    browserSessionId: 'browser-cdp-001',
};

class FakeLocator {
    constructor(page, selector) {
        this.page = page;
        this.selector = selector;
    }

    last() {
        return this;
    }

    first() {
        return this;
    }

    nth() {
        return this;
    }

    filter() {
        return this;
    }

    locator(selector) {
        return new FakeLocator(this.page, selector);
    }

    async count() {
        if (this.selector === '[data-testid="accounts-profile-button"]') return 1;
        if (this.selector.includes('aria-haspopup="menu"')) return 1;
        if (this.selector === '[data-message-author-role="assistant"]') return 1;
        if (this.selector === '[data-message-author-role="user"]') return 1;
        if (this.selector.includes('data-system-hint-type="search"')) return 1;
        return 0;
    }

    async isVisible() {
        if (this.selector.includes('#prompt-textarea')) return true;
        if (this.selector === '[data-testid="accounts-profile-button"]') return true;
        if (this.selector === '[data-testid="composer-intelligence-picker-content"]') {
            return false;
        }
        if (this.selector === 'exact-text') return this.page.menuOpened;
        if (this.selector.includes('aria-haspopup="menu"')) return true;
        return this.selector === '[data-message-author-role="assistant"]';
    }

    async click() {
        this.page.menuOpened = true;
    }

    async hover() {
        this.page.menuOpened = true;
    }

    async evaluate(callback) {
        if (this.selector === '[data-testid="accounts-profile-button"]') return true;
        if (this.selector === 'exact-text') return false;
        return callback({});
    }

    async evaluateAll() {
        if (this.selector === '[role="menu"]') return 0;
        if (this.selector === '[data-testid="composer-intelligence-picker-content"]') {
            return this.page.menuOpened;
        }
        if (this.selector.startsWith('[role="menuitemradio"]')) {
            return this.page.menuOpened ? ['GPT-5.6 Sol', 'Very high'] : [];
        }
        if (this.selector === 'a[href*="/c/"]') {
            return [`https://chatgpt.com/c/${this.page.conversationId}`];
        }
        return [];
    }

    async allInnerTexts() {
        if (this.selector === '[data-message-author-role="user"]') {
            return [`bound request ${this.page.requestId}`];
        }
        return [];
    }
}

class FakePage {
    constructor(context, requestId, conversationId = 'conversation-new') {
        this.fakeContext = context;
        this.requestId = requestId;
        this.conversationId = conversationId;
        this.menuOpened = false;
        this.keyboard = {
            press: async () => {
                this.menuOpened = false;
            },
        };
    }

    context() {
        return this.fakeContext;
    }

    url() {
        return `https://chatgpt.com/c/${this.conversationId}`;
    }

    locator(selector) {
        return new FakeLocator(this, selector);
    }

    getByText() {
        return new FakeLocator(this, 'exact-text');
    }

    async waitForLoadState() {
        return undefined;
    }

    async waitForTimeout() {
        return undefined;
    }
}

function fakeChromium(requestId) {
    const context = {
        page: null,
        pages() {
            return [this.page];
        },
        async newCDPSession() {
            return {
                async send(method) {
                    if (method === 'Target.getTargetInfo') {
                        return { targetInfo: { targetId: 'target-001' } };
                    }
                    return {
                        product: 'Chrome/140.0.0.0',
                        userAgent: 'Mozilla/5.0 Chrome/140.0.0.0',
                    };
                },
                async detach() {
                    return undefined;
                },
            };
        },
    };
    context.page = new FakePage(context, requestId);
    const browser = {
        contexts: () => [context],
        async newBrowserCDPSession() {
            return {
                async send(method) {
                    assert.equal(method, 'Target.getTargetInfo');
                    return {
                        targetInfo: {
                            targetId: 'browser-cdp-001',
                            type: 'browser',
                        },
                    };
                },
                async detach() {
                    return undefined;
                },
            };
        },
    };
    return {
        browser,
        chromium: {
            async connectOverCDP() {
                return browser;
            },
        },
    };
}

function bridgeValues(agbrowseRoot) {
    return {
        'agbrowse-root': agbrowseRoot,
        'cdp-endpoint': 'http://127.0.0.1:9222',
        'request-id': 'request-001',
        role: 'WEB_SCOUT',
    };
}

test('argument parser keeps explicit external runtime and binding values', () => {
    const parsed = parseArgs([
        'preflight',
        '--agbrowse-root',
        'external-agbrowse',
        '--cdp-endpoint',
        'http://127.0.0.1:9222',
        '--browser-session-id',
        'browser-001',
        '--request-id',
        'request-001',
    ]);

    assert.equal(parsed.command, 'preflight');
    assert.equal(parsed.values['agbrowse-root'], 'external-agbrowse');
    assert.equal(parsed.values['browser-session-id'], 'browser-001');
    assert.equal(parsed.values['request-id'], 'request-001');
});

test('only credential-free loopback CDP endpoints are accepted', () => {
    assert.equal(
        validateCdpEndpoint('http://127.0.0.1:9222'),
        'http://127.0.0.1:9222/',
    );
    assert.throws(
        () => validateCdpEndpoint('https://example.test:9222'),
        /cdp_endpoint_invalid/u,
    );
    assert.throws(
        () => validateCdpEndpoint('http://user:secret@127.0.0.1:9222'),
        /cdp_endpoint_invalid/u,
    );
    assert.throws(
        () => validateCdpEndpoint('http://127.0.0.1:9222?token=secret'),
        /cdp_endpoint_invalid/u,
    );
    assert.equal(
        browserSessionIdFromWebSocketUrl(
            'ws://127.0.0.1:9222/devtools/browser/browser-cdp-001',
        ),
        'browser-cdp-001',
    );
    assert.throws(
        () => browserSessionIdFromWebSocketUrl(
            'ws://example.test:9222/devtools/browser/browser-cdp-001',
        ),
        /browser_session_binding_unavailable/u,
    );
});

test('model family and reasoning checks are exact and fail closed', () => {
    assert.deepEqual(
        validateCheckedSelections(['GPT-5.6 Sol', 'Very high']),
        { modelVerified: true, reasoningVerified: true },
    );
    assert.deepEqual(
        validateCheckedSelections(['GPT-5.5', 'Very high']),
        { modelVerified: false, reasoningVerified: true },
    );
    assert.deepEqual(
        validateCheckedSelections(['GPT-5.6 Sol', 'High']),
        { modelVerified: true, reasoningVerified: false },
    );
    assert.equal(normalizeReasoning('xhigh'), 'xhigh');
    assert.equal(normalizeReasoning('not very high'), null);
});

test('conversation URL parser rejects alternate origins and URL decorations', () => {
    assert.equal(
        conversationIdFromUrl('https://chatgpt.com/c/conversation-new'),
        'conversation-new',
    );
    assert.equal(
        conversationIdFromUrl('https://chatgpt.example/c/conversation-new'),
        null,
    );
    assert.equal(
        conversationIdFromUrl('https://chatgpt.com/c/conversation-new/extra'),
        null,
    );
});

test('completion needs an assistant result, composer, and no stop or error signal', () => {
    assert.deepEqual(
        validateCompletionSignals({
            assistantPresent: true,
            composerVisible: true,
            streamingActive: false,
            thinkingStopped: false,
            providerError: false,
            interrupted: false,
        }),
        {
            response_complete: true,
            thinking_stopped: false,
            interrupted: false,
        },
    );
    assert.equal(
        validateCompletionSignals({
            assistantPresent: true,
            composerVisible: true,
            streamingActive: true,
            thinkingStopped: false,
            providerError: false,
            interrupted: false,
        }).response_complete,
        false,
    );
    assert.deepEqual(
        validateCompletionSignals({
            assistantPresent: true,
            composerVisible: true,
            streamingActive: false,
            thinkingStopped: true,
            providerError: false,
            interrupted: true,
        }),
        {
            response_complete: true,
            thinking_stopped: true,
            interrupted: true,
        },
    );
});

test('active browse verification requires request-bound search or citation evidence', () => {
    assert.deepEqual(
        validateActiveBrowseSignals({
            requestSearchHintCount: 1,
            assistantCitationCount: 0,
        }),
        {
            active_browse_verified: true,
            active_browse_evidence_count: 1,
            request_search_hint_count: 1,
            assistant_citation_count: 0,
        },
    );
    assert.equal(
        validateActiveBrowseSignals({
            requestSearchHintCount: 0,
            assistantCitationCount: 0,
        }).active_browse_verified,
        false,
    );
});

test('preflight, rebind, and postflight preserve exact transport bindings', async (t) => {
    const root = mkdtempSync(join(tmpdir(), 'research-bridge-'));
    t.after(() => rmSync(root, { recursive: true, force: true }));
    mkdirSync(join(root, 'web-ai'), { recursive: true });
    writeFileSync(
        join(root, 'web-ai', 'session.mjs'),
        [
            'let targetId = null;',
            'export function bindSessionToTab(_sessionId, value) {',
            '  targetId = value;',
            '  return { targetId };',
            '}',
            'export function updateSession(_sessionId, { conversationUrl }) {',
            '  return { targetId, conversationUrl };',
            '}',
        ].join('\n'),
        'utf8',
    );
    const runtime = fakeChromium('request-001');
    const values = bridgeValues(root);

    const before = await runBridgeCommand('preflight', values, {
        chromium: runtime.chromium,
        cdpBinding: TEST_CDP_BINDING,
    });
    assert.equal(before.model_family, 'GPT-5.6 Sol Pro');
    assert.equal(before.model_base, 'GPT-5.6 Sol');
    assert.equal(before.access_tier, 'Pro');
    assert.equal(before.reasoning_profile, 'xhigh');
    assert.equal(before.ui_tuple_verified, true);
    assert.equal(before.fallback_used, false);
    assert.equal(before.browser_session_id, 'browser-cdp-001');
    assert.equal(before.request_id, 'request-001');
    assert.equal(before.conversation_id, 'conversation-new');
    assert.deepEqual(before.conversation_ids, ['conversation-new']);

    const rebound = await runBridgeCommand(
        'rebind',
        {
            ...values,
            'browser-session-id': before.browser_session_id,
            'target-id': 'target-001',
            'session-id': 'agbrowse-session-001',
            'timeout-seconds': '2',
        },
        {
            chromium: runtime.chromium,
            cdpBinding: TEST_CDP_BINDING,
            stdinPayload: {
                request_id: 'request-001',
                role: 'WEB_SCOUT',
                browser_session_id: before.browser_session_id,
                conversation_ids: [],
            },
        },
    );
    assert.equal(rebound.target_id, 'target-001');
    assert.equal(rebound.session_id, 'agbrowse-session-001');
    assert.equal(rebound.conversation_id, 'conversation-new');

    const assistant = await runBridgeCommand(
        'await-assistant',
        {
            ...values,
            'browser-session-id': before.browser_session_id,
            'target-id': 'target-001',
            'conversation-id': 'conversation-new',
            'timeout-seconds': '2',
        },
        {
            chromium: runtime.chromium,
            cdpBinding: TEST_CDP_BINDING,
        },
    );
    assert.equal(assistant.status, 'assistant-detected');
    assert.equal(assistant.assistant_count, 1);

    const after = await runBridgeCommand(
        'postflight',
        {
            ...values,
            'browser-session-id': before.browser_session_id,
            'target-id': 'target-001',
            'conversation-id': 'conversation-new',
        },
        {
            chromium: runtime.chromium,
            cdpBinding: TEST_CDP_BINDING,
        },
    );
    assert.equal(after.response_complete, true);
    assert.equal(after.thinking_stopped, false);
    assert.equal(after.interrupted, false);
    assert.equal(after.active_browse_verified, true);
    assert.equal(after.active_browse_evidence_count, 1);
    assert.equal(Number.isNaN(Date.parse(after.observed_at)), false);
});

test('rebind rejects a caller browser session echo that differs from CDP', async () => {
    const root = join(tmpdir(), 'unused-research-bridge-root');
    const runtime = fakeChromium('request-001');
    await assert.rejects(
        runBridgeCommand(
            'rebind',
            {
                ...bridgeValues(root),
                'browser-session-id': 'caller-invented-session',
                'target-id': 'target-001',
                'session-id': 'agbrowse-session-001',
            },
            {
                chromium: runtime.chromium,
                cdpBinding: TEST_CDP_BINDING,
                stdinPayload: {
                    request_id: 'request-001',
                    role: 'WEB_SCOUT',
                    browser_session_id: 'caller-invented-session',
                    conversation_ids: [],
                },
            },
        ),
        /browser_session_mismatch/u,
    );
});

test('rebind refuses a baseline from another request', async () => {
    const root = join(tmpdir(), 'unused-research-bridge-root');
    const runtime = fakeChromium('request-001');
    await assert.rejects(
        runBridgeCommand(
            'rebind',
            {
                ...bridgeValues(root),
                'browser-session-id': 'browser-cdp-001',
                'target-id': 'target-001',
                'session-id': 'agbrowse-session-001',
            },
            {
                chromium: runtime.chromium,
                cdpBinding: TEST_CDP_BINDING,
                stdinPayload: {
                    request_id: 'request-other',
                    role: 'WEB_SCOUT',
                    browser_session_id: 'browser-cdp-001',
                    conversation_ids: [],
                },
            },
        ),
        /rebind_input_binding_mismatch/u,
    );
});
