"""
Static sanity check for the PHP panel.

There is no PHP runtime on the Windows dev machine, so this catches the
mistakes that a syntax check would otherwise catch, without running PHP:

  1. every .php file has balanced { } ( ) [ ] outside of strings/comments
  2. every .php file starts with <?php and has no stray closing tag
  3. views do not call undefined helper functions (checked against lib/*.php)
  4. every action referenced by a <form>/link exists in index.php's switch
  5. every action in index.php's switch has a corresponding handler
  6. required files referenced with require(__DIR__ . '/...') actually exist
  7. the CSS braces balance and every class used in the views exists in the CSS
  8. the language switch: t() falls back to Chinese, and every t('...') key
     used by the views exists in the English table
  9. the theme switch: both data-theme values exist in the CSS, the aurora
     background is pure CSS, and the default stays dark
 10. the waiting-dialog JavaScript contains no hardcoded Chinese
 11. avatar.png is used with a file_exists() fallback (never a broken image)

Usage:
    python tests/panel_static_test.py
"""

import os
import re
import sys
from urllib.parse import urljoin

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PANEL = os.path.join(ROOT, "panel")

PASSED = []
FAILED = []


def check(name, ok, detail=""):
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)
    (PASSED if ok else FAILED).append(name)


def php_files():
    found = []

    for base, _dirs, files in os.walk(PANEL):
        # skip build artefacts
        if any(part in base for part in (".git", "node_modules")):
            continue

        for name in files:
            if name.endswith(".php"):
                found.append(os.path.join(base, name))

    return sorted(found)


def parse_dockerfile_copies():
    """
    Parse `COPY <src> <dst>` lines from the panel Dockerfile.

    Returns {"web": "/var/www/panel", "lib": "/var/www/lib", ...}.
    """
    path = os.path.join(PANEL, "Dockerfile")

    if not os.path.isfile(path):
        return {}

    result = {}

    for line in open(path, encoding="utf-8").read().splitlines():
        line = line.strip()

        if not line.upper().startswith("COPY "):
            continue

        parts = line.split()

        if len(parts) < 3:
            continue

        source = parts[1]
        destination = parts[-1]

        result[source] = destination

    return result


def strip_php_blocks(source):
    """
    Keep only the HTML parts of a template.

    Class names produced by PHP expressions (ternaries, variables) are not
    static, so they are removed here to avoid false positives.
    """
    parts = re.split(r"<\?(?:php|=)?", source)
    html = [parts[0]]

    for chunk in parts[1:]:
        _php, _, after = chunk.partition("?>")
        html.append(after)

    return "\n".join(html)


