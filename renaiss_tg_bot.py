import os
import json
import logging
import time
import sqlite3
import cloudscraper
import concurrent.futures
from urllib.parse import quote
from datetime import datetime

from dotenv import load_dotenv
from web3 import Web3
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- 基礎配置 ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RPC_URL = "https://bsc-dataseed.binance.org/"
REGISTRY_ADDR = "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30"
DB_NAME = "renaiss_cache.db"

# 初始化日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
scraper = cloudscraper.create_scraper()
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# 載入 ABI
abi_path = os.path.join(os.path.dirname(__file__), "registry_abi.json")
with open(abi_path, "r") as f:
    contract = w3.eth.contract(address=w3.to_checksum_address(REGISTRY_ADDR), abi=json.load(f))

# --- 1. 資料庫工具 ---

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS assets (
            token_id TEXT PRIMARY KEY,
            name TEXT,
            fmv REAL,
            last_updated TEXT
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_tid ON assets (token_id)')
    conn.commit()
    conn.close()

def save_assets_batch(items):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item in items:
        fmv_raw = item.get('fmvPriceInUSD')
        if fmv_raw is None:
            fmv_raw = item.get('fmv', 0)
        
        cursor.execute('''
            INSERT OR REPLACE INTO assets (token_id, name, fmv, last_updated)
            VALUES (?, ?, ?, ?)
        ''', (str(item['tokenId']), item['name'], int(fmv_raw) / 100, now))
    conn.commit()
    conn.close()

def escape_md(text):
    """轉義 MarkdownV2 保留字元"""
    if text is None: return ""
    reserved_chars = r'_*[]()~`>#+-=|{}.!'
    text = str(text)
    for char in reserved_chars:
        text = text.replace(char, "\\" + char)
    return text

# --- 2. API 與同步邏輯 ---

async def sync_market_task():
    """背景排程：每小時更新全市場價格"""
    logging.info("⏰ 開始同步市場價格...")
    base_url = "https://www.renaiss.xyz/api/trpc/collectible.list"
    offset, limit = 0, 50
    total = 0
    headers = {'x-trpc-source': 'react', 'User-Agent': 'Mozilla/5.0'}

    while True:
        payload = {"0": {"json": {"limit": limit, "offset": offset}}}
        input_str = quote(json.dumps(payload, separators=(',', ':')))
        url = "{}?batch=1&input={}".format(base_url, input_str)
        try:
            resp = scraper.get(url, headers=headers, timeout=20)
            if resp.status_code != 200: break
            collection = resp.json()[0]['result']['data']['json'].get('collection', [])
            if not collection: break
            save_assets_batch(collection)
            total += len(collection)
            offset += limit
            time.sleep(0.5)
        except Exception as e:
            logging.error("同步中斷: {}".format(e))
            break
    logging.info("✅ 同步完成，共更新 {} 筆資產數據。".format(total))

def fetch_single_detail(token_id):
    """補償查詢：DB 找不到時查 API"""
    url = "https://www.renaiss.xyz/api/trpc/collectible.detail"
    p = {"0": {"json": {"tokenId": str(token_id)}}}
    input_str = quote(json.dumps(p, separators=(',', ':')))
    try:
        resp = scraper.get("{}?batch=1&input={}".format(url, input_str), timeout=10)
        raw = resp.json()[0]['result']['data']['json']
        if raw:
            save_assets_batch([raw])
            return raw
    except: pass
    return None

# --- 3. 機器人對話邏輯 ---

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if not w3.is_address(addr):
        await update.message.reply_text("❌ 地址格式無效。")
        return

    status_msg = await update.message.reply_text("🔎 正在檢索資產價值...")
    
    try:
        c_addr = w3.to_checksum_address(addr)
        balance = contract.functions.balanceOf(c_addr).call()
        if balance == 0:
            await status_msg.edit_text("📭 此地址未持有資產。")
            return

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            token_ids = list(ex.map(lambda i: contract.functions.tokenOfOwnerByIndex(c_addr, i).call(), range(balance)))

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        placeholders = ','.join(['?'] * len(token_ids))
        cursor.execute("SELECT token_id, name, fmv FROM assets WHERE token_id IN ({})".format(placeholders), [str(tid) for tid in token_ids])
        rows = cursor.fetchall()
        db_results = {int(row[0]): {"name": row[1], "fmv": row[2]} for row in rows}
        conn.close()

        report = [f"📊 *Renaiss 資產報告*\n`{escape_md(addr[:6])}...{escape_md(addr[-4:])}`\n"]
        total_val = 0.0

        for tid in sorted(token_ids):
            item = db_results.get(tid)
            url = f"https://www.renaiss.xyz/card/{tid}"
            
            if not item:
                item_raw = fetch_single_detail(tid)
                if item_raw:
                    item = {"name": item_raw['name'], "fmv": int(item_raw.get('fmvPriceInUSD', 0)) / 100}

            if item:
                total_val += item['fmv']
                name_md = escape_md(item['name'])
                formatted_price = "{:,.2f}".format(item['fmv'])
                price_md = escape_md("${}".format(formatted_price))
                report.append(f"💎 [*{name_md}*]({url})  `{price_md}`")
            else:
                tid_md = escape_md(str(tid)[-6:])
                report.append(f"❓ [資產 \#{tid_md}]({url}) ` 未獲取價格`")

        total_price_md = escape_md("${:,.2f}".format(total_val))
        report.append(f"\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n💰 *總計估值: {total_price_md}*")
        
        await update.message.reply_text("\n".join(report), parse_mode='MarkdownV2', disable_web_page_preview=True)
        await status_msg.delete()

    except Exception as e:
        logging.error("Handle error: {}".format(e))
        await update.message.reply_text("❌ 錯誤: {}".format(e))

# --- 4. 啟動進入點 ---

async def post_init(application: Application):
    """在異步事件迴圈啟動後，初始化排程任務"""
    scheduler = AsyncIOScheduler()
    # 啟動時立刻執行一次，之後每 60 分鐘跑一次
    scheduler.add_job(sync_market_task, 'interval', minutes=60, next_run_time=datetime.now())
    scheduler.start()
    logging.info("⏰ 背景同步排程已啟動。")

def main():
    init_db()
    
    # 使用 post_init 來掛載異步排程器，解決 Event Loop 報錯
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("👋 歡迎！請傳送地址開始掃描。")))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))
    
    logging.info("🚀 機器人啟動中...")
    app.run_polling()

if __name__ == "__main__":
    main()