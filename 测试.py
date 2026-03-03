"""
第二步：读取 newspaper_list.xlsx，逐个访问报刊页面，爬取每期文章信息
目标字段：文章标题 / 作者 / 日期 / 期号 / 来源报刊 / 文章详情URL
策略：
  - 复用第一步保存的 cookies_step1.json（免重新登录）
  - 若 Cookie 失效，自动重新走 Selenium 登录流程
  - 通过 AJAX 接口 /literature/entitysearch/{hash} 翻页获取文章列表
  - 支持断点续爬（已爬过的报刊跳过）
  - 输出保存到 articles_output.csv

依赖（与第一步相同）：
  pip install requests beautifulsoup4 lxml openpyxl selenium eventlet ddddocr opencv-python
"""

# ========================= 配置区域 =========================
WEBVPN_BASE  = 'https://webvpn.xmu.edu.cn'
WEBVPN_LOGIN = f'{WEBVPN_BASE}/users/sign_in'
CNBKSY_BASE  = f'{WEBVPN_BASE}/https/77726476706e69737468656265737421e7e056d2243e6a5b6d11c7af9758'

USRID    = '2006100198'
PASSWORD = 'Liang522'
USRNAME  = '厦门大学'

# ---- 输入：xlsx 文件路径，报刊URL在第几列（0-indexed）----
XLSX_PATH  = './newspaper_list.xlsx'
URL_COL    = 2          # 第三列（0-indexed = 2）
HEADER_ROW = 0          # 是否有表头行（0=有表头，None=无表头）

# ---- 输出 ----
OUTPUT_CSV     = './articles_output.csv'
PROGRESS_FILE  = './progress_step2.json'   # 记录已完成的报刊，支持断点续爬
COOKIE_FILE    = './cookies_step1.json'    # 复用第一步 Cookie

# ---- 请求参数 ----
REQUEST_DELAY = 1.5
MAX_RETRY     = 5
BACKOFF_BASE  = 2
PAGE_SIZE     = 10       # 每页文章数（与网站一致）
DRIVER_TYPE   = 'Chrome'

# ---- 调试参数（测试时使用）----
TEST_MODE        = True   # True=测试模式，False=正式爬取全部
TEST_NEWSPAPER_N = 1      # 测试模式下只爬前 N 个报刊
TEST_ISSUE_N     = 3      # 测试模式下每个报刊只爬前 N 期（0=不限）
# ========================= 配置区域 =========================

import os, sys, csv, json, time, re, random, logging, base64, warnings
import eventlet
warnings.filterwarnings('ignore')

import requests
from bs4 import BeautifulSoup

try:
    import openpyxl
except ImportError:
    print("请安装 openpyxl: pip install openpyxl")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('step2.log', encoding='utf-8'),
    ]
)
log = logging.getLogger(__name__)

# ================================================================
# CSV 分卷输出（每卷最多 MAX_ROWS_PER_FILE 行）
# ================================================================
CSV_FIELDS = [
    '报刊名', '期数',
    '文章标题', '作者', '文章日期', '版次',
    '文章详情URL',
]

MAX_ROWS_PER_FILE = 100_000        # 每个 CSV 最多数据行数
OUTPUT_CSV_PREFIX = './articles'   # 输出前缀：articles_001.csv, articles_002.csv ...

class CsvWriter:
    """自动分卷 CSV 写入器，每卷不超过 MAX_ROWS_PER_FILE 行。"""

    def __init__(self):
        self._vol    = 0
        self._rows   = 0
        self._fh     = None
        self._writer = None
        self._total  = 0
        self._open_next_vol()

    def _vol_path(self, vol: int) -> str:
        return f'{OUTPUT_CSV_PREFIX}_{vol:03d}.csv'

    def _open_next_vol(self):
        if self._fh:
            self._fh.close()
        self._vol  += 1
        self._rows  = 0
        path = self._vol_path(self._vol)
        self._fh     = open(path, 'w', newline='', encoding='utf-8-sig')
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        self._writer.writeheader()
        log.info(f'📄 新建 CSV 卷：{path}')

    def write(self, rows: list):
        for row in rows:
            if self._rows >= MAX_ROWS_PER_FILE:
                self._open_next_vol()
            self._writer.writerow({k: row.get(k, '') for k in CSV_FIELDS})
            self._rows  += 1
            self._total += 1
        if self._fh:
            self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    @property
    def total(self) -> int:
        return self._total

    @property
    def current_file(self) -> str:
        return self._vol_path(self._vol)


