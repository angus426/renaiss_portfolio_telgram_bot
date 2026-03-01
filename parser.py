def analyze_renaiss_metadata(metadata_json):
    """
    專門針對 Renaiss RWA NFT 的解析器
    """
    # 1. 提取基本資訊
    full_name = metadata_json.get("name", "Unknown Card")
    image_url = metadata_json.get("image", "")
    
    # 2. 提取屬性 (從 attributes list 轉為 dict 方便查詢)
    attrs = {attr['trait_type']: attr['value'] for attr in metadata_json.get("attributes", [])}
    
    # 3. 核心數據清洗
    # 從 "PSA 9 Mint 2014 Pokemon..." 中提取更簡潔的標題
    # 或是直接組合 attributes
    short_name = full_name.split("2014")[-1].strip() if "2014" in full_name else full_name
    
    grade = attrs.get("Grade", "N/A")
    card_set = attrs.get("Set", "N/A")
    serial = attrs.get("Serial", "N/A")
    year = attrs.get("Year", "N/A")
    lang = attrs.get("Language", "N/A")

    return {
        "display_title": f"{year} {short_name}",
        "grade": grade,
        "set": card_set,
        "serial": serial,
        "language": lang,
        "image": image_url,
        "raw_name": full_name
    }

# --- 模擬測試 ---
# data = (你貼的那段 JSON)
# info = analyze_renaiss_metadata(data)
# print(f"卡片：{info['display_title']}")  # 輸出: 2014 Pokemon Japanese Xy Promo 68 Pikachu Outbreak!
# print(f"等級：{info['grade']}")           # 輸出: 9 Mint