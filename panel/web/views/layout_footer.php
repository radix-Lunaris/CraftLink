<footer class="footer">
    <span><?= h(t('CraftLink 管理面板')) ?> v2.0.0</span>

    <?php
    // 备案号与版权：直接内置，不需要改任何配置文件。
    // （config.php 里若额外提供了 panel.icp / panel.copyright 则优先使用，
    //   方便以后换号 / 换版权行。）
    //
    // 备案号本身是数据（登记号），永远不翻译；版权那句话是面板自己写的，
    // 跟着语言走 —— 所以它是 t('...') 的键，不是写死的字符串。
    //
    // [!] 备案号必须是纯文本：这里**不许**加任何指向备案查询站的外链。
    //     用户明确要求它不带链接，加回去就是 bug（tests/panel_static_test.py
    //     里有一条检查守着这件事）。
    $icp = trim((string) (config('panel.icp') ?? ''));

    if ($icp === '') {
        $icp = '陇ICP备2026008614号';
    }

    $copyright = trim((string) (config('panel.copyright') ?? ''));

    if ($copyright === '') {
        // 产品已改名：MC-Link → CraftLink（方块联机）。
        // 中文原文里带中文括号，英文表里只留 CraftLink 本身。
        $copyright = sprintf(t('© %s CraftLink（方块联机）'), date('Y'));
    }
    ?>

    <span class="legal">
        <?= h($icp) ?>
        <span class="sep">·</span>
        <?= h($copyright) ?>
    </span>
</footer>

<?php
// ---- 页面脚本要用到的文案 ----------------------------------------------
//
// 集中在这里由 PHP 写好、JSON 内联给脚本用，脚本里因此**一个中文都不出现**：
// 切到英文时，自动刷新那两个状态药丸和关房等待对话框的每一句话都跟着变。
//
// 为什么必须这样：脚本是部署包里的 .php 一部分（内联在 footer 里，
// 见下面的说明），它没法知道用户选了哪种语言 —— 只有 PHP 知道。
// 写死中文的话，英文界面下点「关闭房间」会突然冒出一整屏中文。
$js_strings = [
    // 自动刷新：布尔值 -> 药丸
    'ok'              => t('正常'),
    'bad'             => t('不可用'),

    // 关房等待对话框
    'wait_title'      => t('正在关闭房间，请稍候'),
    'wait_title_named' => t('正在关闭房间「%s」，请稍候'),
    'wait_elapsed'    => t('已等待 %s 秒'),
    'wait_slow'       => t('中继还没有确认，最多再等几秒（房间可能已经卡住）'),
    'wait_done'       => t('房间已关闭'),
    'wait_problem'    => t('关闭房间没有完成'),
    'hint_default'    => t('正在通知房间里的玩家退出，最多等待 %s 秒'),
    'back_to_rooms'   => t('返回房间列表'),
    'reload'          => t('刷新页面'),
    'close'           => t('关闭'),

    // 各种失败原因（这些以前都硬写在脚本里）
    'bad_reply'       => t('服务器返回了无法识别的回复（HTTP %s）'),
    'done_message'    => t('房间已关闭。'),
    'unknown_problem' => t('关闭失败，中继没有说明原因。'),
    'timeout'         => t('等待超时：中继还没有确认。请刷新页面查看房间是否已经关闭。'),
    'offline'         => t('无法连接面板服务器（网络中断，或面板正在重启）。请刷新页面重试。'),
    'request_failed'  => t('请求失败，请刷新页面重试。'),
];
?>

<?php
// ---- 实时数据更新 ------------------------------------------------------
//
// Polls web/live.php and patches the numbers marked with data-live="...".
//
// It deliberately does NOT reload the page, and it shows NO banner: the
// previous version printed "数据每 N 秒自动更新" in the footer, which is
// noise on every page for something the user never asked to be told about.
// Updated values simply change in place.
//
// Three things keep it from getting in the way:
//   * it skips a tick while the user is typing in a field
//   * it stops while the tab is hidden
//   * if a poll fails it backs off instead of hammering a struggling server
//
// STRINGS 是上面那份 PHP 生成的文案表（全局，下面那个脚本也在用）。
?>
<script>
var STRINGS = <?= json_encode($js_strings, JSON_UNESCAPED_UNICODE | JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT) ?>;