# 全局写入器（在 main() 中初始化）
_csv_writer = None

def append_articles(rows: list):
    """将爬取结果写入分卷 CSV，自动分卷。"""
    global _csv_writer
    if _csv_writer is None:
        _csv_writer = CsvWriter()
    mapped = []
    for row in rows:
        mapped.append({
            '报刊名':      row.get('报刊名', ''),
            '期数':        row.get('日期', ''),
            '文章标题':    row.get('文章标题', ''),
            '作者':        row.get('作者', ''),
            '文章日期':    row.get('日期', ''),
            '版次':        row.get('版次', ''),
            '文章详情URL': row.get('文章详情URL', ''),
        })
    _csv_writer.write(mapped)

# ================================================================
# 进度管理（断点续爬）
# ================================================================
def load_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        try:
            return set(json.loads(open(PROGRESS_FILE, encoding='utf-8').read()))
        except Exception:
            pass
    return set()

def save_progress(done: set):
    with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(done), f, ensure_ascii=False)

# ================================================================
# 一、Selenium 登录（与第一步完全一致）
# ================================================================
def safe_sleep(t):
    with eventlet.Timeout(t, False):
        time.sleep(t)

def make_driver():
    from selenium import webdriver
    if DRIVER_TYPE == 'Chrome':
        opts = webdriver.ChromeOptions()
    elif DRIVER_TYPE == 'Firefox':
        opts = webdriver.FirefoxOptions()
    else:
        opts = webdriver.EdgeOptions()
    opts.add_argument('--ignore-certificate-errors')
    opts.add_argument('--headless')
    opts.add_argument('--log-level=3')
    if DRIVER_TYPE != 'Firefox':
        opts.add_experimental_option('excludeSwitches', ['enable-logging'])
    if DRIVER_TYPE == 'Chrome':
        return webdriver.Chrome(options=opts)
    elif DRIVER_TYPE == 'Firefox':
        return webdriver.Firefox(options=opts)
    return webdriver.Edge(options=opts)

def el(target, method, name):
    try:
        from selenium.webdriver.common.by import By
        return target.find_element(method, name)
    except Exception:
        return None

def se_click(driver, elem):
    driver.execute_script('arguments[0].click();', elem)

def pass_slide(driver):
    import cv2, numpy as np
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By
    try:
        start = time.time()
        while time.time() - start < 15:
            if el(driver, By.CLASS_NAME, 'drag-slide-identity'):
                break
            safe_sleep(0.5)
        target_elem = el(driver, By.CLASS_NAME, 'drag-slide-identity')
        if not target_elem:
            log.info('未检测到滑块，跳过')
            return True
        target_src = target_elem.get_attribute('src')
        bg_wrap    = el(driver, By.CLASS_NAME, 'drag-captcha-bg')
        bg_src     = el(bg_wrap, By.TAG_NAME, 'img').get_attribute('src')
        target_img = cv2.imdecode(
            np.frombuffer(base64.b64decode(target_src.split(',')[-1]), np.uint8),
            cv2.IMREAD_GRAYSCALE)
        bg_img = cv2.imdecode(
            np.frombuffer(base64.b64decode(bg_src.split(',')[-1]), np.uint8),
            cv2.IMREAD_GRAYSCALE)
        result  = cv2.matchTemplate(bg_img, target_img, cv2.TM_CCOEFF_NORMED)
        _, _, _, max_loc = cv2.minMaxLoc(result)
        slide_x = max_loc[0]
        slider = el(driver, By.CLASS_NAME, 'drap-captcha-slidetrigger')
        if slider:
            ActionChains(driver).click_and_hold(slider).perform()
            ActionChains(driver).move_by_offset(slide_x, 0).perform()
            ActionChains(driver).release().perform()
        log.info(f'滑块完成，偏移={slide_x}')
        return True
    except Exception as e:
        log.error(f'滑块失败: {e}')
        return False