def strip_php_strings_and_comments(source):
    """
    Replace the contents of strings and comments with spaces so bracket
    counting cannot be confused by a brace inside a message.
    """
    out = []
    i = 0
    length = len(source)

    while i < length:
        char = source[i]

        # line comment
        if source.startswith("//", i) or source.startswith("#", i):
            while i < length and source[i] != "\n":
                out.append(" ")
                i += 1
            continue

        # block comment
        if source.startswith("/*", i):
            while i < length and not source.startswith("*/", i):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue

        # single quoted string ('' and \' escapes)
        if char == "'":
            out.append("'")
            i += 1
            while i < length:
                if source[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if source[i] == "'":
                    out.append("'")
                    i += 1
                    break
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            continue

        # double quoted string
        if char == '"':
            out.append('"')
            i += 1
            while i < length:
                if source[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if source[i] == '"':
                    out.append('"')
                    i += 1
                    break
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            continue

        out.append(char)
        i += 1

    return "".join(out)


def check_balance(path, source):
    cleaned = strip_php_strings_and_comments(source)

    pairs = {"{": "}", "(": ")", "[": "]"}
    closing = {v: k for k, v in pairs.items()}

    stack = []

    for index, char in enumerate(cleaned):
        if char in pairs:
            stack.append(char)
        elif char in closing:
            if not stack or stack[-1] != closing[char]:
                line = cleaned[:index].count("\n") + 1
                return False, f"第 {line} 行出现多余的 {char}"

            stack.pop()

    if stack:
        return False, f"缺少闭合符号：{''.join(stack)}"

    return True, ""


def strip_code_comments(source):
    """
    Remove // and /* */ comments, leaving string literals untouched.

    PHP and JavaScript share the comment syntax, so one helper serves both:
    the language table scan (#8) must not see a t('...') that only exists in a
    comment, and the "no hardcoded Chinese in the dialog script" check (#10)
    must not trip over the (legitimately Chinese) comments in that script.
    """
    out = []
    index = 0
    length = len(source)
    quote = None

    while index < length:
        char = source[index]

        if quote is not None:
            out.append(char)

            if char == "\\":
                if index + 1 < length:
                    out.append(source[index + 1])
                    index += 2
                    continue
            elif char == quote:
                quote = None

            index += 1
            continue

        if char in "'\"":
            quote = char
            out.append(char)
            index += 1
            continue

        if source.startswith("//", index):
            while index < length and source[index] != "\n":
                index += 1
            continue

        if source.startswith("/*", index):
            index += 2
            while index < length and not source.startswith("*/", index):
                index += 1
            index += 2
            continue

        out.append(char)
        index += 1

    return "".join(out)


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 64)
    print("CraftLink（方块联机）PHP 面板静态检查")
    print("=" * 64)

    files = php_files()

    check("找到 PHP 文件", len(files) > 0, f"{len(files)} 个")

    if not files:
        return 1

    # ---- 1 + 2: structure ----
    fragment_files = set()

    for path in files:
        relative = os.path.relpath(path, ROOT)
        source = open(path, "r", encoding="utf-8").read()

        ok, detail = check_balance(path, source)
        check(f"括号平衡: {relative}", ok, detail)

        starts_with_tag = source.lstrip().startswith("<?php")

        if not starts_with_tag:
            # layout_footer.php is an HTML fragment that is only ever
            # required from inside a controller, so it legitimately starts
            # with markup.
            fragment_files.add(relative)

        check(
            f"以 <?php 开头（或是纯片段）: {relative}",
            starts_with_tag or "layout_footer.php" in relative,
        )

        # A file must not end with a closing tag followed by blank content
        # (that is a classic stray-whitespace bug). Templates that end with
        # "require __DIR__ . '/layout_footer.php'; ?>" close their PHP block
        # on purpose, so PHP blocks are stripped before the check.
        check(
            f"文件结尾没有多余的结束标签: {relative}",
            not strip_php_blocks(source).rstrip().endswith("?>"),
        )

    # ---- 1b: PHP/HTML 标签必须成对出现 ----
    #
    # 编辑模板时最容易犯的错：少写一个 ?>，后面的 HTML 就被当成 PHP 代码执行
    # （整页 500），或者多写一个 ?> 把一段 PHP 当文本吐出来。括号平衡检查抓
    # 不到这类错误，所以这里单独看一遍标签。文件以 PHP 模式结束是合法的
    # （不用补 ?>），所以只看"多余的结束标签"和"嵌套的开始标签"。
    php_tag = re.compile(r"<\?php|<\?=|\?>")
    tag_problems = []

    for path in files:
        relative = os.path.relpath(path, ROOT)
        text = open(path, "r", encoding="utf-8").read()

        inside = False

        for match in php_tag.finditer(text):
            token = match.group(0)
            line = text[:match.start()].count("\n") + 1

            if token == "?>":
                if not inside:
                    tag_problems.append(f"{relative}:{line} 多余的 ?>")
                inside = False
            else:
                if inside:
                    tag_problems.append(f"{relative}:{line} 嵌套的 {token}")
                inside = True

    check(
        "模板里的 <?php / <?= / ?> 标签没有错配",
        not tag_problems,
        "; ".join(tag_problems[:5]),
    )

    # ---- 3: helper functions used by views must exist ----
    #
    # EVERY file in panel/lib is scanned, not just common.php: the shared
    # helpers now live in two files (common.php and i18n.php), and a helper
    # defined in i18n.php is just as available to a view as one defined in
    # common.php.
    lib_dir = os.path.join(PANEL, "lib")
    common = open(os.path.join(lib_dir, "common.php"), encoding="utf-8").read()

    defined = set()

    for name in sorted(os.listdir(lib_dir)):
        if not name.endswith(".php"):
            continue

        text = open(os.path.join(lib_dir, name), encoding="utf-8").read()
        defined.update(
            re.findall(r"function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text)
        )

    # ---- 3a: NO function may be declared twice ----
    #
    # PHP fatals on a redeclaration:
    #
    #     Cannot redeclare is_root() (previously declared in common.php:466)
    #
    # and that is a WHOLE-PANEL 500, not a broken page -- which is exactly what
    # shipped once, because nothing here checked it and no PHP ran before the
    # upload. Every file is scanned, not just common.php: a view that declares
    # a helper that common.php already has is just as fatal.
    declarations = {}

    for path in files:
        relative = os.path.relpath(path, PANEL)
        text = open(path, encoding="utf-8").read()

        for function_name in re.findall(
            r"^\s*function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text, re.M
        ):
            declarations.setdefault(function_name, []).append(relative)

    duplicates = {
        name: places
        for name, places in declarations.items()
        if len(places) > 1
    }

    check(
        f"没有任何函数被重复定义（共 {len(declarations)} 个）",
        not duplicates,
        "重复定义会让整个面板 500："
        + "、".join(
            f"{name}（{' + '.join(places)}）"
            for name, places in sorted(duplicates.items())[:5]
        ),
    )
    php_builtins = {
        "isset", "empty", "count", "in_array", "sprintf", "intdiv", "round",
        "htmlspecialchars", "json_encode", "json_decode", "explode", "implode",
        "trim", "strlen", "substr", "array_key_exists", "is_array", "is_file",
        "number_format", "max", "min", "array_map", "array_filter",
        "rtrim", "ltrim", "str_replace", "preg_replace", "preg_match",
        "preg_match_all", "preg_split", "define", "defined", "func_get_args",
        "array_merge", "array_values", "array_keys", "array_slice", "sort",
        "usort", "str_pad", "str_repeat", "str_contains", "str_starts_with",
        "str_ends_with", "nl2br", "urlencode", "rawurlencode", "http_build_query",
        "time", "date", "strtotime", "gmdate", "microtime", "bin2hex",
        "random_bytes", "hash", "hash_hmac", "hash_equals", "hash_pbkdf2",
        "hex2bin", "base64_encode", "base64_decode", "session_start",
        "session_name", "session_destroy", "session_regenerate_id",
        "session_status", "session_set_cookie_params", "session_get_cookie_params",
        "setcookie", "header", "http_response_code", "set_exception_handler",
        "set_error_handler", "ini_get", "ini_set", "error_log", "file_put_contents",
        "file_get_contents", "fopen", "fclose", "fwrite", "is_dir", "mkdir",
        "unlink", "rename", "copy", "glob", "basename", "dirname", "realpath",
        "curl_init", "curl_setopt", "curl_setopt_array", "curl_exec",
        "curl_error", "curl_errno", "curl_getinfo", "curl_close",
        "is_readable", "is_writable", "is_dir", "is_file", "is_numeric",
        "getenv", "putenv", "array_replace_recursive", "array_merge_recursive",
        "var_export", "print_r", "trigger_error", "set_time_limit",
        "session_id", "session_write_close", "filter_var", "ctype_alnum",
        "ctype_digit", "array_column", "array_key_first", "array_diff",
        "array_unique", "array_reverse", "ksort", "asort", "floatval",
        "intval", "strval", "boolval", "gmstrftime", "idate",
        # added with the relay-reply-shape fix: relay_message() reads a message
        # out of whichever of the three relay reply shapes came back.
        "is_string", "is_bool", "is_int", "is_null", "is_object",
        # added with the three-tier roles.
        "strtolower", "in_array", "sprintf", "trim", "explode", "count",
        # added with no_cache_headers().
        "headers_sent", "header",
        # added with rgba_from_hex()/role_style() (the role colours are turned
        # into rgba() in PHP so the stylesheet needs no color-mix()).
        "hexdec", "dechex", "ctype_xdigit",
        # added with the "old relay" hint in setting_save.
        "strpos", "str_contains", "str_starts_with", "str_ends_with",
        # added with asset_url() (cache-busting the stylesheet).
        "filemtime",
        # added with panel_base_url()/enforce_trailing_slash() (子路径反代).
        "parse_url", "basename", "dirname", "headers_sent", "strpos",
        "preg_match", "preg_replace", "number_format", "round", "floor",
        "ceil", "max", "min", "abs", "array_values", "array_keys",
        "array_filter", "array_map", "array_merge", "array_search",
        "array_slice", "array_sum", "array_flip", "array_combine",
        "usort", "uasort", "ksort", "sort", "json_encode", "json_decode",
        "urlencode", "rawurlencode", "urldecode", "nl2br", "wordwrap",
        "strip_tags", "htmlspecialchars", "date_default_timezone_get",
        "ini_get", "ini_set", "error_log", "class_exists", "function_exists",
        "extension_loaded", "method_exists", "property_exists",
        # added with the language/theme switch: avatar_url() probes the image
        # with file_exists() so a missing avatar.png falls back to the letter
        # block instead of a broken-image icon.
        "file_exists", "setcookie",
    }

    call_pattern = re.compile(r"(?<![\w>$])([a-z_][a-z0-9_]*)\s*\(")

    unknown = {}

    for path in files:
        relative = os.path.relpath(path, PANEL)
        source = open(path, "r", encoding="utf-8").read()

        # Strip inline <script> and <style> blocks BEFORE looking for PHP
        # calls. The templates legitimately contain JavaScript (the layout
        # footer has the auto-refresh widget), and a JS function called as
        # `tick()` is not an undefined PHP function. Without this the checker
        # reports every JS identifier as a missing PHP helper.
        source = re.sub(
            r"<script\b.*?</script>", "", source, flags=re.S | re.I
        )
        source = re.sub(
            r"<style\b.*?</style>", "", source, flags=re.S | re.I
        )

        # Strip strings and comments first: error messages and Chinese text
        # contain words followed by parentheses and produced false positives.
        cleaned = strip_php_strings_and_comments(source)

        for name in call_pattern.findall(cleaned):
            if name in defined or name in php_builtins:
                continue

            # control keywords and language constructs
            if name in {
                "if", "for", "foreach", "while", "switch", "match", "function",
                "array", "list", "catch", "fn", "echo", "print", "return",
                "require", "require_once", "include", "include_once", "new",
                "use", "namespace", "class", "static", "elseif", "else",
                "declare", "strict_types", "int", "string", "bool", "float",
                "void", "self", "parent", "true", "false", "null", "and", "or",
                "instanceof", "throw", "try", "do", "default", "case", "break",
                "continue", "exit", "die", "unset", "global", "clone",
                "current_user", "is_admin", "require_login", "require_admin",
                "csrf_token", "check_csrf", "flash", "redirect", "h", "db",
                "relay_api", "config", "start_session", "human_bytes",
                "seconds_to_human", "json_out", "login_user", "verify_password",
                # browser side (inline JavaScript in the templates)
                "confirm", "alert", "document", "window", "return",
            }:
                continue

            unknown.setdefault(name, set()).add(relative)

    check(
        "没有调用未定义的函数",
        not unknown,
        "; ".join(f"{k} in {sorted(v)}" for k, v in list(unknown.items())[:5]),
    )

    # ---- 4 + 5: actions ----
    front = open(os.path.join(PANEL, "web", "index.php"), encoding="utf-8").read()

    switch_actions = set(
        re.findall(r"case\s+'([a-z_]+)'\s*:", front)
    )

    used_actions = set()

    for path in files:
        source = open(path, "r", encoding="utf-8").read()
        used_actions.update(
            re.findall(r"index\.php\?action=([a-z_]+)", source)
        )

    missing = used_actions - switch_actions

    check(
        "所有页面里引用的 action 都有处理分支",
        not missing,
        f"缺少: {sorted(missing)}" if missing else f"{len(switch_actions)} 个分支",
    )

    check(
        "index.php 定义了关键分支",
        {"login", "logout", "dashboard", "rooms", "users", "events",
         "room_delete", "session_kick"} <= switch_actions,
        str(sorted(switch_actions)),
    )

    # ---- 6: required files exist, resolved the way the CONTAINER does ----
    #
    # The Dockerfile maps:
    #     copy, destination
    #     "web"    -> /var/www/panel        (nginx root)
    #     "lib"    -> /var/www/lib          (outside the web root)
    #
    # So `require __DIR__ . '/../lib/common.php'` from a file that ends up in
    # /var/www/panel resolves to /var/www/lib/common.php -- i.e. it must exist
    # in the LOCAL `panel/lib`, not in `panel/web/lib`.
    #
    # An earlier Dockerfile only copied `web`, so lib/ never reached the
    # container and every page returned HTTP 500. These checks now mirror the
    # container layout to catch that class of mistake.
    copy_map = parse_dockerfile_copies()

    check(
        "Dockerfile 同时复制了 web 和 lib",
        "web" in copy_map and "lib" in copy_map,
        str(copy_map) if copy_map else "解析 Dockerfile 失败",
    )

    broken = []
    checked = 0

    # docker destination (container path) -> local source directory
    destination_sources = {
        os.path.abspath(destination): os.path.join(PANEL, source)
        for source, destination in copy_map.items()
        if os.path.isdir(os.path.join(PANEL, source))
    }

    def container_dir(local_dir):
        """Map a local directory to its directory inside the container."""
        local_dir = os.path.abspath(local_dir)

        for destination, source_root in destination_sources.items():
            source_root = os.path.abspath(source_root)

            if local_dir == source_root:
                return destination

            if local_dir.startswith(source_root + os.sep):
                relative = os.path.relpath(local_dir, source_root)
                return os.path.join(destination, relative)

        return None

    for path in files:
        source = open(path, "r", encoding="utf-8").read()

        here = container_dir(os.path.dirname(path))

        if here is None:
            continue

        for target in re.findall(r"require(?:_once)?\s+([^;]+);", source):
            match = re.search(r"'([^']+)'", target)

            if not match:
                continue

            relative_target = match.group(1)

            if not relative_target.startswith("/"):
                continue

            # Resolve inside the container, then map back to the local file
            # that will be copied to that path.
            container_path = os.path.normpath(
                os.path.join(here, relative_target.lstrip("/"))
            )

            checked += 1
            found = False

            for destination, source_root in destination_sources.items():
                if container_path == destination or container_path.startswith(
                    destination + os.sep
                ):
                    relative = os.path.relpath(container_path, destination)
                    local = os.path.normpath(
                        os.path.join(source_root, relative)
                    )

                    if os.path.isfile(local):
                        found = True

                    break

            if not found:
                broken.append(
                    f"{os.path.relpath(path, PANEL)} -> {relative_target} "
                    f"(容器内 {container_path} 不存在)"
                )

    check(
        "require 引用的文件在容器布局下都存在",
        not broken,
        "; ".join(broken),
    )

    check(
        "确实检查到了 require 语句",
        checked >= 5,
        f"检查了 {checked} 条",
    )

    # ---- 7: CSS ----
    css_path = os.path.join(PANEL, "web", "assets", "style.css")
    css = open(css_path, encoding="utf-8").read()

    check(
        "CSS 括号平衡",
        css.count("{") == css.count("}"),
        f"{css.count('{')} / {css.count('}')}",
    )

    css_classes = set(re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", css))

    view_classes = set()

    for path in files:
        source = open(path, "r", encoding="utf-8").read()

        for value in re.findall(r'class="([^"]*)"', strip_php_blocks(source)):
            view_classes.update(token for token in value.split() if token)

    missing_classes = {
        name for name in view_classes
        if name not in css_classes
    }

    check(
        "视图中使用的 class 都在 CSS 里定义",
        not missing_classes,
        f"缺少: {sorted(missing_classes)}" if missing_classes else
        f"{len(view_classes)} 个类",
    )

    # ---- extra: config sample shape ----
    sample = open(
        os.path.join(PANEL, "config.sample.php"), encoding="utf-8"
    ).read()

    for key in ("db", "relay", "panel", "base_url", "token", "session_secret"):
        check(f"config.sample.php 含 {key}", key in sample)

    check(
        "config.php 没有被提交（应只有 sample）",
        not os.path.isfile(os.path.join(PANEL, "config.php")),
    )

    # ---- config.php mount point must match common.php's expectation ----
    compose = open(
        os.path.join(PANEL, "docker-compose.yml"), encoding="utf-8"
    ).read()

    check(
        "compose 把 config.php 挂载到 /var/www/lib/config.php",
        "/var/www/lib/config.php" in compose,
        [line.strip() for line in compose.splitlines()
         if "config.php" in line and ":" in line][:2],
    )

    # ---- config.php lookup: must NOT use a ".." path ----
    #
    # `__DIR__ . '/../config.php'` resolves fine under php-cli but FAILS under
    # php-fpm (realpath() returns false), because /var/www is an overlayfs
    # mount point. That produced a "works from the shell, 500 in the browser"
    # bug. The lookup must normalise the directory with realpath() instead.
    common_src = open(
        os.path.join(PANEL, "lib", "common.php"), encoding="utf-8"
    ).read()

    # Strip comments first (both // and /* */): the explanatory comments
    # legitimately quote the old broken pattern.
    common_code = re.sub(r"//[^\n]*", "", common_src)
    common_code = re.sub(r"/\*.*?\*/", "", common_code, flags=re.S)

    # The invariant that actually matters: a config path must never contain a
    # raw ".." segment. Under php-fpm realpath() of such a path returns false
    # (overlayfs mount point), which produced a "works in the shell, 500 in the
    # browser" bug. Every candidate must therefore be built from a realpath()'d
    # directory.
    raw_dotdot = [
        line.strip() for line in common_code.splitlines()
        if "config.php" in line and ".." in line
    ]

    check(
        "config 候选路径里没有未规范化的 .. 段",
        not raw_dotdot,
        "; ".join(raw_dotdot) if raw_dotdot else "OK",
    )

    check(
        "候选目录都经过 realpath() 规范化",
        "realpath($here)" in common_code
        or "realpath(__DIR__)" in common_code,
    )

    # ---- config.php is OPTIONAL now: credentials come from compose env ----
    check(
        "config.php 缺失时不再报错退出（改为内置默认值 + 环境变量）",
        "config.php is OPTIONAL" in common_src
        and "$skipFileLoad" in common_src,
    )

    check(
        "支持从环境变量读取数据库密码与面板 Token",
        "MCLINK_DB_PASSWORD" in common_src
        and "MCLINK_ADMIN_API_TOKEN" in common_src,
    )

    compose_full = open(
        os.path.join(PANEL, "docker-compose.yml"), encoding="utf-8"
    ).read()

    check(
        "面板 compose 把凭据放进 environment（config.php 被覆盖也不断服务）",
        "MCLINK_DB_PASSWORD" in compose_full
        and "MCLINK_ADMIN_API_TOKEN" in compose_full,
    )

    # ---- permissions: the container runs php-fpm as nobody(65534) ----
    check(
        "文档里给出了 config.php 的权限修法（chown 65534 + chmod 640）",
        "65534" in compose_full and "chmod 640" in compose_full,
    )

    # ---- relay reply shape: never read a key the relay does not send ----
    #
    # The relay answers in three different shapes:
    #   GET  /settings              -> {"ok":true,"data":{...}}
    #   PUT  /settings              -> {"ok":true,"message":"...","data":{...}}
    #   POST /users/<u>/rename      -> {"ok":true,"message":"...","data":{...}}
    #   POST /rooms/purge-stale     -> {"ok":true,"message":"..."}
    #
    # Reading the wrong one silently degrades to a generic fallback string, so
    # a working operation looks like it "lost the detail". That bug shipped
    # once (all three call sites were wrong at the same time), hence this test.
    index_src = open(
        os.path.join(PANEL, "web", "index.php"), encoding="utf-8"
    ).read()

    rooms_src = open(
        os.path.join(PANEL, "web", "views", "rooms.php"), encoding="utf-8"
    ).read()

    users_src = open(
        os.path.join(PANEL, "web", "views", "users.php"), encoding="utf-8"
    ).read()

    check(
        "relay_message() 助手存在（统一从中继回包里取 message）",
        "function relay_message(" in common_src,
    )

    check(
        "relay_message() 覆盖 raw / raw.data / data / 顶层 message 四种位置",
        common_src.count("$result['raw']['message']") >= 1
        and common_src.count("$result['raw']['data']['message']") >= 1
        and common_src.count("$result['data']['message']") >= 1
        and common_src.count("$result['message']") >= 1,
    )

    # The three call sites that were reading a key the relay never sends.
    check(
        "改名 / 保存开关 / 清理残留 都改用 relay_message() 取提示",
        index_src.count("relay_message($result)") >= 3,
        f"出现 {index_src.count('relay_message($result)')} 次",
    )

    check(
        "没有代码再去读中继不存在的 $result['data']['message']",
        "$result['data']['message']" not in index_src,
    )

    check(
        "没有代码再去读中继不存在的 $result['message']",
        "$result['message']" not in index_src,
    )

    # /health has no "data" layer either: it must be read from raw.
    header_src = open(
        os.path.join(PANEL, "web", "views", "layout_header.php"),
        encoding="utf-8",
    ).read()

    check(
        "/health 的字段从 raw 里读（它没有 data 层）",
        "$header_health['raw']['database']" in header_src
        and "'data']['database'" not in header_src,
    )

    # ---- dead code after redirect() ----
    #
    # The login branch used to have a second, unreachable copy of the session
    # setup AFTER `redirect(...)`. It was harmless only because redirect()
    # calls exit(); the moment anyone made redirect() non-fatal it would have
    # overwritten the read-only login flash with the "welcome back, admin" one.
    login_block = index_src.split("case 'logout':")[0]

    # NOTE: the needle is pre-bound because an f-string expression may not
    # contain a backslash before Python 3.12.
    session_write_needle = "$_SESSION['user'] = ["

    check(
        "登录分支里没有 redirect() 之后的死代码（重复的 session 写入）",
        login_block.count("session_regenerate_id") == 1
        and login_block.count(session_write_needle) == 1,
        f"regenerate={login_block.count('session_regenerate_id')} "
        f"session_write={login_block.count(session_write_needle)}",
    )

    check(
        "登录分支在 redirect() 之前就把只读提示写好了",
        login_block.count("redirect('index.php?action=dashboard')") == 1,
    )

    # ---- the live_effect text the relay sends must actually be shown ----
    dashboard_src = open(
        os.path.join(PANEL, "web", "views", "dashboard.php"), encoding="utf-8"
    ).read()

    check(
        "仪表盘会把开关的 live_effect 文案显示出来",
        "live_effect" in dashboard_src,
    )

    # ---- no stale "admins only" text ----
    #
    # The login rule was reversed (all accounts may log in read-only) but two
    # paragraphs still claimed player accounts cannot enter the panel at all.
    # Text that contradicts the enforcement is worse than no text: it makes an
    # admin believe a player is locked out when they are actually reading
    # everything.
    stale_claims = []

    for path in files:
        relative = os.path.relpath(path, PANEL)
        text = open(path, encoding="utf-8").read()

        for phrase in ("只有<b>管理员</b>账号能登录本面板",
                       "玩家账号即使密码正确也无法进入"):
            if phrase in text:
                stale_claims.append(f"{relative}: {phrase}")

    check(
        "没有「只有管理员能登录面板」这类过时说法",
        not stale_claims,
        "; ".join(stale_claims) if stale_claims else "OK",
    )

    check(
        "账号页说明了普通账号可以只读登录",
        "所有账号都能登录这个面板" in open(
            os.path.join(PANEL, "web", "views", "users.php"),
            encoding="utf-8",
        ).read(),
    )

    # ---- room revocation ----
    #
    # The client's own delete refuses a room that still has players, which is a
    # safety rail for players. The ADMIN path must be unconditional: an admin
    # closing a room means closing it. Sending the panel at the client path
    # would make the button fail exactly when it is needed.
    check(
        "房间页的强制关闭走 room_revoke（不是客户端的删除路径）",
        'action="index.php?action=room_revoke"' in rooms_src,
        "仍然指向 room_delete",
    )

    check(
        "强制关闭有二次确认，并说明会踢掉多少人",
        "无法撤销" in rooms_src and "名玩家" in rooms_src,
    )

    check(
        "index.php 处理 room_revoke",
        "case 'room_revoke':" in index_src,
    )

    check(
        "room_revoke 调用中继的 /revoke 接口",
        "/revoke" in index_src,
    )

    # ---- 踢出单个房间成员（不是踢游戏会话，也不是删房）----
    #
    # 房间详情里要有一张成员表 + 每人的「踢出」按钮；三种身份的二次确认文案
    # 不能少（房主被踢 = 解散，主机被踢 = 当前所有玩家的游戏连接会断）。
    # 控制器走 `POST /rooms/<room>/kick`，body 里带 client_id。
    check(
        "房间页有「踢出成员」表单（room_member_kick）且提交 client_id",
        'action="index.php?action=room_member_kick"' in rooms_src
        and 'name="client_id"' in rooms_src,
    )

    check(
        "成员表数据来自中继快照的 member_list（老中继没有该键时不炸）",
        "$room['member_list'] ?? []" in rooms_src,
    )

    check(
        "踢房主的确认框说明会解散房间",
        "当前命中房主，踢出后房间会解散" in rooms_src,
    )

    check(
        "踢 host 的确认框说明会断开所有玩家的游戏连接",
        "当前命中玩家为 host，踢出后会断开当前所有玩家的游戏连接" in rooms_src,
    )

    check(
        "index.php 处理 room_member_kick 并调用 /kick 接口",
        "case 'room_member_kick':" in index_src
        and "/kick" in index_src
        and "['client_id' => $client]" in index_src,
    )

    check(
        "踢人动作同样过 require_operator()（权限组和关房一致）",
        "case 'room_member_kick':" in index_src
        and index_src.split("case 'room_member_kick':", 1)[1]
            .split("case ", 1)[0]
            .count("require_operator();") == 1,
    )

    # A hung relay must not leave the panel spinning forever.
    relay_src = open(
        os.path.join(os.path.dirname(PANEL), "server", "admin_api.py"),
        encoding="utf-8",
    ).read()

    check(
        "吊销接口有超时保护（否则面板会被硬控）",
        "wait_for" in relay_src and "revoke_room_by_name" in relay_src,
    )

    relay_members = open(
        os.path.join(os.path.dirname(PANEL), "server", "room_members.py"),
        encoding="utf-8",
    ).read()

    check(
        "吊销会先通知客户端再断开（否则客户端会无限重连）",
        "ROOM REVOKED" in relay_members
        and relay_members.index("ROOM REVOKED")
        < relay_members.index("async def revoke_room(") + 4000,
    )

    # ---- 关房等待页：点「强制关闭」不再卡住 30 秒 ----
    #
    # 中继关房是有意慢的：先把「房间已关闭」通知给房间里的每个客户端，等他
    # 们自己退出，超时的才强制断开（中继内部最多 25 秒）。所以同步提交的后果
    # 是管理员对着一个没反应的页面等 30 秒，既不知道是在干活还是已经卡死。
    #
    # 现在的链路是：表单带 js-room-close -> layout_footer.php 里的脚本拦住
    # 提交，fetch 时自己加上 ajax=1 -> index.php 见到 ajax=1 就回 JSON ->
    # 页面把中继的原话显示出来。下面这些检查守的就是每一环 —— 少了任何一环，
    # 管理员又会回到"点了没反应"的状态，而且看不出问题出在哪。
    footer_src = open(
        os.path.join(PANEL, "web", "views", "layout_footer.php"),
        encoding="utf-8",
    ).read()

    check(
        "关房表单带 js-room-close 钩子（房间页和仪表盘都要有）",
        "js-room-close" in rooms_src and "js-room-close" in dashboard_src,
    )

    check(
        "index.php 只在带 ajax=1 时回 JSON（其它情况不变）",
        "$wants_json" in index_src and "$post['ajax']" in index_src,
    )

    check(
        "关房结果用 json_out() 回给页面（ok / message / error）",
        "json_out([" in index_src
        and "'message'" in index_src
        and "'error'" in index_src,
    )

    check(
        "页面读的字段和 index.php 回的字段一致",
        "reply.ok" in footer_src
        and "reply.message" in footer_src
        and "reply.error" in footer_src,
    )

    check(
        "房间标识走 body（POST /rooms/revoke），不拼进 URL 路径",
        "'/rooms/revoke'" in index_src and "'room' => $room" in index_src,
    )

    check(
        "等待页有 32 秒兜底超时，比中继自己的 25 秒长",
        "32000" in footer_src
        and "AbortController" in footer_src
        and "abort()" in footer_src,
    )

    check(
        "超时/失败都告诉管理员刷新页面（绝不留下永远转圈的页面）",
        "等待超时" in footer_src and "刷新页面" in footer_src,
    )

    check(
        "没有 JS 时仍然走 flash + 跳转（功能不依赖脚本）",
        index_src.count("redirect('index.php?action=rooms')") >= 3,
    )

    check(
        "房间页显示房间名（name），表单里提交的仍是房间 ID（room）",
        "$room_name" in rooms_src
        and 'name="room" value="<?= h($room_id) ?>"' in rooms_src,
    )

    check(
        "仪表盘同样显示房间名、提交房间 ID",
        "$room_name" in dashboard_src
        and 'name="room" value="<?= h($room_id) ?>"' in dashboard_src,
    )

    check(
        "CSS 里有等待对话框的样式和 js-room-close 钩子",
        "js-room-close" in css
        and "dialog.room-wait" in css
        and "room-wait-spinner" in css,
    )

    # ---- three tier roles ----
    check(
        "面板定义了三级角色常量",
        "const ROLE_ROOT" in common_src
        and "const ROLE_ADMIN" in common_src
        and "const ROLE_PLAYER" in common_src,
    )

    check(
        "角色颜色与客户端一致（root 橙 / admin 绿 / player 蓝）",
        "#ff9f43" in common_src
        and "#3ddc84" in common_src
        and "#4c8dff" in common_src,
    )

    check(
        "根管理员不能被任何人修改（包括它自己）",
        "function can_act_on" in common_src
        and "Nobody touches root through the panel, including root."
        in common_src,
    )

    check(
        "后端逐个动作校验目标账号的角色（不只是隐藏按钮）",
        index_src.count("$guard_target($username);") >= 5,
        f"只找到 {index_src.count('$guard_target($username);')} 处",
    )

    check(
        "创建账号只能选管理员或玩家（不能造出第二个根管理员）",
        'name="role" value="player"' in users_src
        and 'name="role" value="admin"' in users_src
        and 'value="root"' not in users_src,
    )

    # ============================================================
    # 8. 语言切换（中文 / English）
    # ============================================================
    #
    # 面板以前只有中文。现在每一条用户可见的字符串都过 t()，而 t() 有一条
    # 铁律：查不到就返回中文原文。这套东西能安全上线全靠那条铁律 ——
    # 漏翻一句最多是"这句还是中文"，绝不会变成空白（那才会让人以为面板坏了）。
    i18n_path = os.path.join(PANEL, "lib", "i18n.php")
    i18n_src = (
        open(i18n_path, encoding="utf-8").read()
        if os.path.isfile(i18n_path) else ""
    )

    login_src = open(
        os.path.join(PANEL, "web", "login.php"), encoding="utf-8"
    ).read()

    prefs_src = open(
        os.path.join(PANEL, "web", "views", "prefs.php"), encoding="utf-8"
    ).read()

    account_src = open(
        os.path.join(PANEL, "web", "views", "account.php"), encoding="utf-8"
    ).read()

    logout_src = open(
        os.path.join(PANEL, "web", "logout.php"), encoding="utf-8"
    ).read()

    check("lib/i18n.php 存在并定义了 t()", i18n_src != "" and "function t(" in i18n_src)

    check(
        "t() 查不到键时回落到中文原文（绝不会渲染空白）",
        "$table[$original] ?? $original" in i18n_src
        and "if ($original === '' || panel_lang() === PANEL_LANG_ZH)" in i18n_src,
    )

    check(
        "默认仍然是中文 + 暗色（不点切换条时和以前完全一样）",
        "const PANEL_LANG_ZH = 'zh'" in i18n_src
        and "const PANEL_THEME_DARK  = 'dark'" in i18n_src
        and "?? PANEL_LANG_ZH" in i18n_src
        and "?? PANEL_THEME_DARK" in i18n_src,
    )

    check(
        "语言 / 主题存在 cookie 里（panel_lang / panel_theme）",
        "'panel_lang'" in i18n_src
        and "'panel_theme'" in i18n_src
        and "setcookie($name, $value" in i18n_src,
    )

    check(
        "index.php 和 login.php 都在输出任何东西之前应用语言 / 主题",
        "panel_apply_preferences();" in index_src
        and "panel_apply_preferences();" in login_src,
    )

    check(
        "顶部切换条里有中文 / English 两个选项，且是普通链接（不需要 JS）",
        "panel_pref_url('lang', PANEL_LANG_ZH" in prefs_src
        and "panel_pref_url('lang', PANEL_LANG_EN" in prefs_src
        and ">中文</a>" in prefs_src
        and ">English</a>" in prefs_src,
    )

    check(
        "布局头部引入了切换条（登录页也有）",
        "require __DIR__ . '/prefs.php';" in header_src
        and "require __DIR__ . '/views/prefs.php';" in login_src,
    )

    # ---- t() 的每一条键都要在英文表里 ----
    #
    # 这一条是"翻译完整性"的唯一保证：漏一条不会报错（t() 会安静地回中文），
    # 所以只能在这里把用到的键和表里的键对一遍。
    table_body = ""

    if "$table = [" in i18n_src:
        table_body = i18n_src.split("$table = [", 1)[1].split("\n    ];", 1)[0]

    entry_pattern = re.compile(
        r"^\s*'((?:[^'\\]|\\.)*)'\s*=>\s*'((?:[^'\\]|\\.)*)',\s*$", re.M
    )
    english_entries = [(m.group(1), m.group(2)) for m in entry_pattern.finditer(table_body)]
    english_keys = {key for key, _value in english_entries}

    check(
        "英文翻译表能被解析（每条都是 '原文' => 'English',）",
        len(english_keys) >= 300,
        f"{len(english_keys)} 条",
    )

    # 占位符数量必须一一对应：sprintf() 少一个参数在 PHP 8 会抛 ArgumentCountError
    # （那就是整页 500），多一个则英文句子里会缺值。
    placeholder_mismatch = [
        key for key, value in english_entries
        if len(re.findall(r"%[sd]", key)) != len(re.findall(r"%[sd]", value))
    ]

    check(
        "英文里的 %s / %d 占位符数量和中文原文一致",
        not placeholder_mismatch,
        "; ".join(placeholder_mismatch[:5]),
    )

    # 英文值里不能有单引号或反斜杠：有的译文会塞进 confirm('...') 这样的
    # 单引号 JS 字面量里（HTML 属性解码之后就是 JS 源码），一个撇号足以把
    # 字符串截断 —— 点了确认等于没点。
    risky = [
        key for key, value in english_entries
        if "'" in value or "\\" in value or "\n" in value
    ]

    check(
        "英文里没有单引号 / 反斜杠 / 换行（不会破坏 confirm() 里的 JS 字符串）",
        not risky,
        "; ".join(risky[:5]),
    )

    t_call_pattern = re.compile(
        r"(?<![\w$>])t\('((?:[^'\\]|\\.)*)'(?:\s*,\s*'([a-z_]*)')?\)"
    )

    used_keys = set()

    for path in files:
        if path.endswith("i18n.php"):
            continue

        text = strip_code_comments(open(path, encoding="utf-8").read())

        for match in t_call_pattern.finditer(text):
            used_keys.add(
                (match.group(2) + "|" + match.group(1))
                if match.group(2) else match.group(1)
            )

    untranslated = sorted(used_keys - english_keys)

    check(
        f"视图里用到的每一条 t() 都在英文表里（共 {len(used_keys)} 条）",
        not untranslated,
        "; ".join(untranslated[:5]),
    )

    # NOTE: the needle is pre-bound because an f-string expression may not
    # contain a backslash or a nested quote before Python 3.12.
    t_needle = "t('"

    check(
        "t() 覆盖了控制器的提示消息（不只是模板里的字）",
        index_src.count(t_needle) >= 25 and common_src.count(t_needle) >= 12,
        f"index.php {index_src.count(t_needle)} 处 / "
        f"common.php {common_src.count(t_needle)} 处",
    )

    # ---- 中文默认下页面上的字样不能被换掉 ----
    #
    # 页名（$page_title）用的是原始中文：导航条的 active 判断拿它做比较，
    # 翻译只负责显示。这里守住这个分工，免得以后有人把 t() 套到比较上。
    check(
        "页名仍然是原始中文（active 判断不受语言影响）",
        "$page_title = '仪表盘';" in dashboard_src
        and "$title === '仪表盘'" in header_src,
    )

    # ---- 英文界面下不能漏出中文（不是 t() 键的中文字面量） ----
    #
    # WHY THIS CHECK EXISTS: t() 查不到键时会安静地回中文原文 —— 那是刻意的
    # 兜底（见上面的说明），代价是"漏翻一句"永远不会报错。上一轮就留下了几处
    # 这种漏网：页脚写着旧产品名，登录页的提示语没进翻译表。切到 English 才
    # 看得出来，即使用户报了也只知道"某处还有中文"。
    #
    # 这里用一个保守的分词器回答一个很窄的问题：**这个中文字面量是不是
    # 直接吐给浏览器的？** 只有"是"才算问题。
    #
    # 判据是字面量前面紧挨着的是不是 t(（允许 h(t(）—— t() 的键就是中文，
    # 所以"在 t() 里"等于"已经进了翻译表"，而那件事上面那条检查已经管了。
    #
    # 刻意不看的四类（它们不是面板的界面文案）：
    #   * 注释（PHP / HTML 都是）—— 不会出现在页面上
    #   * 中继自己的错误文案（例如 $result['error'] 里的 '未知的设置项'，
    #     它是**比对用的针**，翻译了反而匹配不上）
    #   * <code> / <pre> 里的命令路径
    #   * <script> / <style> 的正文：那里的中文只有注释，真正的文案由 PHP
    #     通过 STRINGS 传进去（见检查 #10）
    #
    # 已知盲点（写在报告里，别当成"检查过了=没问题"）：
    #   * 中文字面量在 PHP 里拼一半、HTML 里补一半（$room_js 那种）看不出来
    #   * 从变量里来的中文（$relay_message、$result['error']）本来就是数据
    #   * JS 里用 PHP 写死的中文（STRINGS 之外新加的）只看得住注释那一层，
    #     真正的守卫是检查 #10
    #   * 页名 $page_title / $title 会比较、会做 active 判断，是刻意保留的中文
    #   * 语言切换条那个「中文」标签按设计就是中文
    cjk_pattern = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")
    php_open = re.compile(r"<\?(?:php|=)?")
    attribute_pattern = re.compile(r"""=\s*("[^"]*"|'[^']*')""")
    t_key_prefix = re.compile(r"(?<![\w$>])t\(\s*$")

    # 输出里允许出现的中文字面量：页名要拿去做 active 比较，语言切换条的
    # 「中文」按设计永远是中文，站名默认值后面会过 t()。
    # 备案号**不在**这里：M1/D5 之后它来自「面板设置」，代码里不许有默认值。
    allowed_chinese = {
        "CraftLink 管理面板",
        "仪表盘", "房间", "账号", "操作日志", "我的账号", "中文",
    }

    # 这一行是配置默认值，不是页面文案（它后面会走 t($panel_title)）；
    # 中继自己的报错原文是**比对用的针**（strpos 那个 needle），翻译了反而
    # 匹配不上。注释里已经在 t() 的说明旁边写过这件事。
    allowed_prefixes = (
        "$page_title = '",
        "'title' => '",
    )
    allowed_substrings = (
        "strpos($result['error'],",
    )

    def skip_quoted(body, index):
        """消费一个 PHP 字符串字面量: (正文, 新下标, 换行数)."""
        quote = body[index]
        cursor = index + 1
        text = []
        newlines = 0

        while cursor < len(body):
            char = body[cursor]

            if char == "\\":
                text.append(body[cursor:cursor + 2])
                cursor += 2
                continue

            if char == quote:
                cursor += 1
                break

            if char == "\n":
                newlines += 1

            text.append(char)
            cursor += 1

        return "".join(text), cursor, newlines

    def php_literals(body, base_line):
        """[(正文, 行号)]：不是 t() 键的 PHP 字符串字面量."""
        found = []
        index = 0
        size = len(body)
        line = base_line

        while index < size:
            char = body[index]

            if char == "\n":
                line += 1
                index += 1
                continue

            if body.startswith("//", index) or char == "#":
                while index < size and body[index] != "\n":
                    index += 1
                continue

            if body.startswith("/*", index):
                end = body.find("*/", index + 2)
                end = size if end == -1 else end + 2
                line += body.count("\n", index, end)
                index = end
                continue

            if char in "'\"":
                start_line = line
                text, next_index, newlines = skip_quoted(body, index)

                if t_key_prefix.search(body[max(0, index - 2000):index]) is None:
                    found.append((text, start_line))

                line += newlines
                index = next_index
                continue

            index += 1

        return found

    def chinese_in_output(source):
        """[(正文, 行号)]：会渲染到页面上的、不是 t() 键的中文字面量."""
        found = []

        def flush_text():
            """标签之间的裸文本也算输出（<h2>中文</h2> 这种最容易漏）。"""
            if text_buffer[0] != "":
                found.append((text_buffer[0], text_buffer[1]))
                text_buffer[0] = ""

        text_buffer = ["", 1]
        # 标签内部（<a href="..."> 里）的字符不算正文；< > 之间的才是。
        in_tag = [False]
        # 刚看完一个 PHP 块（<?= ... ?>），后面可能还跟着关标签的符号
        after_php = [False]
        index = 0
        size = len(source)
        line = 1

        while index < size:
            char = source[index]

            match = php_open.match(source, index)

            if match is not None:
                flush_text()
                start = match.end()
                end = source.find("?>", start)
                end = size if end == -1 else end

                found.extend(php_literals(source[start:end], line))
                line += source.count("\n", start, end)
                index = end + (2 if end < size else 0)

                # <a href="<?= ... ?>"> 里的 PHP 块收尾之后，剩下的那截
                # 引号 / 尖括号是在关外层标签，不是正文。少看这一眼，报出来的
                # 就是 '">中文' 这种带符号的片段。
                after_php[0] = True
                continue

            if source.startswith("<!--", index):
                flush_text()
                end = source.find("-->", index + 4)
                end = size if end == -1 else end + 3
                line += source.count("\n", index, end)
                index = end
                continue

            if char == "<":
                flush_text()

                if re.match(r"<\s*(script|style)\b", source[index:], re.I):
                    end = re.search(
                        r"</\s*(script|style)\s*>", source[index:], re.I
                    )

                    if end is None:
                        break

                    stop = index + end.end()
                    line += source.count("\n", index, stop)
                    index = stop
                    continue

                tag_end = source.find(">", index)

                if tag_end == -1:
                    break

                tag = source[index:tag_end]

                for match in attribute_pattern.finditer(tag):
                    raw = match.group(1)[1:-1]
                    found.append(
                        (re.sub(r"<\?(?:php|=)?.*?\?>", " ", raw, flags=re.S), line)
                    )

                line += tag.count("\n")
                index = tag_end + 1
                in_tag[0] = False
                continue

            if char == "\n":
                flush_text()
                after_php[0] = False
                text_buffer[1] = line + 1
                line += 1
                index += 1
                continue

            if after_php[0]:
                after_php[0] = False

                # 关标签的那点符号：跳过它，别让它混进正文
                if char == '"':
                    index += 1
                    continue

            # 标签的收尾 ">"（例如 <a ...>中文</a> 里那个）不算正文，
            # 否则报出来的会是 ">中文" 这种带符号的片段。正文里的 ">"
            # 本来就该写成 &gt;，所以这里直接跳过不会有漏报。
            if char == ">":
                in_tag[0] = False
                index += 1
                continue

            if char == "<":
                in_tag[0] = True

            if text_buffer[0] == "":
                text_buffer[1] = line

            text_buffer[0] += char
            index += 1

        flush_text()

        return found

    chinese_leaks = []

    for path in files:
        relative = os.path.relpath(path, PANEL).replace(os.sep, "/")

        # i18n.php **就是**那张中文表，天经地义全是中文。
        if relative.endswith("i18n.php"):
            continue

        raw = open(path, encoding="utf-8").read()

        # <code> / <pre> 里是命令、路径、报文样例，全是字面量，不翻译。
        raw = re.sub(r"<(code|pre)\b[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)

        text_lines = raw.split("\n")

        for literal, line in chinese_in_output(raw):
            stripped = literal.strip()

            if not cjk_pattern.search(stripped) or stripped in allowed_chinese:
                continue

            source_line = text_lines[line - 1].strip() if line <= len(text_lines) else ""

            if source_line.startswith(allowed_prefixes):
                continue

            if any(needle in source_line for needle in allowed_substrings):
                continue

            chinese_leaks.append(f"{relative}:{line} 中文没进 t(): {stripped[:40]}")

    check(
        f"视图里没有漏出 t() 的中文字面量（允许 {len(allowed_chinese)} 条登记项）",
        not chinese_leaks,
        "; ".join(chinese_leaks[:6])
        if chinese_leaks
        else "没有发现直接渲染的中文",
    )

    check(
        "中文检测真的在扫视图（不是空转）",
        len(files) >= 5 and len(php_open.findall(dashboard_src)) > 0,
        f"扫了 {len(files)} 个 PHP 文件",
    )

    # ============================================================
    # 9. 主题切换（亮色 / 暗色）—— 暗色必须保持原样
    # ============================================================

    check(
        "<html> 上带 data-theme（布局和登录页都要有）",
        'data-theme="<?= h(panel_theme()) ?>"' in header_src
        and 'data-theme="<?= h(panel_theme()) ?>"' in login_src,
    )

    check(
        "CSS 同时处理 data-theme 的两个值",
        'html[data-theme="light"]' in css
        and 'html[data-theme="dark"]' in css,
    )

    check(
        "暗色仍然是原来那套变量（:root 的值没被改动过）",
        css.count("--bg: #11151c") == 2
        and css.count("--card: #1b2230") == 2
        and css.count("--accent: #4c8dff") == 2,
        f"bg={css.count('--bg: #11151c')} card={css.count('--card: #1b2230')} "
        f"accent={css.count('--accent: #4c8dff')}",
    )

    check(
        "亮色：白卡片 + 浅灰页面 + 深色文字",
        "--card: #ffffff" in css
        and "--fg: #1b2230" in css
        and "html[data-theme=\"light\"] body" in css,
    )

    check(
        "亮色的极光是纯 CSS（固定定位的模糊色晕，没有外部图片 / 动画循环）",
        "html[data-theme=\"light\"] body::before" in css
        and "radial-gradient(" in css
        and "filter: blur(" in css
        and "position: fixed" in css
        and "url(" not in css,
    )

    check(
        "亮色下闪条 / 药丸 / 角色药丸 / 输入框 / 等待对话框都重写了颜色",
        'html[data-theme="light"] .flash.error' in css
        and 'html[data-theme="light"] .pill.bad' in css
        and 'html[data-theme="light"] .role-pill' in css
        and 'html[data-theme="light"] input[type="password"]' in css
        and 'html[data-theme="light"] dialog.room-wait' in css,
    )

    # 极光是静态的（没有动画），所以不需要 prefers-reduced-motion；
    # 但如果以后有人给它加了动画，这条会提醒他补上媒体查询。
    aurora_block = css.split('html[data-theme="light"] body::before', 1)[1]
    aurora_block = aurora_block.split("}", 1)[0]

    check(
        "极光层没有任何动画（有动画就必须尊重 prefers-reduced-motion）",
        "animation" not in aurora_block
        or "prefers-reduced-motion" in css,
    )

    # ============================================================
    # 9b. 改名 MC-Link → CraftLink（方块联机）
    # ============================================================
    #
    # 产品改名了：面板上任何一处还写着 MC-Link 的地方都是 bug。名字会出现在
    # 四个位置（顶栏、登录页、<title>、页脚），所以下面逐条点名，而不是只查
    # 一个字符串 —— 少改一处，用户还是要再报一次。
    #
    # 只查"给人看的"文案：/opt/mc-link 这种服务器路径、rebuind.sh 这种命令、
    # mc-relay 这种容器名都是技术标识，改了会把命令指向不存在的东西。
    old_brand = re.compile(r"MC[-\s]?Link", re.I)
    old_brand_hits = []

    for path in files:
        relative = os.path.relpath(path, PANEL).replace(os.sep, "/")
        text = open(path, encoding="utf-8").read()

        for match in old_brand.finditer(text):
            line_number = text[:match.start()].count("\n") + 1
            line_text = text.split("\n")[line_number - 1]
            context = " ".join(
                text[max(0, match.start() - 45):match.end() + 45].split()
            )

            # 技术标识：服务器目录 /opt/mc-link/、命令里的路径、数据库名 mclink、
            # 环境变量 MCLINK_*、容器名 mc-relay —— 这些改了会把命令指到不存在的
            # 地方，一个字符都不能动。
            if "/opt/mc-link" in context or "mc-link/" in context:
                continue

            if re.match(r"^mclink", match.group(0), re.I):
                continue

            # 说明"改名"这件事的注释要写出旧名字才说得清。判据要具体：
            # 这一行得是注释，而且带着改名的标记（箭头，或者"原"/"旧"这种
            # 明确表示"以前叫这个"的字）。含糊的旧名字照样报。
            is_comment = "//" in line_text or line_text.lstrip().startswith(
                ("#", "*", "/*")
            )

            if is_comment and re.search(
                r"→|原 |原MC|原 MC|旧的 |旧名字|版权", line_text
            ):
                continue

            old_brand_hits.append(f"{relative}:{line_number} {context[:70]}")

    check(
        "面板里没有残留的旧产品名「MC-Link」（路径/命令除外）",
        not old_brand_hits,
        "; ".join(old_brand_hits[:6]),
    )

    check(
        "顶栏 + 登录页 + <title> + 页脚都写 CraftLink",
        "CraftLink 管理面板" in header_src
        and "CraftLink 管理面板" in login_src
        and "CraftLink 管理面板" in footer_src
        and "'CraftLink 管理面板'" in common_src
        and "t('请使用你的 CraftLink 账号登录')" in login_src,
    )

    check(
        "英文表里是新名字（'CraftLink 管理面板' => ...）",
        "'CraftLink 管理面板' => 'CraftLink Admin Panel'" in i18n_src
        and "'请使用你的 CraftLink 账号登录'" in i18n_src,
    )

    # 品牌标记：内联 SVG 草方块。不能引用不存在的图片 —— 部署包只收
    # panel/web/assets/*.css，多一个 logo.png / logo.svg 都会在服务器上 404。
    check(
        "顶栏和登录页用的都是内联 SVG 品牌标记（不引用图片文件）",
        header_src.count('class="logo-mark"') == 1
        and login_src.count('class="logo-mark"') == 1
        and "<polygon" in header_src
        and "logo-mark" in css,
    )

    check(
        "旧 logo（蓝底「MC」字母方块）已经拿掉",
        '<span class="logo">MC</span>' not in header_src
        and '<span class="logo big">MC</span>' not in login_src
        and ">MC</span>" not in header_src,
    )

    # ---- 页脚：备案号（★ 新口径：没配置就不渲染）+ 版权行跟着语言走 ----
    #
    # 用户明确要求过：备案号是纯文本，不许链到工信部。这一条守死它 ——
    # 不只是"现在没有"，而是"以后也不许出现"。
    #
    # ★ 2026-10-05（设置页第 10 条）：ICP **可配置，默认空**。
    #   以前页脚里写死了一个登记号（`if ($icp === '') { $icp = '陇ICP...'; }`），
    #   等于把某一个站点的备案信息发给了所有用户 —— 开源版必须：
    #     * 没配置（空 / 未设）-> 页脚**什么都不渲染**；
    #     * 配置了            -> 渲染配置值，纯文本、h() 转义、不带外链。
    #
    #   过渡期说明：面板工作流正在改 layout_footer.php（去掉那个写死的兜底）。
    #   在它落地之前，"默认空"这一条按 [TODO] 记录、不判失败；他们删掉兜底
    #   之后，下面的新口径断言**自动变为强制**（不需要有人回来改这个文件）。
    #
    #   注意判据是"**代码里把某个登记号当默认值**"（`$icp = '<某登记号>'`），
    #   不是"文件里出现过某串字" —— 注释里讲历史值时不该误判。
    icp_hardcoded = re.search(r"\$icp\s*=\s*'[^']*ICP", footer_src) is not None

    if icp_hardcoded:
        print(
            "  [TODO] 页脚仍把某个登记号当默认值（$icp = '<登记号>'）—— "
            "等面板工作流落实「ICP 默认空 -> 不渲染」后，下面的断言自动转为强制",
            flush=True,
        )
    else:
        check(
            "★ 页脚不再把某个登记号当默认值（ICP 默认空 -> 不渲染）",
            re.search(r"\$icp\s*!==?\s*''", footer_src) is not None,
            "需要一个'非空才渲染'的判断（否则空值会渲染出孤零零的分隔符）",
        )

    # 这一条与上面的口径无关，两个阶段都必须为真：配置了 ICP，页脚渲染的
    # 就是**配置值本身**（从设置/配置里拿，并且 h() 转义后输出）。
    check(
        "★ ICP 渲染的是配置值本身（读设置 + h() 转义，不是写死的串）",
        "h($icp)" in footer_src
        and (
            "panel_icp_number()" in footer_src
            or "config('panel.icp')" in footer_src
        ),
        "页脚必须：读配置/面板设置里的 ICP -> h() 转义输出",
    )

    check(
        "★ 面板配置里 ICP 的默认值是空串（不是某个站的登记号）",
        "'icp' => ''" in common_src,
        "config 默认值必须是空的，否则'默认不渲染'又变成一句空话",
    )

    check(
        "页脚没有指向工信部 / 任何备案查询站的外链",
        "miit.gov.cn" not in footer_src
        and "beian.miit" not in footer_src
        and "beian.gov.cn" not in footer_src,
        "备案号必须是纯文本：用户明确要求过不许加外链",
    )

    check(
        "版权行用新产品名（中文原文带「方块联机」，英文只留 CraftLink）",
        "© %s CraftLink（方块联机）" in footer_src
        and "'© %s CraftLink（方块联机）' => '© %s CraftLink'" in i18n_src,
    )

    check(
        "页脚品牌文字也过 t()（英文界面下不再是中文）",
        "h(t('CraftLink 管理面板'))" in footer_src,
    )

    # ============================================================
    # 9c. 亮色 / 暗色两套主题下，可交互控件的颜色都要写全
    # ============================================================
    #
    # 用户报过两次"亮色主题下按钮白底白字"。根因是亮色那一段只覆盖了一部分
    # 选择器，剩下的靠继承 —— 而继承来的正是暗色的浅色文字。
    #
    # 所以这里不靠肉眼：把 CSS 里所有控件规则抓出来，逐条查"亮色有没有显式的
    # color + background"。合并写的选择器（逗号一组）也算，因为一组里每个
    # 选择器都拿到了同样的声明。
    #
    # 一个不是真正解析器的解析器：够用就行 —— 它只需要认出
    #     selector[, selector] { prop: value; }
    # 这一种写法，而面板的 CSS 就是这一种。嵌套的 @media 里的规则也能抓到
    # （去掉前缀之后选择器还是原来的形状）。
    def css_rules(text):
        rules = {}

        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
            body = match.group(2)

            for selector in match.group(1).split(","):
                selector = " ".join(selector.split())

                # 注释尾巴（"/* ... */ .foo"）算不到选择器里
                selector = selector.split("*/")[-1].strip()

                if selector.startswith("@") or selector == "":
                    continue

                # :not(...) 只是排除条件，不改变"这条规则属于哪个控件"。
                # 面板里的 hover / focus 为了不跟 :focus 打架写成了
                # :hover:not(:focus)，归一化之后才认得出来。
                plain = " ".join(
                    re.sub(r":not\([^)]*\)", "", selector).split()
                )

                rules.setdefault(plain, "")
                rules[plain] += body

        return rules

    all_rules = css_rules(css)
    light_rules = css_rules(css.split('html[data-theme="light"]', 1)[1])

    def properties(body):
        found = set()

        for declaration in body.split(";"):
            name, _, _value = declaration.partition(":")
            found.add(name.strip().lower())

        return found

    def has_colour(body, name):
        """
        color / background / border 有没有被显式写过。

        写法可以是简写（border: 1px solid ...）也可以是单独的 longhand
        （border-color: ...），两种都算写过。border-radius 不算 —— 它是圆角，
        不是颜色。
        """
        names = {n for n in properties(body) if n}

        if name in names:
            return True

        for candidate in names:
            if not candidate.startswith(name + "-"):
                continue

            if candidate in ("border-radius", "border-radius-x",
                             "border-radius-y"):
                continue

            if name == "outline" and candidate == "outline-offset":
                continue

            return True

        return False

    def coverage(body):
        covered = set()

        for name in ("color", "background", "border", "box-shadow"):
            if has_colour(body, name):
                covered.add(name)

        return covered

    REQUIRED = "color background".split()

    # 控件清单：亮色和暗色都必须**各自写全**颜色。暗色的"基线"就是 :root 那一套
    # 变量，但它同样要显式声明，不然以后有人改了基线，暗色会静默跟着变。
    control_selectors = [
        'input[type="text"]',
        'input[type="password"]',
        'input[type="number"]',
        'input[type="checkbox"]',
        'input[type="radio"]',
        'select',
        'textarea',
    ]

    # 文本框 / 数字 / 下拉 / 多行：静态外观要 color + background；
    # hover / focus 是"反馈"，只要求它明确改掉边框（聚焦环另外还要 box-shadow）。
    #
    # disabled 例外：**暗色**只压透明度，颜色沿用基样式（深底浅字压暗之后照样
    # 能读），所以暗色不查颜色；亮色必须自己写全 —— 亮色下"继承基样式"继承到
    # 的正是暗色的浅色字，白底白字就是这么来的。
    text_states = {
        "": REQUIRED,
        ":hover": ["border"],
        ":focus": ["border", "box-shadow"],
        ":disabled": [],
    }

    # 勾 / 圆点是画出来的，不是文字，所以复选框和单选框不查 color。
    box_states = {
        "": ["background", "border"],
        ":hover": ["border"],
        ":focus-visible": ["box-shadow"],
        ":disabled": [],
    }

    control_states = {
        'input[type="text"]': text_states,
        'input[type="password"]': text_states,
        'input[type="number"]': text_states,
        'input[type="search"]': text_states,
        "select": text_states,
        "textarea": text_states,
        'input[type="checkbox"]': box_states,
        'input[type="radio"]': box_states,
    }

    # 勾选态：底和边都得给
    checked_states = {
        'input[type="checkbox"]': ["background", "border"],
        'input[type="radio"]': ["background", "border"],
    }

    control_problems = []

    def themed_body(theme, selector):
        """
        某个主题下这个选择器实际生效的声明。

        暗色的"默认主题"就是不带前缀的那一套（:root 的变量），所以暗色查的是
        "裸规则 ∪ 带前缀的规则"；亮色则必须自己写全，**不许**靠继承暗色 ——
        继承来的正是暗色的浅色文字，用户看到的白底白字就是这么来的。
        """
        if theme == "dark":
            return all_rules.get(selector, "") \
                + all_rules.get(f'html[data-theme="dark"] {selector}', "")

        return light_rules.get(f'html[data-theme="light"] {selector}', "") \
            + light_rules.get(selector, "")

    def require_properties(theme, selector, needed, problems):
        """这条规则必须显式写出 needed 里的每一项（颜色走 has_colour 的宽容判断）。"""
        body = themed_body(theme, selector)

        if body == "":
            problems.append(f"{theme} 缺少 {selector}")
            return

        names = properties(body)

        for name in needed:
            if name in ("color", "background", "border", "box-shadow"):
                present = has_colour(body, name)
            else:
                present = name in names

            if not present:
                problems.append(f"{theme} {selector} 没有显式 {name}")

    for selector, states in control_states.items():
        for state, needed in states.items():
            for theme in ("dark", "light"):
                require_properties(
                    theme, selector + state, needed, control_problems
                )

        # 亮色还得额外把颜色写全（见上面的说明）。复选框 / 单选框里没有文字，
        # 所以它们只查底和边。
        require_properties(
            "light",
            selector + ":disabled",
            REQUIRED if states is text_states else ["background", "border"],
            control_problems,
        )
        require_properties("dark", selector + ":disabled", ["opacity"],
                           control_problems)

    for selector, needed in checked_states.items():
        for theme in ("dark", "light"):
            require_properties(
                theme, selector + ":checked", needed, control_problems
            )

    check(
        f"输入类控件在暗色 / 亮色下都有显式颜色（{len(control_states)} 类 × 2 套主题）",
        not control_problems,
        "; ".join(control_problems[:6]),
    )

    # ---- 输入框"现代化"的具体参数 ----
    #
    # 用户的抱怨是"这些输入框好土，像 Win XP"。上面那些检查守的是颜色，
    # 这一条守的是形状：明确的高度、圆角、左右留白、过渡、聚焦环 ——
    # 有人把它们删回默认外观，这里会红。
    control_base = all_rules.get('input[type="text"]', "")

    check(
        "输入框有明确高度 / 圆角 / 左右留白 / 0.15s 过渡",
        "height: 37px" in control_base
        and "border-radius: 9px" in control_base
        and "padding: 0 12px" in control_base
        and "transition:" in control_base,
        " ".join(control_base.split())[:120],
    )

    check(
        "每个输入控件类型都吃到了这套样式（不是只写了 text）",
        all(
            all_rules.get(selector, "").find("height: 37px") >= 0
            for selector in (
                'input[type="text"]',
                'input[type="password"]',
                'input[type="number"]',
                'input[type="search"]',
                "select",
                "textarea",
            )
        ),
    )

    focus_rings = sum(
        1 for selector, body in all_rules.items()
        if selector.endswith(":focus")
        and ('input[type="' in selector or selector.startswith("select")
             or selector.startswith("textarea"))
        and "box-shadow: 0 0 0 3px rgba(" in body
    )

    check(
        "聚焦时有可见的高亮环（box-shadow，亮暗两套各一条）",
        focus_rings >= 2,
        f"找到 {focus_rings} 条带高亮环的 :focus 规则",
    )

    check(
        "占位符在亮暗两套主题下都有单独的颜色",
        "::placeholder" in css
        and 'html[data-theme="light"] ::placeholder' in css
        and "::placeholder { color: var(--muted)" in css,
    )

    check(
        "下拉框 / 多行输入也有样式（不是只有单行文本框）",
        "select {" in css
        and "textarea {" in css
        and "padding-right: 30px" in css
        and "resize: vertical" in css,
    )

    # ---- 复选框 / 单选框：自定义外观，但表单语义一个字没动 ----
    checkbox_base = all_rules.get('input[type="checkbox"]', "")

    check(
        "复选框 / 单选框不用系统默认外观（appearance: none + 自己画）",
        "appearance: none" in checkbox_base
        and "-webkit-appearance: none" in checkbox_base
        and 'input[type="checkbox"]:checked::after' in all_rules
        and 'input[type="radio"]:checked::after' in all_rules,
    )

    check(
        "勾选态、hover、键盘焦点(:focus-visible)、禁用态都给全了",
        all(
            selector in all_rules
            for selector in (
                'input[type="checkbox"]:hover',
                'input[type="checkbox"]:focus-visible',
                'input[type="checkbox"]:disabled',
                'input[type="radio"]:hover',
                'input[type="radio"]:focus-visible',
                'input[type="radio"]:disabled',
            )
        ),
    )

    check(
        "自定义勾选框没有用 display:none（那样就点不到、也 Tab 不到了）",
        "display: none" not in checkbox_base
        and "opacity: 0" not in checkbox_base
        and "visibility: hidden" not in checkbox_base,
    )

    # 锁着的单选项（普通管理员看到的那两个）：用的是相邻兄弟选择器，
    # 不能写成后代选择器 —— 那样连正在生效的那个（disabled + checked）
    # 也会被压暗，用户会以为自己的选择没生效。
    lock_rules = [
        selector for selector in all_rules
        if "checkbox" in selector and "disabled" in selector
        and ("+" in selector or "~" in selector or ":has(" in selector)
    ]

    check(
        "锁着的选项用相邻兄弟 / :has（不是后代选择器，否则选中那个也被压暗）",
        bool(lock_rules) and "input:disabled input" not in all_rules,
        "; ".join(lock_rules[:3]),
    )

    # ---- 密码框的「显示密码」小眼睛 ----
    #
    # 不是新增功能：只是让已经输进去的密码能看一眼。type 在 password / text
    # 之间切，name 和 value 一个字都不动，提交的还是同一个字段。
    #
    # 四个 = 登录页 1 + 「我的账号」3。账号管理页里那个"给别人设的新密码"是
    # **明文**输入框（type="text"），它刻意不带小眼睛 —— 明文框上再放一个
    # "显示"按钮没有意义，只会多一个点了没反应的小图标（用户报的就是这个）。
    toggle_fields = 0
    toggle_buttons = 0

    for path in files:
        text = open(path, encoding="utf-8").read()
        toggle_fields += text.count('class="password-field"')
        toggle_buttons += text.count('class="password-toggle"')

    check(
        "四个密码框包上了 .password-field（登录 1 + 我的账号 3）",
        toggle_fields == 4 and toggle_buttons == 4,
        f"password-field={toggle_fields} password-toggle={toggle_buttons}",
    )

    check(
        "账号页给别人设密码的明文框没有小眼睛（一页两枚眼睛那种冲突不再有）",
        'class="password-field"' not in users_src
        and 'class="password-toggle"' not in users_src,
        "users.php 里还有小眼睛",
    )

    check(
        "小眼睛按钮用内联 SVG（不引用任何图片文件）",
        'class="eye"' in login_src
        and 'class="eye-off"' in login_src
        and 'class="eye"' in account_src,
    )

    check(
        "小眼睛的脚本内联在 prefs.php（登录页和每个页面都会加载，没有新 .js 文件）",
        "password-toggle" in prefs_src
        and "input.type = hidden ? 'text' : 'password'" in prefs_src,
    )

    # ---- 小眼睛为什么点不动：事件委托 ----
    #
    # prefs.php 是在顶栏（<header>）里引的，也就是**在页面正文之前**执行。
    # 老写法 `document.querySelectorAll('.password-field')` 在那一刻还没有
    # 节点可绑：「我的账号」那三个密码框在文档后面，按钮上一根监听都没有，
    # 点它当然一动不动。登录页恰好相反（密码框在 prefs.php 之前），所以那边
    # 一直是好的 —— 这正是用户报的"登录页能点、我的账号页点不动"。
    #
    # 现在的实现是事件委托：点击冒泡到 document 再去找最近的 .password-field，
    # 谁先谁后都无所谓，页面上任何密码框（包括 PHP 循环渲染出来的）都自动
    # 生效。这两条检查守的就是"别再退回按加载顺序绑定的写法"。
    # NOTE: 注释里写着老写法（说明为什么改），所以先去掉注释再看代码。
    prefs_code = strip_code_comments(prefs_src)

    check(
        "小眼睛用事件委托挂监听（DOM 里后出现的密码框也能点）",
        "document.addEventListener('click'" in prefs_code
        and "querySelectorAll('.password-field')" not in prefs_code,
        "又变回加载时就 querySelectorAll 的写法了",
    )

    # NOTE: the needle is pre-bound because an f-string expression may not
    # contain a backslash before Python 3.12.
    wrapped_needle = 'class="password-field"'

    check(
        "「我的账号」的三个密码框都在 .password-field 里（都能拿到小眼睛）",
        account_src.count(wrapped_needle) == 3
        and account_src.count('class="password-toggle"') == 3,
        f"{account_src.count(wrapped_needle)} 个包装",
    )

    # ---- 小眼睛要正正好在输入框中间 ----
    #
    # 容器以前是纯 position:relative 的 <span>。行内盒比 37px 的输入框高一截
    # （基线下面还有字体下沿），按钮 top:50% 落的是"容器"的中心，于是看着偏下。
    # 换成 display:flex + align-items:center 之后容器高度就等于输入框高度。
    password_field_rule = all_rules.get(".password-field", "")

    check(
        ".password-field 是 flex + 垂直居中（容器和输入框一样高，按钮才在正中间）",
        "display: flex" in password_field_rule
        and "align-items: center" in password_field_rule,
        " ".join(password_field_rule.split())[:120],
    )

    # ---- 关掉浏览器自带的密码小眼睛 ----
    #
    # Edge（Windows 默认浏览器）会在每个 input[type=password] 右端画一枚它
    # 自己的小眼睛，和我们那枚叠在一起 —— 用户看到的就是"输入密码的时候出现
    # 两个小眼睛"。::-ms-reveal / ::-ms-clear 是关掉它的官方方式。
    check(
        "关掉了浏览器自带的密码小眼睛（::-ms-reveal / ::-ms-clear）",
        "input[type=\"password\"]::-ms-reveal" in css
        and "input[type=\"password\"]::-ms-clear" in css,
        "Edge 会出现第二枚眼睛",
    )

    check(
        "密码框也去掉了 WebKit 的原生装饰（-webkit-appearance: none）",
        "-webkit-appearance: none" in all_rules.get('input[type="password"]', ""),
    )

    check(
        "小眼睛在亮暗两套主题下都有颜色",
        "html[data-theme=\"light\"] .password-field .password-toggle" in css
        and ".password-field .password-toggle {" in css,
    )

    check(
        "checkbox / radio 在亮暗两套下都有颜色（暗色走基线，亮色必须自己写）",
        all(
            themed_body(theme, selector) != ""
            for theme in ("dark", "light")
            for selector in ('input[type="checkbox"]', 'input[type="radio"]')
        )
        and 'html[data-theme="light"] input[type="checkbox"]' in light_rules
        and 'html[data-theme="light"] input[type="radio"]' in light_rules,
    )

    check(
        "没有新增 .js 文件（部署包只收 .css，新增的 .js 会 404）",
        not any(
            name.endswith(".js")
            for _base, _dirs, names in os.walk(
                os.path.join(PANEL, "web", "assets")
            )
            for name in names
        ),
    )

    # ---- 真的算一遍对比度 ----
    #
    # 上面查的是"有没有写颜色"，这一条查"写了之后看不看得清"。用户报的
    # "白底白字"就是颜色写了但算起来看不清的那一类 —— 与其靠眼睛，不如把
    # WCAG 的相对亮度公式搬进来：4.5:1 是正常文字的下限，我们放宽到 4.0
    # （面板里都是短标签和按钮，字体偏粗），低于这个值直接失败。
    def relative_luminance(colour):
        text = colour.strip().lstrip("#")

        if len(text) == 3:
            text = "".join(char * 2 for char in text)

        channels = [int(text[i:i + 2], 16) / 255 for i in (0, 2, 4)]

        def linear(value):
            return value / 12.92 if value <= 0.03928 \
                else ((value + 0.055) / 1.055) ** 2.4

        red, green, blue = (linear(channel) for channel in channels)

        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    def contrast(foreground, background):
        first = relative_luminance(foreground)
        second = relative_luminance(background)

        lighter, darker = max(first, second), min(first, second)

        return (lighter + 0.05) / (darker + 0.05)

    # (前景, 背景, 说明, 下限)：全部取"最终落到屏幕上的实色"，不用变量 ——
    # 变量在亮暗两套里各有一份，这里两套都列出来。
    #
    # 下限 4.5 = WCAG AA 正常文字；3.0 只给"暗色主题下沿用 --accent 的地方"
    #（白字落在 #4c8dff 上是 3.2:1，按钮字是加粗的，正好落在 WCAG 对大号/
    # 粗体文字的 3:1 档）。那个颜色是暗色主题的既定视觉，也是客户端和面板
    # 共用的那个蓝，不能为了这零点几去改它 —— 所以这里把下限写明白，
    # 而不是把阈值悄悄调低到看不出问题。亮色的强调色是另一支（#2f6fe4），
    # 白字 4.65:1，稳稳够 AA。
    CONTRAST_PAIRS = [
        ("#ffffff", "#2f6fe4", "亮色 主按钮文字/底色", 4.5),
        ("#ffffff", "#2a63cd", "亮色 主按钮 hover", 4.5),
        ("#1b2230", "#f4f6fb", "亮色 次要按钮文字/底色", 4.5),
        ("#101623", "#e7ecf6", "亮色 次要按钮 hover", 4.5),
        ("#9c2a24", "#fdeaea", "亮色 危险按钮", 4.5),
        ("#1b2230", "#ffffff", "亮色 页面正文/白卡片", 4.5),
        ("#4f5a73", "#ffffff", "亮色 次要文字/白卡片", 4.5),
        ("#14683c", "#e9f8ef", "亮色 成功闪条", 4.5),
        ("#9c2a24", "#fdecea", "亮色 错误闪条", 4.5),
        ("#7f5a10", "#fdf5e2", "亮色 警告闪条", 4.5),
        ("#27457f", "#eef3fd", "亮色 只读提示条", 4.5),
        ("#1b2230", "#ffffff", "亮色 输入框文字/白底", 4.5),
        ("#5f6a80", "#f0f2f7", "亮色 禁用输入框", 3.5),
        ("#6b7590", "#ffffff", "亮色 占位符", 4.5),
        ("#101623", "#4c8dff", "亮色 角色药丸 player（调色板冻结）", 4.5),
        ("#101623", "#3ddc84", "亮色 角色药丸 admin（调色板冻结）", 4.5),
        ("#101623", "#ff9f43", "亮色 角色药丸 root（调色板冻结）", 4.5),
        ("#ffffff", "#2f6fe4", "亮色 语言切换选中", 4.5),
        # 暗色：原来那一套，一起算，免得以后有人"顺手调一下"
        ("#ffffff", "#4c8dff", "暗色 主按钮文字/底色（ACCENT 冻结）", 3.0),
        ("#e8edf7", "#232c3d", "暗色 次要按钮文字/底色", 4.5),
        ("#ffb3ae", "#43262a", "暗色 危险按钮", 4.5),
        ("#e8edf7", "#1b2230", "暗色 页面正文/卡片", 4.5),
        ("#9aa7bd", "#1b2230", "暗色 次要文字/卡片", 4.5),
        ("#8ef0b6", "#16301f", "暗色 成功药丸", 4.5),
        ("#ffb3ae", "#331a1c", "暗色 错误药丸", 4.5),
        ("#ffffff", "#4c8dff", "暗色 语言切换选中（ACCENT 冻结）", 3.0),
        # 操作日志的分类徽章：五类各自的颜色，两套主题都要读得清。
        ("#a9c6ff", "#17263f", "暗色 分类 登录", 4.5),
        ("#d9bcff", "#2a2036", "暗色 分类 房间", 4.5),
        ("#8ef0b6", "#14332a", "暗色 分类 连接", 4.5),
        ("#ffd98a", "#33280f", "暗色 分类 面板操作", 4.5),
        ("#c3cbd9", "#262b33", "暗色 分类 系统", 4.5),
        ("#27457f", "#e8eefc", "亮色 分类 登录", 4.5),
        ("#552b86", "#f2eafc", "亮色 分类 房间", 4.5),
        ("#14683c", "#e7f7ee", "亮色 分类 连接", 4.5),
        ("#7f5a10", "#fdf3df", "亮色 分类 面板操作", 4.5),
        ("#38425c", "#eef1f7", "亮色 分类 系统", 4.5),
        # 分段选择器选中的那格（和主按钮同一组颜色，单独列出来免得以后
        # 有人只改了其中一处）。
        ("#ffffff", "#2f6fe4", "亮色 分段选择器选中", 4.5),
        ("#ffffff", "#4c8dff", "暗色 分段选择器选中（ACCENT 冻结）", 3.0),
    ]

    dim_pairs = []

    for foreground, background, label, floor in CONTRAST_PAIRS:
        ratio = contrast(foreground, background)

        if ratio < floor:
            dim_pairs.append(f"{label} 只有 {ratio:.1f}:1（要 {floor}）")

    check(
        f"按钮 / 药丸 / 闪条的文字对比度都够（算了 {len(CONTRAST_PAIRS)} 对）",
        not dim_pairs,
        "; ".join(dim_pairs[:6]),
    )

    # 选择框展开后的选项列表：Windows 上不跟随 select 的颜色，必须单独给
    light_css = css.split('html[data-theme="light"]', 1)[1]

    check(
        "下拉选项在亮暗两套下都显式给了颜色（否则展开列表会看不见）",
        "option {" in css
        and 'html[data-theme="light"] option {' in light_css,
    )

    # ---- 按钮：亮色必须逐条覆盖，暗色不许被顺手改掉 ----
    REQUIRED_BUTTONS = [
        "button",
        "button:hover",
        "button:active",
        "button:disabled",
        "button.primary",
        "button.primary:hover",
        "button.danger",
        ".button",
        ".link-button",
        ".pref",
        ".pref:hover",
        ".pref.active",
        "a.pref.active",
    ]

    button_problems = []

    for selector in REQUIRED_BUTTONS:
        body = themed_body("light", selector)

        if body == "":
            button_problems.append(f"亮色缺少 {selector}")
            continue

        # hover / active 是反馈：要求它明确改掉底色或边框，而不是"和静止态一样"。
        needed = ["border"] if selector.endswith((":hover", ":active")) \
            else REQUIRED
        covered = coverage(body)

        for name in needed:
            if name not in covered:
                button_problems.append(f"亮色 {selector} 没有显式 {name}")

    check(
        f"亮色下每个按钮样式都有显式颜色（{len(REQUIRED_BUTTONS)} 条）",
        not button_problems,
        "; ".join(button_problems[:6]),
    )

    # 暗色是基线，这些规则必须还在（有人"重构"时把它们删掉就会红）
    dark_button_missing = [
        selector for selector in (
            "button", "button:hover", "button.primary", "button.primary:hover",
            "button.danger", "button.danger:hover", "button.small",
            "a.button", ".link-button", ".link-button:hover",
            ".pref", ".pref:hover", ".pref.active",
        )
        if selector not in all_rules
    ]

    check(
        "暗色的按钮规则一条都没少（暗色就是原来那一套）",
        not dark_button_missing,
        f"少了: {dark_button_missing}",
    )

    check(
        "暗色主题块里没有混进亮色的覆盖（两套主题不会互相污染）",
        all_rules.get('html[data-theme="dark"] button', "") == ""
        and all_rules.get('html[data-theme="dark"] button.primary', "") == "",
    )

    # ---- 以后新加的按钮样式不能"忘了亮色" ----
    #
    # 这一条是用户那句"以后有人加了一个没写亮色覆盖的按钮样式，检查要能红"
    # 的落点：把 CSS 里**所有**出现在亮色规则里的按钮选择器当作已知类别，
    # 再回头查它们有没有亮色覆盖。新加一个 .foo 按钮却没有亮色规则 = 失败。
    button_like = sorted({
        selector
        for selector in all_rules
        if re.search(r"(^|\s|>|,)(button|\.button|\.link-button|\.pref)\b", selector)
        or re.match(r"^html\[data-theme=\"(light|dark)\"\]\s*(button|\.button|\.link-button|\.pref)", selector)
    })

    # 已登记的例外：这些不是"按钮的颜色"，各有原因。
    button_exceptions = {
        "button:focus-visible": "焦点环用的是 outline，颜色由 :focus 那条给",
        ".button:focus-visible": "同上",
        ".button:disabled": "禁用态的颜色由 button:disabled 那条统一给",
        ".password-toggle": "小眼睛按钮的颜色在 .password-field 那几条里",
        ".password-field .password-toggle": "小眼睛：亮色那条单独写了",
        ".password-field .password-toggle:hover": "小眼睛 hover",
        ".password-field .password-toggle:focus-visible": "小眼睛焦点环",
    }

    uncovered_buttons = []

    for selector in button_like:
        bare = selector.replace('html[data-theme="light"] ', "") \
                       .replace('html[data-theme="dark"] ', "")

        if bare in button_exceptions or selector in button_exceptions:
            continue

        if bare.startswith("button:focus-visible") or ":disabled" in bare:
            continue

        key = f'html[data-theme="light"] {bare}'

        if key not in light_rules and light_rules.get(bare, "") == "":
            uncovered_buttons.append(bare)

    check(
        f"每个按钮样式都有亮色覆盖（查了 {len(button_like)} 条选择器）",
        not uncovered_buttons,
        "亮色里没有: " + ", ".join(uncovered_buttons[:6]),
    )

    # ============================================================
    # 10. 关房等待页的文案不能硬编码在 JS 里
    # ============================================================
    #
    # 脚本是内联在 footer 里的（部署包只收 .css，单独的 .js 会 404），它自己
    # 不知道用户选了哪种语言 —— 只有 PHP 知道。所以文案必须由 PHP 通过
    # STRINGS 传进来；脚本里一旦写死中文，英文界面下点「关闭房间」就会突然
    # 冒出一整屏中文。
    scripts = re.findall(r"<script\b.*?</script>", footer_src, flags=re.S | re.I)

    check("footer 里有内联脚本", len(scripts) >= 2, f"{len(scripts)} 段")

    cjk = re.compile(r"[\u3400-\u9fff\uff00-\uffef\u3000-\u303f]")

    hardcoded = []

    for script in scripts:
        code = strip_code_comments(script)

        if cjk.search(code):
            hardcoded.append(code.strip()[:80])

    check(
        "等待页 / 自动刷新的 JS 里没有硬编码中文（文案由 PHP 传入）",
        not hardcoded,
        " | ".join(hardcoded[:2]),
    )

    check(
        "文案通过 PHP 生成的 STRINGS 传给脚本",
        "var STRINGS = " in footer_src
        and "$js_strings = [" in footer_src
        and "STRINGS.wait_elapsed" in footer_src
        and "STRINGS.wait_done" in footer_src
        and "STRINGS.reload" in footer_src,
    )

    check(
        "对话框的每句话都取自 STRINGS（不再出现 '已等待 ' 这种字面量）",
        "STRINGS.wait_title" in footer_src
        and "STRINGS.hint_default" in footer_src
        and "STRINGS.bad_reply" in footer_src
        and "STRINGS.timeout" in footer_src
        and "STRINGS.offline" in footer_src,
    )

    check(
        "残留记录那行的提示文案也是从 PHP 传的（data- 属性）",
        'data-room-hint="<?= h(t(' in rooms_src,
    )

    # ============================================================
    # 11. 头像：有图片就用图片，没有就退回字母（绝不出现破图）
    # ============================================================
    check(
        "avatar_url() 用 file_exists() 探图片，没有就返回空串",
        "function avatar_url(" in common_src
        and "file_exists($candidate)" in common_src
        and "return '';" in common_src.split("function avatar_url(", 1)[1],
    )

    check(
        "图片地址走 asset_url('assets/avatar.png')（带缓存戳）",
        "asset_url('assets/avatar.png')" in common_src,
    )

    check(
        "登录页：有图片显示头像，没有就退回内联草方块（不引用任何图片文件）",
        '$avatar = avatar_url();' in login_src
        and 'class="login-avatar"' in login_src
        and 'class="logo big"' in login_src
        and 'class="logo-mark"' in login_src
        and "if ($avatar !== '')" in login_src,
    )

    check(
        "顶栏：有图片就用同一张，没有就还是首字母（角色药丸不变）",
        '$avatar = avatar_url();' in header_src
        and "if ($avatar !== '')" in header_src
        and 'class="role-pill"' in header_src
        and "$initial = substr(" in header_src,
    )

    check(
        "头像图片有尺寸样式（登录页大、顶栏小）",
        ".login-avatar" in css and ".who .avatar img" in css,
    )

    # ============================================================
    # 12. 面板修复：分段选择器 / 日志页（留白 + 保留期 + 筛选 + 分类）
    # ============================================================
    #
    # 用户拿着截图报的五件事，下面每一条都对着一个具体的坏法：
    #   1. 登录页的小眼睛没居中（容器是行内盒，比输入框高几个像素）
    #      + Edge 自带的小眼睛和我们那枚重叠 → 两个眼睛
    #   2. 「我的账号」三个小眼睛点不动（脚本在密码框之前执行，绑不上）
    #   3. 账号管理页"给别人改密码"的明文框上也有小眼睛 → 去掉
    #   4. 新建账号的角色选择器渲染坏了（label.checkbox 的 width:auto 压掉了
    #      单选框的 17px，appearance:none 之后只剩一条缝）
    #   5. 日志页太挤、没有保留期、没有筛选、分类看不出来
    relay_server_src = open(
        os.path.join(ROOT, "server", "relay_server.py"), encoding="utf-8"
    ).read()

    db_store_src = open(
        os.path.join(ROOT, "server", "db_store.py"), encoding="utf-8"
    ).read()

    events_src = open(
        os.path.join(PANEL, "web", "views", "events.php"), encoding="utf-8"
    ).read()

    # ---- 12a. 新建账号的角色选择器：圆角方块分段控件 ----
    segmented_rule = all_rules.get(".segmented", "")
    segmented_checked = all_rules.get(
        ".segmented-option input:checked + .segmented-label", ""
    )
    segmented_input = all_rules.get('.segmented input[type="radio"]', "")

    check(
        "角色选择器换成分段控件（坏掉的 label.checkbox + radio 已经不在页面里）",
        'class="segmented"' in users_src
        and 'class="segmented-option"' in users_src
        and 'class="segmented-label"' in users_src
        and 'class="checkbox' not in users_src,
    )

    # NOTE: the needle is pre-bound because an f-string expression may not
    # contain a backslash before Python 3.12.
    radio_needle = 'type="radio" name="role"'

    check(
        "分段控件仍然提交同一个字段和同样的值（role=player / role=admin）",
        users_src.count(radio_needle) == 3
        and 'name="role" value="player"' in users_src
        and 'name="role" value="admin"' in users_src
        and 'value="root"' not in users_src,
        f"{users_src.count(radio_needle)} 个单选项",
    )

    check(
        "分段控件是一排圆角方块、选中的填强调色（暗色基线写全）",
        "display: inline-flex" in segmented_rule
        and "border-radius" in segmented_rule
        and "var(--accent)" in segmented_checked
        and has_colour(segmented_checked, "background")
        and has_colour(segmented_checked, "color"),
        " ".join(segmented_checked.split())[:120],
    )

    check(
        "分段控件亮色下也写全了颜色（容器 / 选中 / 锁着的那格）",
        coverage(themed_body("light", ".segmented")) >= {"background", "border"}
        and "var(--accent)"
        in themed_body("light", ".segmented-option input:checked + .segmented-label")
        and has_colour(
            themed_body("light", ".segmented-option.locked .segmented-label"),
            "color",
        ),
    )

    check(
        "分段控件键盘可用（radio 还在、Tab 有焦点环，不是 display:none 藏起来）",
        ".segmented-option input:focus-visible + .segmented-label" in all_rules
        and "opacity: 0" in segmented_input
        and "display: none" not in segmented_input
        and "display:none" not in segmented_input,
    )

    check(
        "上锁的那个选项保留了说明（锁着而不是隐藏，藏起来像面板坏了）",
        "segmented-option locked" in users_src
        and ".segmented-option.locked .segmented-label" in all_rules,
    )

    # ---- 12b. 日志页：留白 ----
    events_cell = all_rules.get(".table.events-table td", "")

    check(
        "日志表有呼吸的空间（更高内边距 + 行距 1.65 + 固定列宽）",
        "padding: 11px 14px" in events_cell
        and "line-height: 1.65" in events_cell
        and "table-layout: fixed" in all_rules.get(".table.events-table", ""),
        " ".join(events_cell.split())[:120],
    )

    check(
        "说明列允许折行（长说明换行，不是把表格撑爆 / 挤成一团）",
        "white-space: normal"
        in all_rules.get(".table.events-table td.cell-detail", "")
        and "white-space: normal" in all_rules.get(".table.events-table", ""),
    )

    check(
        "八列都给了列宽（时间/分类/操作/操作者/房间/会话/IP/说明）",
        all(
            f".table.events-table th:nth-child({index})" in all_rules
            for index in range(1, 9)
        ),
    )

    # NOTE: the needle is pre-bound because an f-string expression may not
    # contain a backslash before Python 3.12.
    mono_needle = 'class="mono"'

    check(
        "等宽字体只给技术列（时间 / 会话 / 来源 IP），说明与人名用正文字体",
        events_src.count(mono_needle) == 3
        and 'class="cell-detail"' in events_src
        and events_src.count("<th>") == 8,
        f"mono={events_src.count(mono_needle)} th={events_src.count('<th>')}",
    )

    # ---- 12c. 保留期：每 10 天自动清理一次 ----
    check(
        "面板侧保留期常量 = 10 天，删的是超期的那部分",
        "const PANEL_EVENT_RETENTION_DAYS = 10;" in common_src
        and "function panel_purge_old_events(" in common_src
        and "DELETE FROM relay_events WHERE created_at < (NOW() - INTERVAL %d DAY)"
        in common_src,
    )

    check(
        "日志页每次打开顺手清一次（只更新了面板时也还有保留期）",
        "panel_purge_old_events()" in index_src
        and "t('自动清理超过 %d 天的日志：%d 条')" in index_src,
    )

    check(
        "中继侧定时清理真的被启动（每 10 天 / 每 6 小时检查一次，任务被登记）",
        "EVENTS_RETENTION_DAYS = 10" in relay_server_src
        and "EVENTS_RETENTION_INTERVAL = 6 * 3600" in relay_server_src
        and "async def events_retention_loop(" in relay_server_src
        and "asyncio.create_task(events_retention_loop())" in relay_server_src
        and "retention_task," in relay_server_src,
    )

    check(
        "重建中继后一分钟内就跑第一遍清理（否则要等 6 小时才看得出跑没跑）",
        "60 if first_pass else max(60, EVENTS_RETENTION_INTERVAL)"
        in relay_server_src,
    )

    check(
        "清理循环出错也不会影响中继（自己吞掉异常 + 尊重取消）",
        "except asyncio.CancelledError:" in relay_server_src.split(
            "async def events_retention_loop(", 1
        )[1].split("async def ", 1)[0]
        and "[EVENTS RETENTION ERROR]" in relay_server_src,
    )

    check(
        "四个 store 都实现了 purge_events（接口 + 内存 + MySQL + 安全壳）",
        db_store_src.count("async def purge_events(self, max_age_days):") == 4,
        f"{db_store_src.count('async def purge_events(self, max_age_days):')} 个",
    )

    check(
        "清理只删 relay_events 里超过保留期的行（一条 DELETE，没别的表）",
        '"DELETE FROM relay_events "' in db_store_src
        and '"WHERE created_at < (NOW() - INTERVAL %s DAY)"' in db_store_src,
    )

    # ---- 12d. 筛选条（服务端） ----
    check(
        "日志页有分类 / 结果两个下拉，默认是「全部」（普通 GET 表单，不需要 JS）",
        'name="category"' in events_src
        and 'name="outcome"' in events_src
        and 'method="get"' in events_src
        and events_src.count("t('全部')") == 2,
    )

    check(
        "筛选在服务端（拼进 SQL 的 WHERE、绑定参数、LIMIT 跟着筛选走）",
        "' WHERE action IN ('" in common_src
        and "$statement->execute($params);" in common_src
        and "$limit = max(1, min(1000, $limit));" in common_src,
    )

    check(
        "筛选参数过白名单（认不出的值一律当「全部」，数组参数也挡掉）",
        "is_string($_GET['category'] ?? null)" in index_src
        and "array_key_exists($filter_category, panel_event_categories())" in index_src
        and "array_key_exists($filter_outcome, panel_event_outcomes())" in index_src,
    )

    # ---- 12e. 分类：存在、可辨、有颜色 ----
    #
    # 分类表是 PHP 数组，这里按它自己的形状解析出来，再逐条查：
    # 色调必须合法、分类必须是那五个之一、该有的代号一个都不能少。
    type_entry = re.compile(
        r"^\s*'([a-z_]+)'\s*=>\s*\[t\('([^']*)'\),\s*'([a-z]*)',\s*'([a-z]+)'\],\s*$",
        re.M,
    )
    event_types = {
        match.group(1): (match.group(2), match.group(3), match.group(4))
        for match in type_entry.finditer(common_src)
    }

    check(
        "审计分类表能被解析（每条形如 'code' => [t('说明'), '色调', '分类']）",
        len(event_types) >= 35,
        f"{len(event_types)} 条",
    )

    known_categories = ("login", "room", "connection", "panel", "system")

    bad_types = [
        code for code, (_label, tone, category) in event_types.items()
        if tone not in ("", "ok", "warn", "bad") or category not in known_categories
    ]

    check(
        "每一条的色调和分类都合法（没有漏写 / 写错列的）",
        not bad_types,
        "; ".join(bad_types[:5]),
    )

    required_codes = {
        # 登录 / 登出：所有用户（含根管理员），成功与失败都要有
        "login_ok", "login_failed", "login_disabled", "login_throttled",
        "panel_login_ok", "panel_login_failed", "panel_logout", "logout",
        # 房间：建房 / 加入 / 退出 + 关房
        "room_created", "room_joined", "room_left",
        "client_delete_room", "auto_delete_empty_room", "host_timeout",
        "panel_revoke_room", "panel_delete_room", "panel_revoke_stale_room",
        "panel_delete_stale_room", "panel_purge_stale_rooms",
        # 连接：房主上下线 + 玩家进 / 出世界
        "host_online", "host_offline", "session_open", "session_close",
        # 面板操作：改开关 / 账号增删 / 提权降权 / 改密码改名 / 清空日志 / 踢人
        "setting_changed", "panel_create_user", "panel_delete_user",
        "panel_set_role", "panel_set_password", "panel_rename_user",
        "panel_self_rename", "panel_disable_user", "panel_enable_user",
        "panel_clear_events", "panel_kick_user", "panel_kick_player",
        # 系统
        "bootstrap_admin", "events_purged", "user_deleted",
    }

    missing_types = sorted(required_codes - set(event_types))

    check(
        "四类日志该有的代号一个不少（登录/房间/连接/面板操作）",
        not missing_types,
        "缺少: " + ", ".join(missing_types[:8]),
    )

    check(
        "每一类都真的有行（不是只有分类名字）",
        all(
            sum(1 for _code, meta in event_types.items() if meta[2] == slug) >= 2
            for slug in ("login", "room", "connection", "panel")
        ),
    )

    check(
        "分类徽章在亮暗两套主题下都有颜色（五类各自一个颜色）",
        all(
            f".cat-{slug}" in all_rules
            and f'html[data-theme="light"] .cat-{slug}' in light_rules
            for slug in known_categories
        ),
    )

    # ---- 12f. 面板自己写的那几笔审计 ----
    check(
        "面板登录成功 / 失败 / 登出都记（失败带原因，全都不记密码）",
        "audit_event('panel_login_ok'" in index_src
        and "audit_event('panel_login_failed'" in index_src
        and "audit_event('panel_logout'" in index_src
        and "t('面板登录失败：%s')" in index_src,
    )

    check(
        "面板每个写操作都记「谁做的」（操作者 + 目标 + IP + 结果）",
        index_src.count("$audit_op(") >= 14,
        f"{index_src.count('$audit_op(')} 处调用",
    )

    check(
        "审计行带的是浏览器 IP（反代后面的 X-Real-IP / X-Forwarded-For 优先）",
        "panel_client_ip()" in common_src
        and "HTTP_X_REAL_IP" in common_src
        and "HTTP_X_FORWARDED_FOR" in common_src,
    )

    # 硬性规则：**审计里绝不出现密码**。把每一个 audit_event / $audit_op 调用的
    # 参数（按括号配对取出来）扫一遍，只要有一个提到 $password 就算失败 ——
    # 用户明确要求过"绝不把密码打进日志"。
    def php_call_body(source, position):
        """从 position 处的 '(' 开始，返回配对括号之间的内容。"""
        start = source.index("(", position)

        depth = 0

        for cursor in range(start, len(source)):
            if source[cursor] == "(":
                depth += 1
            elif source[cursor] == ")":
                depth -= 1

                if depth == 0:
                    return source[start:cursor + 1]

        return source[start:]

    audit_calls = [
        php_call_body(index_src, match.end() - 1)
        for match in re.finditer(
            r"\$audit_op\(|(?<![\w$>])audit_event\(", index_src
        )
    ]
    leaky = [call for call in audit_calls if "$password" in call]

    check(
        f"审计调用里绝不出现密码变量（扫了 {len(audit_calls)} 处调用）",
        audit_calls and not leaky,
        " | ".join(leaky[:2]),
    )

    check(
        "审计写入永远不能让操作失败（audit_event 自己吞异常）",
        "function audit_event(" in common_src
        and "INSERT INTO relay_events" in common_src
        and "catch (Throwable $e)" in common_src.split("function audit_event(", 1)[1],
    )

    # ---- 12g. 连接：房主上下线真的在日志里（修过的 host BUG 的守卫）----
    #
    # 注意锚点：`"host_online"` 这个字面量在快照 / 状态上报里也出现过
    # （room.snapshot() 的字段名），所以这里找的是 add_event 的那两行
    # ——"host_online", 带着逗号的那个写法。
    host_body_start = relay_server_src.index("async def handle_host(")
    host_body_end = relay_server_src.index("async def host_control_loop(")
    online_at = relay_server_src.index('"host_online",')
    offline_at = relay_server_src.index('"host_offline",')

    check(
        "房主上线 / 下线在中继里各写一笔审计（都在 handle_host 里）",
        host_body_start < online_at < host_body_end
        and host_body_start < offline_at < host_body_end
        and online_at < offline_at,
    )

    check(
        "房主下线那笔写在 finally 里（异常掉线也记得下来）",
        relay_server_src.rindex("finally:", host_body_start, offline_at)
        > host_body_start,
    )

    check(
        "面板把房主上下线归到「连接」并翻译了两条说明",
        event_types.get("host_online", ("", "", ""))[2] == "connection"
        and event_types.get("host_offline", ("", "", ""))[2] == "connection"
        and event_types.get("host_online", ("",))[0] != "host_online"
        and event_types.get("host_offline", ("",))[0] != "host_offline",
    )

    check(
        "玩家进 / 出世界来自中继的 room_sessions（读出来，不是面板造数据）",
        "function panel_session_events(" in common_src
        and "FROM room_sessions" in common_src
        and "session_open" in common_src
        and "session_close" in common_src,
    )

    # ---- 12h. 中继写的每个代号，面板都得有说明 ----
    #
    # 「日志页冒出一串英文代号」这种事不会有第二次：中继（relay_server /
    # gatekeeper / admin_api / settings_service / reset_password）里写进
    # relay_events 的每个 action，都必须在这张分类表里有一条。
    server_dir = os.path.join(ROOT, "server")
    relay_codes = set()

    for name in ("relay_server.py", "gatekeeper.py", "admin_api.py",
                 "settings_service.py", "reset_password.py"):
        text = open(os.path.join(server_dir, name), encoding="utf-8").read()
        relay_codes.update(
            re.findall(r'add_event\(\s*(?:\n\s*)?"([a-z_]+)"', text)
        )
        relay_codes.update(
            re.findall(r'_audit\(\s*(?:\n\s*)?"([a-z_]+)"', text)
        )

    unlabelled = sorted(relay_codes - set(event_types))

    check(
        f"中继写的每个 action 在面板里都有说明和分类（扫到 {len(relay_codes)} 个）",
        relay_codes and not unlabelled,
        "没登记的: " + ", ".join(unlabelled[:8]),
    )

    check(
        "日志页不再声称「中继会一直保留历史」（现在有 10 天保留期）",
        "中继会一直保留历史" not in events_src
        and "PANEL_EVENT_RETENTION_DAYS" in events_src,
    )

    # ============================================================
    # 13. 「面板完全没有 CSS」：地址基准必须跟着浏览器地址栏走
    # ============================================================
    #
    # 现场：面板挂在 https://域名:54865/秘密路径/ 上（1Panel 反代，上游
    # http://127.0.0.1:19102/），同一个域名在 80/443 上还有一个刚建好的静态站。
    # HTML 出得来，assets/style.css 拿不到 —— 浏览器渲染出一页没有 CSS 的裸
    # HTML（内联 SVG 被拉成一整屏）。
    #
    # 根因是"地址基准"由服务端从 REQUEST_URI 推导：
    #   * 反代的 proxy_pass 若写成 http://上游/（结尾带斜杠），nginx 会把
    #     /秘密路径/ 从 URI 上剥掉 —— 面板看到的 REQUEST_URI 里没有前缀，
    #     算出来是 "/"，于是浏览器去 https://域名/assets/style.css 取样式表：
    #     **前缀和端口都没了**，请求落到站点根目录（另一个站点）上，404。
    #   * 服务端就算拿到了前缀，也只有路径 —— 它永远不知道用户是从哪个端口
    #     进来的。
    #
    # 改成 <base href="./"> 之后，基准是**当前文档地址所在目录**：协议、主机、
    # 端口、反代前缀全部来自地址栏，服务端推不推导都不影响结果。下面这些检查
    # 守住这条，免得以后有人又把它改回"服务端拼基址"。
    check(
        "<base> 是相对当前文档的 \"./\"（端口 / 前缀 / 协议都跟着地址栏）",
        '<base href="<?= h(panel_base_href()) ?>">' in header_src
        and '<base href="<?= h(panel_base_href()) ?>">' in login_src
        and "function panel_base_href(" in common_src
        and "return './';" in common_src.split("function panel_base_href(", 1)[1],
    )

    server_based_in_views = []

    for path in files:
        relative = os.path.relpath(path, PANEL).replace(os.sep, "/")

        # lib/ 里那两个函数本身就是"服务端推导"的实现，不在这条检查范围内。
        if relative.startswith("lib"):
            continue

        text = strip_code_comments(open(path, encoding="utf-8").read())

        if "panel_base_url(" in text:
            server_based_in_views.append(relative)

    check(
        "页面里没有一处用服务端推导的基址（它只留给 Location / cookie）",
        not server_based_in_views,
        "; ".join(server_based_in_views),
    )

    # 根绝对地址（/assets/... ）是同一个坑的另一半：浏览器会以站点根目录为
    # 基准解析，前缀和端口一起丢。带主机名的绝对地址同理（还多丢一次端口）。
    root_absolute = []

    for path in files:
        relative = os.path.relpath(path, PANEL).replace(os.sep, "/")
        text = open(path, encoding="utf-8").read()

        for needle in ('href="/', "href='/", 'src="/', "src='/",
                       'action="/', "action='/", 'href="http', 'src="http'):
            if needle in text:
                root_absolute.append(f"{relative}: {needle}")

    check(
        "资源 / 链接 / 表单都是相对地址（没有 /绝对 或 http:// 开头的）",
        not root_absolute,
        "; ".join(root_absolute[:5]),
    )

    base_url_body = common_src.split("function panel_base_url(", 1)[1].split(
        "\n}", 1
    )[0]

    check(
        "panel_base_url() 是纯路径（只用 REQUEST_URI / SCRIPT_NAME，不碰主机名）",
        "REQUEST_URI" in base_url_body
        and "SERVER_NAME" not in base_url_body
        and "HTTP_HOST" not in base_url_body,
        "基址里一旦掺进主机名，端口就会丢",
    )

    asset_body = common_src.split("function asset_url(", 1)[1].split("\n}", 1)[0]

    check(
        "asset_url() 永远返回相对地址（去掉前导斜杠，也不拼任何基址）",
        "ltrim($relative, '/')" in asset_body
        and "panel_base_url(" not in asset_body
        and "http" not in asset_body.lower(),
    )

    check(
        "样式表走 asset_url()（相对地址 + 缓存戳）",
        "asset_url('assets/style.css')" in header_src
        and "asset_url('assets/style.css')" in login_src,
    )

    # ---- 真的按 RFC 3986 解析一遍 ----
    #
    # urljoin(urljoin(文档地址, "./"), 资源相对地址) 就是浏览器解析
    # <base href="./"> + href="assets/style.css?v=..." 的那两步。四种部署形态
    # 逐个算出来，写死在这里当期望值：改坏了这一条会红。
    css_reference = "assets/style.css?v=1757830000"

    resolved_css = {
        document: urljoin(urljoin(document, "./"), css_reference)
        for document in (
            "https://mc.example.com:54865/secret/index.php?action=events",
            "https://mc.example.com:54865/secret/",
            "http://127.0.0.1:19102/index.php?action=dashboard",
            "http://127.0.0.1:19102/",
        )
    }

    expected_css = {
        "https://mc.example.com:54865/secret/index.php?action=events":
            "https://mc.example.com:54865/secret/assets/style.css?v=1757830000",
        "https://mc.example.com:54865/secret/":
            "https://mc.example.com:54865/secret/assets/style.css?v=1757830000",
        "http://127.0.0.1:19102/index.php?action=dashboard":
            "http://127.0.0.1:19102/assets/style.css?v=1757830000",
        "http://127.0.0.1:19102/":
            "http://127.0.0.1:19102/assets/style.css?v=1757830000",
    }

    check(
        "四种部署形态（反代前缀 + 非标准端口 / 直连容器）CSS 都取在当前目录",
        resolved_css == expected_css,
        "; ".join(
            f"{key} -> {value}"
            for key, value in resolved_css.items() if expected_css[key] != value
        ),
    )

    # 反证：如果基址是服务端从 REQUEST_URI 推出来的，反代只要把前缀剥掉
    # （proxy_pass http://上游/，结尾带斜杠），面板就只能算出 "/"，
    # 样式表会被请求到站点根目录 —— 也就是用户看到的"完全没有 CSS"。
    # （注意：纯路径的基址**不会**丢端口，丢的是前缀；端口要丢得靠拼绝对 URL。
    #   所以两种写法都要防：不仅基址要相对，页面里也不许出现 /绝对 地址。）
    stripped_derived = urljoin(
        "https://mc.example.com:54865/secret/index.php", "/"
    )
    stripped_css = urljoin(stripped_derived, css_reference)

    check(
        "（反证）服务端推导的基址在反代剥掉前缀后确实会丢掉 /secret/",
        "/secret/" not in stripped_css and ":54865" in stripped_css,
        stripped_css,
    )

    # 旧页面留在浏览器缓存里 = 旧的 <base> 一起留着，改完代码刷新还是老样子。
    check(
        "登录页自己（不经过 index.php 时）也发 no-store，整页不会被缓存",
        "no_cache_headers();" in login_src,
    )

    # ---- 13b. Location 头也只发相对地址 ----
    #
    # 用户实测的第二发：CSS 好了之后，点「退出登录」直接 404。因为 redirect()
    # 当时把 Location 拼成"服务端推导的基址 + 目标"，而反代把前缀剥掉时那个
    # 基址是 "/" —— 于是 `Location: /index.php?action=login`，浏览器跳到
    # https://域名:54865/index.php?action=login（域名根目录，那里是同一个域名上
    # 的静态站），404。
    #
    # 相对 Location（index.php?action=login）由浏览器用当前文档地址解析：
    # 端口和前缀一起继承，服务端知不知道前缀都无所谓。
    redirect_body = common_src.split("function redirect(", 1)[1].split("\n}", 1)[0]

    check(
        "redirect() 只发相对的 Location（不拼基址、不拼域名）",
        "header('Location: ' . $target);" in redirect_body
        and "panel_base_url(" not in redirect_body
        and "http" not in redirect_body.lower(),
        " ".join(redirect_body.split())[:120],
    )

    # NOTE: the needle is pre-bound because an f-string expression may not
    # contain a backslash before Python 3.12.
    redirect_needle = "redirect('index.php?action="

    check(
        "跳转目标一个字没改（全站仍然是 index.php?action=xxx）",
        index_src.count(redirect_needle) >= 35
        and "redirect('index.php?action=login')" in index_src
        and "redirect('index.php?action=dashboard')" in index_src
        and "redirect('index.php?action=users')" in index_src
        and "redirect('index.php?action=rooms')" in index_src
        and "redirect('index.php?action=account')" in index_src
        and "redirect('index.php?action=events')" in index_src
        and "redirect('index.php?action=login')" in logout_src,
        f"{index_src.count(redirect_needle)} 处",
    )

    # 同一个坑的另一半：把请求地址纠成带结尾斜杠的那次 301。
    # 它用的 $path 来自**浏览器发过来的** REQUEST_URI（本身带前缀），所以前缀
    # 是安全的 —— 但也别改成拼 panel_base_url()。
    slash_body = common_src.split("function enforce_trailing_slash(", 1)[1].split(
        "\n}", 1
    )[0]

    check(
        "补结尾斜杠的 301 用的是请求路径本身（不是服务端推导的基址）",
        "header('Location: ' . $target, true, 301);" in slash_body
        and "panel_base_url(" not in slash_body,
    )

    base_url_callers = []

    for path in files:
        relative = os.path.relpath(path, PANEL).replace(os.sep, "/")
        code = strip_code_comments(open(path, encoding="utf-8").read())

        if "panel_base_url(" in code:
            base_url_callers.append(relative)

    check(
        "panel_base_url() 只剩两处：定义本身 + cookie 的 path",
        sorted(base_url_callers) == ["lib/common.php", "lib/i18n.php"],
        ", ".join(base_url_callers),
    )

    # 真的按 RFC 3986 解析一遍 Location：请求地址 + 相对目标 = 浏览器跳到哪里。
    # 前两种是反代（前缀被 nginx 剥掉，但浏览器地址栏里仍然有前缀），后两种是
    # 直连容器。
    location_cases = {
        "https://mc.example.com:54865/secret/index.php": "index.php?action=login",
        "https://mc.example.com:54865/secret/": "index.php?action=login",
        "http://127.0.0.1:19102/index.php": "index.php?action=dashboard",
        "http://127.0.0.1:19102/": "index.php?action=dashboard",
    }

    resolved_location = {
        request: urljoin(request, target)
        for request, target in location_cases.items()
    }

    check(
        "四种部署形态下 Location 都落在面板目录里（端口和前缀一起继承）",
        resolved_location
        == {
            "https://mc.example.com:54865/secret/index.php":
                "https://mc.example.com:54865/secret/index.php?action=login",
            "https://mc.example.com:54865/secret/":
                "https://mc.example.com:54865/secret/index.php?action=login",
            "http://127.0.0.1:19102/index.php":
                "http://127.0.0.1:19102/index.php?action=dashboard",
            "http://127.0.0.1:19102/":
                "http://127.0.0.1:19102/index.php?action=dashboard",
        },
        str(resolved_location),
    )

    old_style_location = urljoin(
        "https://mc.example.com:54865/secret/index.php",
        "/index.php?action=login",
    )

    check(
        "（反证）拼绝对路径的旧写法会把前缀丢掉 → 落到域名根目录",
        old_style_location == "https://mc.example.com:54865/index.php?action=login",
        old_style_location,
    )

    # ============================================================
    # 14. 下载服务端日志（中继的 relay.log，不是这一页的操作日志）
    # ============================================================
    #
    # 面板这一侧只做三件事：把中继 GET /logs/export 的正文**原样**转发给浏览器、
    # 给一个管理员才看得见的按钮、下载不了的时候给一句人话。PHP 在这台机器上跑
    # 不起来，所以下面这些检查全是"读源码 + 读翻译表"，一条一条对着这三件事。
    #
    # 最要紧的是 (b)：**成功路径上不许出现 redirect / flash / json_out / HTML**。
    # 一次失败的下载必须回日志页并说清原因；而一次成功的下载里只要漏进一个
    # redirect，浏览器拿到的就是一个坏掉的 .log —— 下载头已经发出去了，正文却
    # 变成了面板的页面（或者干脆是空的）。
    download_marker = "case 'logs_download':"
    download_at = index_src.find(download_marker)
    download_block = ""

    if download_at != -1:
        after_case = index_src[download_at + len(download_marker):]
        next_case = after_case.find("case '")
        download_block = after_case[:next_case] if next_case != -1 else after_case

    check(
        "index.php 里有 logs_download 分支，且在 POST 分支里（和别的动作共用 CSRF 校验）",
        download_at != -1
        and "logs_download" in switch_actions
        and index_src.find("if ($_SERVER['REQUEST_METHOD'] === 'POST')") < download_at
        and index_src.find("check_csrf();") < download_at,
        f"第 {index_src[:download_at].count(chr(10)) + 1 if download_at != -1 else 0} 行",
    )

    check(
        "logs_download 用 require_operator() 守着（玩家 / 未登录都到不了中继）",
        "require_operator();" in download_block,
    )

    check(
        "下载走 relay_api_raw()（不是会被 json_decode 的 relay_api），路径 /logs/export，超时 30 秒",
        "relay_api_raw('GET', '/logs/export', 30)" in download_block,
    )

    # 成功路径 = 失败分支（if (!$export['ok'])）之后的这一段。
    # 用 $filename 那一行当分界：它是失败分支之后的第一条语句。
    success_marker = "$filename = 'craftlink-relay-'"
    success_at = download_block.find(success_marker)
    success_path = download_block[success_at:] if success_at != -1 else ""

    check(
        "抓到了成功路径（$filename 之后，也就是失败分支之外的那段代码）",
        success_at != -1 and "$export['body']" in success_path,
    )

    check(
        "成功路径只做下载：不 redirect、不 flash、不 json_out、不写 HTML（没有 ?> 跳回模板）",
        success_path != ""
        and "redirect(" not in success_path
        and "flash(" not in success_path
        and "json_out(" not in success_path
        and "?>" not in success_path,
    )

    check(
        "整个分支只在失败时跳回日志页（有且只有一次 redirect，目标就是 events）",
        download_block.count("redirect(") == 1
        and "redirect('index.php?action=events')" in download_block,
    )

    check(
        "下载头由面板自己发：Content-Type + Content-Disposition + Content-Length",
        "header('Content-Type: text/plain; charset=utf-8');" in success_path
        and "Content-Disposition: attachment; filename=" in success_path
        and "header('Content-Length: ' . strlen($export['body']));" in success_path,
    )

    check(
        "下载头全部发在 echo 正文之前（顺序错了头就无效）",
        success_path.find("Content-Disposition") != -1
        and success_path.find("Content-Length")
        < success_path.find("echo $export['body'];"),
    )

    # 更严的一条：发头之前**一个字符都不能写**。上面那条只看"头在正文之前"，
    # 挡不住"头之前先 echo 了一句别的东西" —— 现实中那是最常见的一种写法，
    # 而且后果一样：PHP 一旦输出过，后面的 header() 全部失效，下载里会混进
    # 那段文字。注释先去掉再找，免得文档里的 "echo" 三个字母把检查吓红。
    clean_success = strip_code_comments(success_path)
    first_header_at = clean_success.find("header(")
    before_headers = (
        clean_success[:first_header_at] if first_header_at != -1 else clean_success
    )

    check(
        "发下载头之前没有任何输出（先吐一个字，后面的 header() 就全失效）",
        first_header_at != -1
        and re.search(r"\b(echo|print|printf|print_r)\b", before_headers) is None
        and "json_out(" not in before_headers
        and "?>" not in before_headers,
        " ".join(before_headers.split())[-80:],
    )

    check(
        "正文原样吐出后立刻 exit（不接着渲染页面、不 redirect）",
        "echo $export['body'];" in success_path
        and "exit;" in success_path
        and success_path.find("echo $export['body'];") < success_path.find("exit;"),
    )

    check(
        "发头之前先确认响应还没开始（headers_sent 守卫，避免警告打断下载）",
        "if (!headers_sent()) {" in success_path,
    )

    check(
        "中继还没有这个接口（404）时给「中继暂不支持日志导出」而不是空白页",
        "$export['status'] === 404" in download_block
        and "t('中继暂不支持日志导出（需要更新服务端）')" in download_block,
    )

    check(
        "失败时 flash + redirect 回日志页，原因是中文的人话（英文走翻译表）",
        "flash(" in download_block
        and "t('下载服务端日志失败：%s')" in download_block
        and "t('中继返回 HTTP %d')" in download_block
        and "redirect('index.php?action=events')" in download_block,
    )

    # ---- 兄弟函数：relay_api_raw() 不能被写成一个"顺手解析一下 JSON"的版本 ----
    raw_helper = ""

    if "function relay_api_raw(" in common_src:
        raw_helper = common_src.split("function relay_api_raw(", 1)[1].split("\n}", 1)[0]

    check(
        "common.php 里有 relay_api_raw()：原始正文 + 状态 + Content-Type / Content-Disposition",
        raw_helper != ""
        and "CURLOPT_RETURNTRANSFER => true" in raw_helper
        and "CURLOPT_HEADERFUNCTION" in raw_helper
        and re.search(r"'ok'\s*=>\s*\$status\s*>=\s*200", raw_helper) is not None
        and "'body'" in raw_helper
        and "'content_type'" in raw_helper
        and "'content_disposition'" in raw_helper,
    )

    check(
        "relay_api_raw() 只在小响应报错时才去解析 JSON，成功时正文一个字节都不动",
        raw_helper.count("json_decode(") == 1
        and "strlen($body) <= 65536" in raw_helper,
    )

    check(
        "relay_api() 的契约没被改动（还是解析 JSON、还是只回 data 字段）",
        "function relay_api(" in common_src
        and "$decoded = json_decode((string) $raw, true);" in common_src
        and re.search(r"'data'\s*=>\s*\$decoded\['data'\] \?\? null,", common_src)
        is not None,
    )

    # ---- (c) 日志页上的按钮：管理员才看得见，POST + CSRF，标签过 t() ----
    export_form_at = events_src.find('action="index.php?action=logs_download"')
    form_start = events_src.rfind("<form", 0, export_form_at) \
        if export_form_at != -1 else -1
    form_stop = events_src.find("</form>", export_form_at) \
        if export_form_at != -1 else -1
    export_form = (
        events_src[form_start:form_stop]
        if form_start != -1 and form_stop != -1 else ""
    )

    check(
        "日志页有「下载服务端日志」按钮：POST + CSRF 隐藏域 + 中文标签走 t()",
        'method="post"' in export_form
        and 'name="csrf"' in export_form
        and "t('下载服务端日志')" in export_form,
    )

    export_guard_at = events_src.rfind(
        "<?php if (can_operate()): ?>", 0, export_form_at
    ) if export_form_at != -1 else -1

    check(
        "按钮只给能操作的人看（外面那层就是 can_operate，中间没有被 endif 提前关掉）",
        export_guard_at != -1
        and "<?php endif; ?>" not in events_src[export_guard_at:export_form_at],
    )

    check(
        "按钮旁边有说明：中继把日志存在内存 / 磁盘上，下载的是原始服务端日志",
        "t('中继把服务端日志保存在内存和磁盘上；下载得到的是原始服务端日志（不是这个页面上的操作日志），反馈问题时附上它最有用。')"
        in events_src,
    )

    check(
        "顺手显示中继日志的行数 / 大小（/logs/tail?lines=1，读不到就整段不显示）",
        "relay_api('GET', '/logs/tail?lines=1')" in index_src
        and "t('中继日志当前：%d 行，%s')" in events_src
        and "human_bytes($log_bytes)" in events_src
        and "$log_info = can_operate()" in index_src,
    )

    # ---- (d) 新加的这块标记里没有绕过 t() 的中文 ----
    #
    # 从这块自己的 <?php if (can_operate()): ?> 开始扫（不能从注释开始：扫描器
    # 一开始处在 HTML 模式，注释里的中文会被当成页面正文报出来）。
    block_marker = "<?php if (can_operate()): ?>"
    export_block_at = events_src.find(
        block_marker, events_src.find("// ---- 服务端日志下载")
    )
    block_tail = events_src[export_block_at:] if export_block_at != -1 else ""
    block_stop = block_tail.find("</p>")
    export_block = (
        block_tail[:block_stop + len("</p>")] if block_stop != -1 else block_tail
    )

    leaked_new = [
        f"第 {line} 行: {literal.strip()[:30]}"
        for literal, line in chinese_in_output(export_block)
        if cjk_pattern.search(literal.strip())
    ]

    check(
        "新加的下载区块里没有绕过 t() 的中文字面量（切到 English 不会冒出中文）",
        export_block != "" and not leaked_new,
        "; ".join(leaked_new[:3]) if leaked_new else "没有发现直接渲染的中文",
    )

    new_keys = {
        (match.group(2) + "|" + match.group(1)) if match.group(2) else match.group(1)
        for match in t_call_pattern.finditer(export_block + download_block)
    }

    check(
        f"下载功能用到的每条中文都在英文表里（{len(new_keys)} 条）",
        len(new_keys) >= 6 and new_keys <= english_keys,
        "; ".join(sorted(new_keys - english_keys)[:5]),
    )

    new_english = [value for key, value in english_entries if key in new_keys]

    check(
        "新增条目的英文里没有中文（English 界面必须真的全英文）",
        len(new_english) == len(new_keys)
        and not [value for value in new_english if cjk_pattern.search(value)],
        "; ".join(value for value in new_english if cjk_pattern.search(value))[:80],
    )

    log_export_rule = all_rules.get(".log-export", "")

    check(
        "下载那一块有独立样式，颜色只用主题变量（亮色 / 暗色都自动跟着走）",
        'class="log-export"' in events_src
        and log_export_rule != ""
        and "display: flex" in log_export_rule
        and "flex-wrap: wrap" in log_export_rule
        and "var(--card)" in log_export_rule
        and "var(--border)" in log_export_rule
        and "#" not in log_export_rule,
    )

    print("")
    print("=" * 64)
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED: {name}")
    print("=" * 64)

    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
