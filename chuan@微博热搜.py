# [title: 微博热搜]
# [language: python]
# [rule: ^(微博热搜|微博热榜|微博热搜榜|微博热点|热搜|weibo|热搜自检).*]
# [disable: false]
# [open_source: false]
# [platform: qq,qb,wx,gw,sb,wb,tg,tb,qx,xy,ip,web]
# [priority: 999999]
# [public: true]
# [version: 1.0.4]
# [author: chuan]
# [service: ]
# [description: 拉取微博实时热搜榜。支持指定条数、附链接、显示分类；带本地缓存与双接口兜底]
# [param: {"required":false,"key":"xinyuesha.wb_top_n","bool":false,"placeholder":"10","name":"默认条数","desc":"1-50，默认 10"}]
# [param: {"required":false,"key":"xinyuesha.wb_cache","bool":false,"placeholder":"60","name":"缓存秒数","desc":"这么多次重复查询直接用缓存，默认 60；填 0 关闭缓存"}]
# [param: {"required":false,"key":"xinyuesha.wb_link","bool":false,"placeholder":"0","name":"默认附链接","desc":"填 1 = 每条附微博搜索链接，填 0 或不填 = 只发纯文本"}]
# [param: {"required":false,"key":"xinyuesha.wb_cookie","bool":false,"placeholder":"SUB=xxxxx","name":"微博 Cookie（可选）","desc":"被限流或返回登录页时才需要，从浏览器 F12 复制"}]
#
# 用法：
#   微博热搜              默认条数
#   微博热搜 20           指定条数（1-50）
#   微博热搜 10 链接      附搜索链接
#   微博热搜 10 分类      每条后面标分类
#   微博热搜 帮助
#
# v1.0.4：★ 真正的根因（前三版全猜错了）
#   不是规则语法，是 **priority**。
#   面板内置一条兜底规则 ^([\s\S]+)$（未匹配任何插件时交给 AI 聊天），它本身占 999 档。
#   同档位下，早年注册的老插件（新月杀 / 服务器状态）排在兜底之前，能抢到消息；
#   新加的插件注册得晚，排在兜底之后，于是永远被兜底吃掉 → 表现就是「发什么都没反应」。
#   对照实验（同一次重启内测 5 个探针）：
#     ^探针A.*        priority 999999 → 命中
#     ^(探针B|探针B2).* priority 999999 → 命中
#     ^探针C$|^探针C2$  priority 999999 → 命中
#     ^探针D\s*        priority 999999 → 命中
#     ^探针E.*        priority 999    → 落到 ^([\s\S]+)$
#   A 与 E 规则几乎等价，唯一差别就是 priority，所以结论明确。
#   修法：新插件 priority 必须 > 999，这里用 999999（与能正常工作的菜单插件同档）。
#   ★ 另一条：改 rule / priority 后必须 pm2 restart yuhua-panel，
#     日志里的「已重载 xxx.py」只重载 param 配置，不会重新编译规则。
#
# v1.0.3：
#
# v1.0.2：
#   · priority 990→999，与另外两个在本面板验证过的插件完全一致（不再有元数据差异）
#   · rule 增加「热搜自检」：一条不联网、不读配置的纯自检命令。
#     能收到自检回复 = 文件已加载 + rule 已命中 + sender 能发消息，三件事同时成立。
#   · 日志改为「stderr + /tmp/xinyuesha_weibo_trace.log」双写，面板日志不好查时就 tail 那个文件
#   · 连回复都失败时也会记一条，避免出现「完全静默」
#
# v1.0.1：
#   · 去掉唯一一个 "bool": true 的参数（面板上其余三个插件全部只用 bool:false，
#     怀疑真实面板不认开关类型，会导致插件注册失败 → 发消息完全没反应）
#   · rule 改成 ^关键词\s* 的写法（与新月杀脚本一致，它在本面板上是验证过的）
#   · 新增别名「微博热搜榜」
#
# 实现说明：
#   · 主接口 https://weibo.com/ajax/statuses/hot_band
#       带 category（分类）与 is_ad（商业投放标记），能干净地过滤广告，实测 51 条里 1 条广告
#   · 兜底   https://weibo.com/ajax/side/hotSearch
#       只有 realtime 列表，字段少一些，但同样免登录可用
#   · s.weibo.com/top/summary 会 302 到 passport 登录页，别用
#   · 两个接口都是 GET + Referer: https://weibo.com/，不需要登录态；Cookie 只在被限流时补
#   · 缓存写 /tmp，多进程/多次调用共享；超时或异常一律降级为上一次缓存
#
# ★ 面板取参坑：bucketGet('桶名', '键名') 第二参必须是短名（wb_top_n），
#   写成 'xinyuesha.wb_top_n' 会永远读不到值。