def selenium_login() -> dict:
    from selenium.webdriver.common.by import By
    literature_url = f'{CNBKSY_BASE}/literature'
    driver = make_driver()
    try:
        while True:
            driver.get(literature_url)
            safe_sleep(2)
            if os.path.exists(COOKIE_FILE):
                try:
                    cached = json.loads(open(COOKIE_FILE, encoding='utf-8').read())
                    for k, v in cached.items():
                        try:
                            driver.add_cookie({'name': k, 'value': v})
                        except Exception:
                            pass
                    driver.get(literature_url)
                    safe_sleep(2)
                    log.info('已加载缓存 Cookie，尝试免登录')
                except Exception as e:
                    log.warning(f'Cookie 文件加载失败: {e}')
            if el(driver, By.ID, 'user_name'):
                log.info('检测到登录页，开始自动填写账号密码...')
                safe_sleep(1)
                try:
                    username_box = el(driver, By.ID, 'user_name')
                    username_box.clear()
                    username_box.send_keys(USRID)
                    pw_wrap = el(driver, By.CLASS_NAME, 'password-input')
                    pw_box  = el(pw_wrap, By.TAG_NAME, 'input')
                    pw_box.clear()
                    pw_box.send_keys(PASSWORD)
                    se_click(driver, el(driver, By.ID, 'login'))
                    safe_sleep(3)
                    if not pass_slide(driver):
                        log.warning('滑块失败，重试')
                        continue
                    safe_sleep(3)
                    driver.get(literature_url)
                    safe_sleep(2)
                except Exception as e:
                    log.error(f'填写登录表单失败: {e}')
                    continue
            common = el(driver, By.ID, 'common')
            if common and USRNAME in common.text:
                log.info('✅ Selenium 登录成功')
                cookie_dict = {c['name']: c['value'] for c in driver.get_cookies()}
                with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(cookie_dict, f, ensure_ascii=False)
                log.info(f'Cookie 已保存 → {COOKIE_FILE}')
                return cookie_dict
            log.warning('登录验证未通过，重试...')
            safe_sleep(2)
    finally:
        driver.quit()

# ================================================================
# 二、requests Session
# ================================================================
SESSION: requests.Session = None

def make_session(cookie_dict: dict) -> requests.Session:
    global SESSION
    sess = requests.Session()
    sess.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Connection': 'keep-alive',
    })
    sess.verify = False
    sess.cookies.update(cookie_dict)
    SESSION = sess
    log.info(f'requests Session 已创建，注入 {len(cookie_dict)} 个 Cookie')
    return sess

def ensure_login() -> requests.Session:
    """尝试加载 Cookie，失败则重新 Selenium 登录"""
    global SESSION
    if os.path.exists(COOKIE_FILE):
        try:
            cookie_dict = json.loads(open(COOKIE_FILE, encoding='utf-8').read())
            sess = make_session(cookie_dict)
            # 验证 Cookie 有效性
            test_url = f'{CNBKSY_BASE}/literature'
            r = sess.get(test_url, timeout=15)
            if USRNAME in r.text or '用户中心' in r.text:
                log.info('Cookie 有效，免登录')
                return sess
            log.warning('Cookie 已失效，重新登录')
        except Exception as e:
            log.warning(f'Cookie 加载异常: {e}')
    cookie_dict = selenium_login()
    return make_session(cookie_dict)