(function () {
    var INTERVAL = <?= (int) AUTO_REFRESH_SECONDS ?> * 1000;

    var timer = null;
    var backoff = 0;
    var failures = 0;

    /**
     * Skip a tick while the user is doing something.
     *
     * Patching numbers is harmless in itself, but a value shifting under the
     * cursor while somebody reads a table is still annoying, and a page that
     * moves during an edit is worse.
     */
    function userIsBusy() {
        var active = document.activeElement;

        if (active) {
            var tag = (active.tagName || '').toLowerCase();

            if (tag === 'input' || tag === 'textarea' || tag === 'select') {
                return true;
            }
        }

        return document.querySelector('dialog[open]') !== null;
    }

    function patch(data) {
        var stats = data.stats || {};

        var nodes = document.querySelectorAll('[data-live]');

        for (var i = 0; i < nodes.length; i++) {
            var key = nodes[i].getAttribute('data-live');

            if (!(key in stats)) { continue; }

            var value = stats[key];

            if (typeof value === 'boolean') {
                nodes[i].textContent = value ? STRINGS.ok : STRINGS.bad;
                nodes[i].className = 'pill ' + (value ? 'ok' : 'bad');
            } else {
                nodes[i].textContent = value;
            }
        }
    }

    function poll() {
        fetch('live.php', {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' },
            cache: 'no-store'
        })
            .then(function (response) {
                if (!response.ok) { throw new Error('HTTP ' + response.status); }
                return response.json();
            })
            .then(function (data) {
                backoff = 0;
                failures = 0;
                patch(data);
            })
            .catch(function () {
                failures++;

                // Give up after a few failures rather than retrying forever on
                // a page nobody is looking at. The next manual reload restarts
                // it.
                if (failures >= 5) {
                    if (timer) { clearInterval(timer); timer = null; }
                    return;
                }

                backoff = Math.min(backoff + 1, 4);
            });
    }

    function start() {
        if (timer) { clearInterval(timer); }

        var delay = INTERVAL * Math.pow(2, backoff);

        timer = setInterval(function () {
            if (userIsBusy()) { return; }

            if (backoff > 0) {
                clearInterval(timer);
                start();
                return;
            }

            poll();
        }, delay);
    }

    poll();
    start();

    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            if (timer) { clearInterval(timer); timer = null; }
        } else {
            poll();
            start();
        }
    });
})();
</script>