import json
import middleware
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# 最早的一行日志：只要它出现在面板日志里，就说明「文件被加载 + rule 命中」两件事都成立，
# 后面再没反应就只可能是取数或发送环节。查问题时先搜 [微博热搜]。
# 同时写一份到 /tmp，面板日志若不好查，可直接看这个文件：
#   tail -n 50 /tmp/xinyuesha_weibo_trace.log
TRACE = os.path.join(tempfile.gettempdir(), 'xinyuesha_weibo_trace.log')


def _trace(msg):
    line = '[%s] %s' % (time.strftime('%m-%d %H:%M:%S'), msg)
    try:
        sys.stderr.write('[微博热搜] ' + msg + '\n')
        sys.stderr.flush()
    except Exception:
        pass
    try:
        with open(TRACE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


_trace('plugin invoked')

senderID = middleware.getSenderID()
sender = middleware.Sender(senderID)
userid = sender.getUserID()

CST = timezone(timedelta(hours=8))
CACHE_FILE = os.path.join(tempfile.gettempdir(), 'xinyuesha_weibo_hot.json')
MAX_N = 50
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# 主接口 → 兜底接口。返回 (条目列表, 错误信息)
SOURCES = [
    ('https://weibo.com/ajax/statuses/hot_band', 'band'),
    ('https://weibo.com/ajax/side/hotSearch', 'side'),
]


# ==================== 配置 ====================
def _cfg(key, default=''):
    short = key.split('.', 1)[1] if key.startswith('xinyuesha.') else key
    for k in (short, 'xinyuesha.' + short):
        try:
            v = middleware.bucketGet('xinyuesha', k)
        except Exception:
            v = None
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def _int_cfg(key, default, lo, hi):
    try:
        n = int(float(_cfg(key, str(default))))
    except Exception:
        n = default
    return max(lo, min(hi, n))


def _bool_cfg(key, default=False):
    v = _cfg(key, '')
    if not v:
        return default
    return v.strip().lower() in ('1', 'true', 'on', 'yes', 'y', '是', '开')


# ==================== 抓取 ====================
def _http_json(url, cookie=''):
    req = urllib.request.Request(url)
    # 微博对默认的 Python-urllib UA 不友好，必须伪装成浏览器
    req.add_header('User-Agent', UA)
    req.add_header('Referer', 'https://weibo.com/')
    req.add_header('Accept', 'application/json, text/plain, */*')
    if cookie:
        req.add_header('Cookie', cookie)
    with urllib.request.urlopen(req, timeout=8) as r:
        raw = r.read()
        if (r.headers.get('Content-Encoding') or '').lower() == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
    return json.loads(raw.decode('utf-8', 'replace'))


def _norm(items, kind):
    """把两种接口的条目统一成 {word, num, tag, cat}"""
    out = []
    for x in items or []:
        if not isinstance(x, dict):
            continue
        if x.get('is_ad') or x.get('ad_type'):      # 商业投放，剔掉
            continue
        word = (x.get('word') or x.get('note') or '').strip()
        if not word:
            continue
        try:
            num = int(x.get('num') or 0)
        except Exception:
            num = 0
        tag = (x.get('label_name') or x.get('icon_desc') or '').strip()
        cat = (x.get('category') or '').strip() if kind == 'band' else ''
        out.append({'word': word, 'num': num, 'tag': tag, 'cat': cat})
    # 同一个词可能被两个榜重复收录
    seen, uniq = set(), []
    for it in out:
        if it['word'] in seen:
            continue
        seen.add(it['word'])
        uniq.append(it)
    return uniq


def fetch(ttl, cookie):
    """返回 (列表, 错误, 是否来自缓存)"""
    cache = _read_cache()
    if cache and ttl > 0 and time.time() - cache.get('ts', 0) < ttl:
        return cache.get('items') or [], None, True

    errs = []
    for url, kind in SOURCES:
        try:
            d = _http_json(url, cookie)
        except urllib.error.HTTPError as e:
            errs.append('%s → HTTP %s' % (url.split('/')[-1], e.code))
            continue
        except Exception as e:
            errs.append('%s → %s' % (url.split('/')[-1], type(e).__name__))
            continue
        data = d.get('data') or {}
        raw = data.get('band_list') if kind == 'band' else data.get('realtime')
        items = _norm(raw, kind)
        if items:
            _write_cache(items)
            return items, None, False
        if d.get('ok') in (0, '0'):
            errs.append('%s → 接口返回 ok=0（多半是被限流）' % url.split('/')[-1])
        else:
            errs.append('%s → 返回无数据' % url.split('/')[-1])

    # 全挂了，有旧缓存就先用旧数据顶着
    if cache and cache.get('items'):
        return cache['items'], None, True
    return [], '；'.join(errs) or '未知错误', False


def _read_cache():
    try:
        with open(CACHE_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get('items') else None
    except Exception:
        return None


def _write_cache(items):
    try:
        tmp = CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'ts': time.time(), 'items': items}, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


# ==================== 格式化 ====================
def fmt_num(n):
    try:
        n = float(n or 0)
    except Exception:
        return ''
    if n <= 0:
        return ''
    if n >= 1e8:
        return '%.1f亿' % (n / 1e8)
    if n >= 1e4:
        return '%.1f万' % (n / 1e4)
    return '%d' % n


def pad(s, width):
    """按显示宽度补空格（汉字/emoji 算 2）"""
    real = 0
    for ch in str(s):
        real += 2 if ord(ch) > 0x2E7F else 1
    return str(s) + ' ' * max(0, width - real)


def build(items, n, with_link, with_cat):
    now = datetime.now(CST)
    lines = ['🔥 微博热搜 · %s' % now.strftime('%H:%M')]
    lines.append('━' * 12)
    for i, it in enumerate(items[:n], 1):
        # 热度放第二列并补到等宽：手机上比「标题后跟数字」整齐得多
        head = '%2d. %s' % (i, pad(fmt_num(it.get('num')), 7))
        tail = []
        if it.get('tag'):
            tail.append('[%s]' % it['tag'])
        if with_cat and it.get('cat'):
            tail.append(it['cat'])
        line = head + it['word'] + ('  ' + ' '.join(tail) if tail else '')
        if with_link:
            line += '\n     https://s.weibo.com/weibo?q=' + urllib.parse.quote(it['word'])
        lines.append(line)
    lines.append('')
    lines.append('共 %d 条 · 显示 %d 条 · %s 更新' % (len(items), min(n, len(items)), now.strftime('%H:%M')))
    msg = '\n'.join(lines)
    if len(msg) > 1800:      # 有的平台单条上限 2000，留点余量
        msg = msg[:1800].rstrip() + '\n…（内容过长已截断，可减少条数）'
    return msg


HELP = '''🔥 微博热搜 用法
━━━━━━━━━━━━
微博热搜            默认条数
微博热搜 20         看前 20 条（1-50）
微博热搜 全部       看完整 50 条
微博热搜 10 链接    每条附微博搜索链接
微博热搜 10 分类    每条标出分类
微博热搜 帮助       就是这条
热搜自检            检查插件是否正常加载（不联网）

· 数据来自微博实时热搜，1 分钟内重复查询走缓存
· 商业推广条目已自动过滤'''


def do_selfcheck(msg):
    """不联网、不读配置的纯自检。能收到这条回复，就说明
    「文件已加载 + rule 已命中 + sender 可发送」三件事全部成立。"""
    import platform as _pf
    keys = sorted(k for k in dir(middleware) if not k.startswith('_'))
    try:
        cwd = os.getcwd()
    except Exception:
        cwd = '(取不到)'
    lines = ['✅ 微博热搜插件自检通过',
             '━━━━━━━━━━━━',
             '收到原文: %r' % msg,
             'Python: %s' % _pf.python_version(),
             '运行目录: %s' % cwd,
             '缓存文件: %s' % CACHE_FILE,
             '缓存存在: %s' % os.path.exists(CACHE_FILE),
             '追踪日志: %s' % TRACE,
             'middleware: %s' % (', '.join(keys[:12]) or '(空)'),
             '',
             '→ 接下来发「微博热搜」应该就有结果了；',
             '   若仍有问题，把这条回复截图发我。']
    return '\n'.join(lines)


def main():
    msg = sender.getMessage().strip()
    _trace('收到: %r' % msg)

    if '自检' in msg:
        sender.reply(do_selfcheck(msg))
        return

    body = re.sub(r'^(微博热搜|微博热榜|微博热搜榜|微博热点|热搜|weibo)', '', msg).strip()

    if body in ('帮助', 'help', '?', '？', '用法'):
        sender.reply(HELP)
        return

    m = re.search(r'\d+', body)
    if m:
        n = int(m.group(0))
    elif re.search(r'全部|all', body, re.I):
        n = MAX_N
    else:
        n = _int_cfg('xinyuesha.wb_top_n', 10, 1, MAX_N)
    n = max(1, min(MAX_N, n))

    with_link = ('链接' in body) or ('link' in body.lower()) \
        or _bool_cfg('xinyuesha.wb_link', False)
    with_cat = ('分类' in body) or ('cat' in body.lower())

    ttl = _int_cfg('xinyuesha.wb_cache', 60, 0, 3600)
    cookie = _cfg('xinyuesha.wb_cookie', '')

    items, err, cached = fetch(ttl, cookie)
    if not items:
        sender.reply('❌ 微博热搜获取失败：%s\n可稍后重试；若长期失败，在参数里补一个微博 Cookie' % err)
        return

    sender.reply(build(items, n, with_link, with_cat))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        _trace('出错: %s: %s' % (type(e).__name__, e))
        try:
            sender.reply('❌ 微博热搜出错：%s: %s' % (type(e).__name__, e))
        except Exception as e2:
            _trace('连回复都失败了: %s: %s' % (type(e2).__name__, e2))