def safe_get(url: str, **kwargs) -> requests.Response | None:
    for attempt in range(1, MAX_RETRY + 1):
        try:
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
            r = SESSION.get(url, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            wait = BACKOFF_BASE ** attempt
            log.warning(f'  GET 失败(第{attempt}次): {e}，{wait}s 后重试')
            time.sleep(wait)
    return None

def safe_post(url: str, **kwargs) -> requests.Response | None:
    for attempt in range(1, MAX_RETRY + 1):
        try:
            time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
            r = SESSION.post(url, timeout=30, **kwargs)
            r.raise_for_status()
            return r
        except Exception as e:
            wait = BACKOFF_BASE ** attempt
            log.warning(f'  POST 失败(第{attempt}次): {e}，{wait}s 后重试')
            time.sleep(wait)
    return None

# ================================================================
# 三、读取 xlsx 中的报刊 URL
# ================================================================
def load_newspaper_urls() -> list[tuple[str, str]]:
    """
    返回 [(报刊名, 报刊URL), ...]
    报刊名取同行第1列，URL取第3列（URL_COL=2）
    """
    wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
    ws = wb.active
    results = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if HEADER_ROW == 0 and i == 0:
            continue   # 跳过表头
        if row is None or len(row) <= URL_COL:
            continue
        url = str(row[URL_COL]).strip() if row[URL_COL] else ''
        name = str(row[0]).strip() if row[0] else ''
        if url and url.startswith('http'):
            # 强制使用 v1 版本页面（v2 格式完全不同）
            if 'skipVersion' not in url:
                url = url.rstrip('/') + '?skipVersion=v1'
            results.append((name, url))
    log.info(f'从 xlsx 读到 {len(results)} 条报刊 URL')
    return results

# ================================================================
# 四、Selenium 驱动：打开报刊页 → 点「报内检索」→ 抓文章
# ================================================================
# 核心思路（参考原始爬虫代码）：
#   1. Selenium 打开报刊主页（v1版本）
#   2. 点击「报内检索」按钮，等待新标签页打开
#   3. 等待 resultRow 出现（页面加载完成）
#   4. 从 rt_1 区域逐行解析文章信息
#   5. 翻页：点击「下页」按钮，等待内容刷新
# ================================================================

DRIVER = None   # 全局 Selenium WebDriver 实例

def get_driver():
    """获取或创建全局 Selenium WebDriver（带 cookie）"""
    global DRIVER
    if DRIVER is not None:
        return DRIVER

    if DRIVER_TYPE == 'Chrome':
        from selenium.webdriver import Chrome
        from selenium.webdriver.chrome.options import Options
    elif DRIVER_TYPE == 'Firefox':
        from selenium.webdriver import Firefox as Chrome
        from selenium.webdriver.firefox.options import Options
    else:
        from selenium.webdriver import Edge as Chrome
        from selenium.webdriver.edge.options import Options

    opts = Options()
    opts.add_argument('--headless')
    opts.add_argument('--ignore-certificate-errors')
    opts.add_argument('--disable-gpu')
    opts.add_argument('log-level=3')
    if DRIVER_TYPE == 'Chrome':
        opts.add_experimental_option('excludeSwitches', ['enable-logging'])

    DRIVER = Chrome(options=opts)

    # 注入 Cookie
    DRIVER.get(WEBVPN_BASE)
    time.sleep(1)
    if os.path.exists(COOKIE_FILE):
        try:
            cookies = json.loads(open(COOKIE_FILE, encoding='utf-8').read())
            for ck in cookies:
                try:
                    DRIVER.add_cookie(ck)
                except Exception:
                    pass
            log.info('Selenium: Cookie 注入完成')
        except Exception as e:
            log.warning(f'Selenium: Cookie 注入失败: {e}')

    return DRIVER


def sel_get(driver, method, name):
    """安全 find_element，找不到返回 None"""
    from selenium.webdriver.common.by import By
    try:
        return driver.find_element(method, name)
    except Exception:
        return None


def sel_gets(driver, method, name):
    """安全 find_elements，找不到返回 []"""
    from selenium.webdriver.common.by import By
    try:
        return driver.find_elements(method, name) or []
    except Exception:
        return []


def wait_for(driver, method, name, timeout=15, interval=0.5):
    """等待元素出现，超时返回 None"""
    from selenium.webdriver.common.by import By
    start = time.time()
    while time.time() - start < timeout:
        el = sel_get(driver, method, name)
        if el is not None:
            return el
        time.sleep(interval)
    return None


def parse_articles_from_driver(driver, newspaper_name: str, newspaper_url: str) -> list[dict]:
    """
    从当前页面的 #rt_1 区域解析所有 resultRow，
    提取：标题、作者、日期、版次、文章详情URL
    """
    from selenium.webdriver.common.by import By
    articles = []

    rt1 = sel_get(driver, By.ID, 'rt_1')
    if rt1 is None:
        log.warning('    parse_articles: 未找到 #rt_1')
        return articles

    rows = sel_gets(rt1, By.CLASS_NAME, 'resultRow')
    for row in rows:
        try:
            tds = sel_gets(row, By.TAG_NAME, 'td')
            if not tds or len(tds) < 2:
                continue

            # 标题 & URL：第2个td的第一个<a>
            title_a = sel_get(tds[1], By.TAG_NAME, 'a')
            if title_a is None:
                continue
            title = title_a.get_attribute('textContent').strip()
            detail_href = title_a.get_attribute('href') or ''
            # href 可能是相对路径，补全
            if detail_href.startswith('/'):
                detail_url = WEBVPN_BASE + detail_href
            else:
                detail_url = detail_href

            # 作者、日期、版次：从 div.fr 的文字中解析
            author = ''
            date_str = ''
            edition = ''
            try:
                fr_div = tds[1].find_element('class name', 'fr')
            except Exception:
                fr_div = None

            if fr_div:
                # 作者：onclick="searchAuthor('XXX')"，取第一个非空的
                author_links = sel_gets(fr_div, By.XPATH,
                    ".//a[contains(@onclick,'searchAuthor')]")
                for al in author_links:
                    txt = al.get_attribute('textContent').strip()
                    if txt:
                        author = txt
                        break

                fr_text = fr_div.get_attribute('textContent') or ''
                # 日期：YYYY 年 M 月 D 日
                dm = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', fr_text)
                if dm:
                    date_str = f'{dm.group(1)}年{dm.group(2)}月{dm.group(3)}日'
                # 版次：[0002 版]
                em = re.search(r'\[\s*(\d+)\s*版\s*\]', fr_text)
                if em:
                    edition = em.group(1).lstrip('0') or '0'

            if title:
                articles.append({
                    '报刊名':     newspaper_name,
                    '报刊URL':    newspaper_url,
                    '文章标题':   title,
                    '作者':       author,
                    '日期':       date_str,
                    '版次':       edition,
                    '文章详情URL': detail_url,
                })
        except Exception as e:
            log.debug(f'    parse row 异常: {e}')
            continue

    return articles


def click_next_page(driver, current_page: int) -> bool:
    """
    点击翻页，返回 True 表示成功翻到下一页，False 表示已是末页。
    参考原始代码的 next_page() 逻辑。
    """
    from selenium.webdriver.common.by import By
    try:
        paging = sel_get(driver, By.ID, 'paging1')
        if paging is None:
            return False
        lis = sel_gets(paging, By.TAG_NAME, 'li')
        for li in lis:
            if '下页' in (li.get_attribute('textContent') or ''):
                if 'disabled' in (li.get_attribute('class') or ''):
                    return False   # 已是末页
                a = sel_get(li, By.TAG_NAME, 'a')
                if a is None:
                    return False
                data_page = a.get_attribute('data-page')
                if data_page and int(data_page) == current_page:
                    return False   # 按钮页码等于当前页，说明末页
                driver.execute_script('arguments[0].click();', a)
                return True
        return False
    except Exception as e:
        log.debug(f'    click_next_page 异常: {e}')
        return False


def scrape_newspaper_via_selenium(np_url: str, np_name: str) -> list[dict]:
    """
    用 Selenium 爬取一个报刊的所有文章：
    1. 打开报刊主页
    2. 点「报内检索」→ 新标签页
    3. 等待加载，逐页抓文章
    """
    from selenium.webdriver.common.by import By

    driver = get_driver()
    all_articles = []

    # 打开报刊主页
    log.info(f'  Selenium: 打开 {np_url}')
    driver.get(np_url)
    time.sleep(REQUEST_DELAY)

    ori_handles = driver.window_handles
    ori_handle  = driver.current_window_handle

    # 找「报内检索」按钮并点击
    search_btn = None
    for btn in sel_gets(driver, By.CLASS_NAME, 'buttonLink'):
        if '内检索' in (btn.get_attribute('textContent') or ''):
            search_btn = btn
            break

    if search_btn is None:
        log.warning('  Selenium: 未找到「报内检索」按钮')
        return all_articles

    driver.execute_script('arguments[0].click();', search_btn)
    log.info('  Selenium: 已点击「报内检索」，等待新标签页...')

    # 等待新标签页打开
    start = time.time()
    while time.time() - start < 15:
        if len(driver.window_handles) > len(ori_handles):
            break
        time.sleep(0.5)

    # 切换到新标签页
    new_handle = None
    for h in driver.window_handles:
        if h not in ori_handles:
            new_handle = h
            break
    if new_handle is None:
        log.warning('  Selenium: 未检测到新标签页')
        return all_articles

    driver.switch_to.window(new_handle)
    log.info(f'  Selenium: 已切换到新标签页 {driver.current_url}')

    # 等待 resultRow 出现（页面加载完成）
    log.info('  Selenium: 等待文章列表加载...')
    result_row = wait_for(driver, By.CLASS_NAME, 'resultRow', timeout=30)
    if result_row is None:
        log.warning('  Selenium: 等待超时，未找到 resultRow')
        driver.close()
        driver.switch_to.window(ori_handle)
        return all_articles

    time.sleep(1)   # 额外等待确保渲染完成

    # 逐页抓取
    page = 1
    while True:
        arts = parse_articles_from_driver(driver, np_name, np_url)
        log.info(f'    第 {page} 页，解析到 {len(arts)} 篇')
        all_articles.extend(arts)

        if not arts:
            break

        # 获取总页数
        total_pages = 1
        count_el = sel_get(driver, By.ID, 'currentCount1')
        # 尝试翻页
        has_next = click_next_page(driver, page)
        if not has_next:
            break

        page += 1
        # 等待页面内容更新：等 currentCount1 文字变化
        expected_start = str((page - 1) * PAGE_SIZE + 1)
        start_wait = time.time()
        while time.time() - start_wait < 15:
            el = sel_get(driver, By.ID, 'currentCount1')
            if el and expected_start in (el.get_attribute('textContent') or ''):
                break
            time.sleep(0.5)
        time.sleep(0.5)

        if TEST_MODE and TEST_ISSUE_N > 0 and page > TEST_ISSUE_N:
            log.info(f'  [测试模式] 已达 {TEST_ISSUE_N} 页上限，停止翻页')
            break

    log.info(f'  Selenium: 共抓取 {len(all_articles)} 篇')
    driver.close()
    driver.switch_to.window(ori_handle)
    return all_articles




# ================================================================
# 主程序
# ================================================================
def main():
    import urllib3
    urllib3.disable_warnings()

    global _csv_writer
    _csv_writer = CsvWriter()
    done_set = load_progress()

    # ---- 登录 ----
    sess = ensure_login()

    # ---- 读取报刊列表 ----
    newspapers = load_newspaper_urls()
    if not newspapers:
        log.error(f'xlsx 中未读到任何 URL，请检查 XLSX_PATH={XLSX_PATH} 和 URL_COL={URL_COL}')
        sys.exit(1)

    try:
        for idx, (name, np_url) in enumerate(newspapers, 1):
            # 测试模式：只爬前 TEST_NEWSPAPER_N 个报刊
            if TEST_MODE and idx > TEST_NEWSPAPER_N:
                log.info(f'[测试模式] 已达 {TEST_NEWSPAPER_N} 个报刊上限，停止')
                break
            log.info(f'\n{"="*60}')
            log.info(f'[{idx}/{len(newspapers)}] {name} | {np_url}')

            # 断点续爬
            if np_url in done_set:
                log.info('  ✅ 已爬过，跳过')
                continue

            # ---- Selenium 爬取该报刊所有文章 ----
            articles = scrape_newspaper_via_selenium(np_url, name)

            np_article_count = len(articles)
            if articles:
                append_articles(articles)
                log.info(f'  ✅ 报刊 [{name}] 完成，共 {np_article_count} 篇，全局累计 {_csv_writer.total} 篇')
            else:
                log.warning(f'  ⚠️ 报刊 [{name}] 未抓到任何文章')
            done_set.add(np_url)
            save_progress(done_set)

    finally:
        _csv_writer.close()

    log.info(f'\n🎉 全部完成！共写入 {_csv_writer.total} 篇文章，'
             f'输出至 {OUTPUT_CSV_PREFIX}_001.csv ~ {_csv_writer.current_file}')


if __name__ == '__main__':
    main()