<?php
// ---- 「关闭房间 / 强制关闭」的等待页 ------------------------------------
//
// 中继关房是有意慢的：它先把「ROOM REVOKED」通知给房间里的每个客户端，等
// 他们自己退出，超时的才强制断开 —— 中继内部最多花 25 秒。所以那个表单以前
// 会同步阻塞 30 秒：管理员看到的是一个完全没反应的页面，既不知道是在干活，
// 还是已经卡死了。
//
// 这里把它接过来：拦住带 js-room-close 的表单提交，弹出一个带秒表的对话框，
// 用 fetch 提交（32 秒后主动放弃，比中继自己多留 7 秒），并把中继的原话显示
// 出来。每一条失败路径都会给出原因 +「刷新页面」按钮，绝不会留下一个永远
// 转圈的页面。
//
// 没有 JS、或者浏览器太老（不支持 fetch / AbortController / <dialog>）时不
// 拦截：表单照旧同步提交，由 index.php 的 flash + 跳转负责 —— 那条路一直都在。
//
// WHY INLINE, NOT A SEPARATE rooms.js: 部署包（scripts/sync-deploy.py）只按
// 扩展名收 panel/web/assets/*.css，新增的 .js 不会进包，服务器上就会 404，
// 等待页会静默失效。内联在 footer 里的脚本跟着 .php 一起被复制，不会漏。
//
// 所有用户可见的句子都来自上面的 STRINGS（PHP 按当前语言生成）：
// 这个脚本本身不含任何写死的文案。
?>
<script>
(function () {
    'use strict';

    var LIMIT_SECONDS = 30;   // 对话框上写明的等待上限
    var TIMEOUT_MS = 32000;   // 比中继自己的 25 秒多留 7 秒
    var SLOW_MS = 26000;      // 到这一步就换一句提示，免得看着像卡死
    var BACK_TO_ROOMS = 'index.php?action=rooms';

    // 老浏览器：没有 fetch / AbortController / <dialog> 就不接管，
    // 表单会正常同步提交（原来的行为），至少不是坏的。
    if (typeof window.fetch !== 'function'
        || typeof AbortController !== 'function'
        || typeof document.createElement('dialog').showModal !== 'function') {
        return;
    }

    var overlay = null;
    var view = null;
    var ticker = null;
    var busy = false;

    // STRINGS 里的模板只有一个 %s 占位符，replace 一次就够。
    function fill(template, value) {
        return String(template).replace('%s', value);
    }

    function makeDialog() {
        var dialog = document.createElement('dialog');
        dialog.className = 'room-wait';

        var head = document.createElement('div');
        head.className = 'room-wait-head';

        var spinner = document.createElement('span');
        spinner.className = 'room-wait-spinner';

        var title = document.createElement('b');
        title.className = 'room-wait-title';

        head.appendChild(spinner);
        head.appendChild(title);

        var timer = document.createElement('div');
        timer.className = 'room-wait-timer';

        var text = document.createElement('div');
        text.className = 'room-wait-text';

        var hint = document.createElement('div');
        hint.className = 'room-wait-hint';

        var actions = document.createElement('div');
        actions.className = 'room-wait-actions';

        dialog.appendChild(head);
        dialog.appendChild(timer);
        dialog.appendChild(text);
        dialog.appendChild(hint);
        dialog.appendChild(actions);

        // Esc 是 <dialog> 的默认关闭方式。等待还没结束就让它关掉，管理员会
        // 以为"什么都没发生"，而房间其实正在关。所以忙的时候按住它。
        dialog.addEventListener('cancel', function (event) {
            if (busy) { event.preventDefault(); }
        });

        document.body.appendChild(dialog);

        overlay = dialog;
        view = {
            title: title,
            timer: timer,
            text: text,
            hint: hint,
            actions: actions,
            spinner: spinner
        };
    }

    function button(label, primary, handler) {
        var element = document.createElement('button');

        element.type = 'button';
        element.textContent = label;

        if (primary) {
            element.className = 'primary';
        }

        element.addEventListener('click', handler);

        return element;
    }

    function clearActions() {
        while (view.actions.firstChild) {
            view.actions.removeChild(view.actions.firstChild);
        }
    }

    function stopTicker() {
        if (ticker !== null) {
            clearInterval(ticker);
            ticker = null;
        }
    }

    function openWait(titleText, hintText) {
        if (overlay === null) {
            makeDialog();
        }

        busy = true;

        view.title.textContent = titleText;
        view.timer.textContent = fill(STRINGS.wait_elapsed, 0);
        view.hint.textContent = hintText;
        view.text.textContent = '';
        view.text.className = 'room-wait-text';
        view.spinner.style.display = '';
        clearActions();
        stopTicker();

        var started = Date.now();

        ticker = setInterval(function () {
            var elapsed = Date.now() - started;

            view.timer.textContent = fill(
                STRINGS.wait_elapsed, Math.floor(elapsed / 1000)
            );

            if (elapsed >= SLOW_MS) {
                view.hint.textContent = STRINGS.wait_slow;
            }
        }, 500);

        if (!overlay.open) {
            overlay.showModal();
        }
    }

    function closeWait() {
        if (overlay !== null && overlay.open) {
            overlay.close();
        }
    }

    function settle(titleText, message, ok) {
        stopTicker();

        busy = false;

        view.spinner.style.display = 'none';
        view.timer.textContent = '';
        view.hint.textContent = '';
        view.title.textContent = titleText;
        view.text.className = ok ? 'room-wait-text ok' : 'room-wait-text bad';
        view.text.textContent = message;

        clearActions();
    }

    function showDone(message) {
        settle(STRINGS.wait_done, message, true);

        view.actions.appendChild(button(STRINGS.back_to_rooms, true, function () {
            window.location.href = BACK_TO_ROOMS;
        }));
    }

    function showProblem(message) {
        settle(STRINGS.wait_problem, message, false);

        view.actions.appendChild(button(STRINGS.reload, true, function () {
            window.location.reload();
        }));

        view.actions.appendChild(button(STRINGS.close, false, closeWait));
    }

    function send(form) {
        var name = form.getAttribute('data-room-name') || '';
        var hint = form.getAttribute('data-room-hint')
            || fill(STRINGS.hint_default, LIMIT_SECONDS);

        openWait(
            name === ''
                ? STRINGS.wait_title
                : fill(STRINGS.wait_title_named, name),
            hint
        );

        var payload = new FormData(form);

        // 服务端认这个标记（index.php 里的 $wants_json）：有它才回 JSON。
        // 由脚本自己加，不写死在表单里 —— 本脚本没跑起来（老浏览器）时
        // 表单里就没有它，仍然走同步提交那条路。
        payload.set('ajax', '1');

        var controller = new AbortController();
        var gaveUp = false;
        var guard = setTimeout(function () {
            gaveUp = true;
            controller.abort();
        }, TIMEOUT_MS);

        fetch(form.action, {
            method: 'POST',
            body: payload,
            credentials: 'same-origin',
            cache: 'no-store',
            signal: controller.signal,
            headers: { 'Accept': 'application/json' }
        }).then(function (response) {
            return response.text().then(function (raw) {
                var reply = null;

                try {
                    reply = JSON.parse(raw);
                } catch (error) {
                    reply = null;
                }

                if (reply === null || typeof reply !== 'object') {
                    // 不是 JSON：多半是 CSRF 校验失败 / 权限不足（那两种回的是
                    // 纯文本），或者反代自己生成的错误页。把状态码说出来，
                    // 不要只丢一句"失败"。
                    throw new Error(fill(STRINGS.bad_reply, response.status));
                }

                return reply;
            });
        }).then(function (reply) {
            clearTimeout(guard);

            if (reply.ok) {
                showDone(reply.message || STRINGS.done_message);
            } else {
                showProblem(reply.error || STRINGS.unknown_problem);
            }
        }).catch(function (error) {
            clearTimeout(guard);

            if (gaveUp || (error && error.name === 'AbortError')) {
                showProblem(STRINGS.timeout);
                return;
            }

            if (error && error.name === 'TypeError') {
                showProblem(STRINGS.offline);
                return;
            }

            showProblem(
                error && error.message ? error.message : STRINGS.request_failed
            );
        });
    }

    function onSubmit(event) {
        // 表单上的 onsubmit="return confirm(...)" 是二次确认。用户点「取消」
        // 时浏览器只是 preventDefault，提交事件照样会传到这里 —— 所以必须先
        // 看 defaultPrevented，不然点了取消也会开始关房。
        if (event.defaultPrevented) {
            return;
        }

        event.preventDefault();

        send(event.currentTarget || event.target);
    }

    function attach() {
        var forms = document.querySelectorAll('form.js-room-close');

        for (var index = 0; index < forms.length; index++) {
            forms[index].addEventListener('submit', onSubmit);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', attach);
    } else {
        attach();
    }
})();
</script>

</body>
</html>
