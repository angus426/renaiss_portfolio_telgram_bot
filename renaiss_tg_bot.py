import os
import json
import logging
import time
import requests
import cloudscraper
from dotenv import load_dotenv
from web3 import Web3
from urllib.parse import quote
import concurrent.futures
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from typing import List, Dict, Set

# --- 載入配置 ---
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
RPC_URL = "https://bsc-dataseed.binance.org/"
REGISTRY_ADDR = "0xF8646A3Ca093e97Bb404c3b25e675C0394DD5b30"

# 初始化
logging.basicConfig(level=logging.INFO)
scraper = cloudscraper.create_scraper() # 使用 cloudscraper 避免 307 跳轉
w3 = Web3(Web3.HTTPProvider(RPC_URL))

with open(os.path.join(os.path.dirname(__file__), "registry_abi.json"), "r") as f:
    contract = w3.eth.contract(address=w3.to_checksum_address(REGISTRY_ADDR), abi=json.load(f))

# --- 核心邏輯：依照 TokenID 找 ItemID ---

# --- 核心邏輯：優化後的批量處理 ---

def get_token_id_at_index(args):
    """
    用於並行抓取 Token ID 的輔助函數
    args: (contract_instance, owner_address, index)
    """
    contract, owner, idx = args
    try:
        return contract.functions.tokenOfOwnerByIndex(owner, idx).call()
    except Exception as e:
        print(f"Error fetching token at index {idx}: {e}")
        return None

def get_all_user_tokens(owner_address, balance) -> List[int]:
    """
    並行抓取用戶所有 Token ID
    """
    token_ids = []
    # 最多 10 個線程並行查詢 RPC (避免觸發 Rate Limit)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(get_token_id_at_index, (contract, owner_address, i)) 
            for i in range(balance)
        ]
        
        for future in concurrent.futures.as_completed(futures):
            tid = future.result()
            if tid is not None:
                token_ids.append(tid)
                
    return sorted(token_ids)

def scan_market_for_tokens(target_token_ids: List[int]) -> Dict[int, dict]:
    """
    單次掃描市場，尋找目標 Token ID 列表中的任何物品
    回傳: { token_id: {name, fmv, itemId...} }
    """
    base_url = "https://www.renaiss.xyz/api/trpc/collectible.list"
    
    # 轉成 Set 加速查詢
    target_set = set(str(t) for t in target_token_ids)
    found_items = {} # { token_id (int): data }
    
    offset = 0
    limit = 50
    
    headers = {
        'x-trpc-source': 'react',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Referer': 'https://www.renaiss.xyz/market'
    }

    print(f"開始掃描市場，目標尋找 {len(target_set)} 個資產...")

    while True:
        # 如果所有目標都找到了，就提前結束
        if len(found_items) >= len(target_token_ids):
            print("所有目標資產已找到，停止掃描。")
            break

        payload = {"0": {"json": {"limit": limit, "offset": offset}}}
        input_str = quote(json.dumps(payload, separators=(',', ':')))
        url = f"{base_url}?batch=1&input={input_str}"
        
        try:
            response = scraper.get(url, headers=headers, timeout=20)
            
            if response.status_code != 200:
                print(f"API 拒絕請求 (Status {response.status_code})")
                break

            if not response.text:
                break

            res = response.json()
            data_json = res[0].get('result', {}).get('data', {}).get('json', {})
            collection = data_json.get('collection', [])
            
            if not collection:
                print("已無更多市場資料 (End of list)")
                break

            # 檢查這一頁有沒有我們的人
            for item in collection:
                tid_str = str(item.get('tokenId'))
                
                if tid_str in target_set:
                    # 找到了！記下來
                    tid_int = int(tid_str)
                    found_items[tid_int] = {
                        "success": True,
                        "itemId": item.get('itemId'),
                        "name": item.get('name'),
                        "fmv": int(item.get('fmvPriceInUSD', 0)) / 100
                    }
                    print(f"✅ 找到資產: {item.get('name')} (ID: {tid_str})")

            offset += limit
            # 小睡一下避免被鎖
            time.sleep(0.5) 
            
        except Exception as e:
            print(f"掃描市場時發生錯誤: {e}")
            break
            
    return found_items

# --- Bot 處理邏輯 ---

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.message.text.strip()
    if not w3.is_address(addr):
        await update.message.reply_text("❌ 地址格式無效。")
        return

    status_msg = await update.message.reply_text("🔍 正在進行計算...")
    
    try:
        checksum_addr = w3.to_checksum_address(addr)
        balance = contract.functions.balanceOf(checksum_addr).call()
        
        if balance == 0:
            await status_msg.edit_text("📭 此地址未持有資產。")
            return

        # ---------------------------------------------------------
        # 優化步驟 1: 並行抓取所有 Token ID
        # ---------------------------------------------------------
        await status_msg.edit_text(f"⚡ 正在並行讀取 {balance} 個資產 ID...")
        user_token_ids = get_all_user_tokens(checksum_addr, balance)
        
        if not user_token_ids:
            await status_msg.edit_text("⚠️ 無法讀取資產 ID，請稍後再試。")
            return

        # ---------------------------------------------------------
        # 優化步驟 2: 單次掃描市場
        # ---------------------------------------------------------
        await status_msg.edit_text(f"🔍 正在掃描市場 (目標: {len(user_token_ids)} 個資產)...\n這可能需要一點時間！")
        
        # 執行批量掃描
        # 這會回傳一個 Dict: { id: {name, fmv...} }
        market_data_map = scan_market_for_tokens(user_token_ids)
        
        # ---------------------------------------------------------
        # 優化步驟 3: 產生美化報表 (MarkdownV2)
        # ---------------------------------------------------------
        report_lines = []
        total_fmv = 0.0
        
        for tid in user_token_ids:
            item_data = market_data_map.get(tid)
            card_url = f"https://www.renaiss.xyz/card/{tid}"
            
            if item_data:
                fmv = item_data['fmv']
                total_fmv += fmv
                # 使用連結格式 [名稱](網址)，名稱部分加粗
                # 價格部分使用代碼格式讓數字對齊
                name = item_data['name'].replace('-', '\\-') # 簡單轉義
                line = f"💎 [*{name}*]({card_url})\n" \
                       f"   `估值: ${fmv:,.2f}`"
                report_lines.append(line)
            else:
                # 縮短 ID 顯示並加上超連結
                tid_short = str(tid)[-6:]
                report_lines.append(f"❓ [資產 \#{tid_short}]({card_url})\n   `狀態: 市場未尋獲`")

        # 組合最終訊息
        header = f"📊 *Renaiss 帳戶資產*\n`地址: {addr[:6]}...{addr[-4:]}`\n"
        divider = "⎯" * 15 
        footer = f"\n{divider}\n💰 *資產總估值: ${total_fmv:,.2f}*"
        
        # 組合與分段發送 (處理 Telegram 4096 字元限制)
        full_text = header + "\n" + "\n".join(report_lines) + footer
        
        if len(full_text) > 4000:
            # 簡易防爆處理
            await update.message.reply_text(header + f"\n(共 {len(report_lines)} 筆資料，僅顯示摘要)\n" + footer, parse_mode='Markdown')
        else:
            await update.message.reply_text(full_text, parse_mode='Markdown')
            
        await status_msg.delete()

    except Exception as e:
        await update.message.reply_text(f"❌ 錯誤: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 請貼上錢包地址，我將為您執行掃描與 FMV 加總。")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))
    print("Bot 已啟動...")
    app.run_polling()

if __name__ == "__main__":
    main()