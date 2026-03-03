import sys
import os
import matplotlib
import datetime
import warnings
import hashlib
import io
warnings.filterwarnings("ignore", category=UserWarning, module="pandas")
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False
import tushare as ts
import pandas as pd
import numpy as np
from collections import defaultdict
import streamlit as st

# Tushare 初始化
ts.set_token('TUSHARE_TOKEN')
pro = ts.pro_api()

# ───────────── 函数定义（核心逻辑不变） ─────────────

def fetch_stock_data(ts_code, start_date, end_date):
    try:
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df.empty:
            raise ValueError("没有获取到任何数据，请检查日期范围或Tushare积分")
        df['trade_date'] = pd.to_datetime(df['trade_date'], format='%Y%m%d')
        df = df.sort_values('trade_date').reset_index(drop=True)
        return df[['trade_date', 'open', 'high', 'low', 'close', 'vol']]
    except Exception as e:
        err_str = str(e).lower()
        if '积分' in err_str or '权限' in err_str:
            raise RuntimeError("❌ Tushare积分不足！日线只需120积分（注册后完善个人信息即可）。")
        raise RuntimeError(f"数据获取失败: {str(e)}")

def calculate_tpo(df):
    df = df.copy()
    for i, idx in enumerate(df.index):
        df.loc[idx, 'letter'] = chr(65 + i)
    min_low = df['low'].min()
    max_high = df['high'].max()
    price_range = max_high - min_low
    step = 0.01 if price_range <= 5 else 0.05 if price_range <= 20 else 0.1 if price_range <= 100 else 0.5
    price_levels = np.arange(np.floor(min_low / step) * step, np.ceil(max_high / step) * step + step, step)
    price_levels = np.unique(np.round(price_levels, 2))
    profile = defaultdict(list)
    for _, row in df.iterrows():
        letter = row['letter']
        low, high = row['low'], row['high']
        for p in price_levels:
            if low <= p <= high:
                profile[p].append(letter)
            if p > high:
                break
    return df, dict(profile)

def get_value_area(profile):
    if not profile:
        return None, None, None
    price_counts = {p: len(letters) for p, letters in profile.items() if letters}
    if not price_counts:
        return None, None, None
    poc_price = max(price_counts, key=price_counts.get)
    total_tpo = sum(price_counts.values())
    target = total_tpo * 0.7
    sorted_prices = sorted(price_counts.keys())
    poc_idx = sorted_prices.index(poc_price)
    cum_tpo = price_counts[poc_price]
    left_idx, right_idx = poc_idx - 1, poc_idx + 1
    while cum_tpo < target and (left_idx >= 0 or right_idx < len(sorted_prices)):
        if left_idx < 0:
            cum_tpo += price_counts[sorted_prices[right_idx]]
            right_idx += 1
        elif right_idx >= len(sorted_prices):
            cum_tpo += price_counts[sorted_prices[left_idx]]
            left_idx -= 1
        else:
            left_c = price_counts[sorted_prices[left_idx]]
            right_c = price_counts[sorted_prices[right_idx]]
            if left_c >= right_c:
                cum_tpo += left_c
                left_idx -= 1
            else:
                cum_tpo += right_c
                right_idx += 1
    val = sorted_prices[left_idx + 1] if left_idx + 1 < len(sorted_prices) else sorted_prices[0]
    vah = sorted_prices[right_idx - 1] if right_idx - 1 >= 0 else sorted_prices[-1]
    return poc_price, vah, val

def plot_market_profile(df, profile, ts_code, start_date, end_date):
    if not profile:
        raise RuntimeError("没有有效TPO数据")
    prices_list = sorted(profile.keys(), reverse=True)
    counts_list = [len(profile[p]) for p in prices_list]
    start_d = df['trade_date'].dt.date.min().strftime('%Y-%m-%d')
    end_d = df['trade_date'].dt.date.max().strftime('%Y-%m-%d')
    fig = plt.figure(figsize=(18, max(12, len(prices_list) * 0.38)))
    ax = fig.add_subplot(111)
    bar_height = (prices_list[0] - prices_list[1]) * 0.92 if len(prices_list) > 1 else 0.25
    ax.barh(prices_list, counts_list, color='skyblue', edgecolor='navy', height=bar_height, linewidth=1.1)
    ax.set_title(f'日线 四度空间 Market Profile - {ts_code}\n{start_d} ~ {end_d}', fontsize=18, pad=30)
    ax.set_xlabel('TPO Count（交易日触及次数）', fontsize=14)
    ax.set_ylabel('Price Level', fontsize=14)
    max_count = max(counts_list) if counts_list else 1
    for i, p in enumerate(prices_list):
        letters_str = ''.join(profile[p])
        if letters_str:
            fs = 11 if len(letters_str) > 12 else 13
            ax.text(counts_list[i] + max_count * 0.05, p, letters_str,
                    va='center', ha='left', fontsize=fs, color='navy', fontweight='extra bold')
    poc, vah, val = get_value_area(profile)
    if poc is not None:
        ax.axhline(y=poc, color='red', linestyle='--', linewidth=3.5, label=f'POC {poc:.2f}')
        ax.axhline(y=vah, color='lime', linestyle='--', linewidth=2.5, label=f'VAH {vah:.2f}')
        ax.axhline(y=val, color='lime', linestyle='--', linewidth=2.5, label=f'VAL {val:.2f}')
        ax.text(max_count * 1.15, poc, f'POC {poc:.2f}', va='center', ha='left',
                color='darkred', fontsize=14, fontweight='bold')
        ax.text(max_count * 1.15, vah, f'VAH {vah:.2f}', va='center', ha='left',
                color='forestgreen', fontsize=12, fontweight='bold')
        ax.text(max_count * 1.15, val, f'VAL {val:.2f}', va='center', ha='left',
                color='forestgreen', fontsize=12, fontweight='bold')
    ax.legend(loc='upper right', fontsize=12)
    ax.grid(axis='x', linestyle='--', alpha=0.7)
    ax.set_xlim(0, max_count * 1.85)
    watermark_text = "Willmat"
    fig.text(0.98, 0.02, watermark_text, fontsize=20, color='gray', alpha=0.4,
             ha='right', va='bottom', rotation=0, fontweight='bold')
    plt.tight_layout()
  
    img_bytes = io.BytesIO()
    plt.savefig(img_bytes, dpi=600, bbox_inches='tight', format='png')
    img_bytes.seek(0)
    plt.close()
  
    return img_bytes, poc or 0, vah or 0, val or 0

def export_to_excel(profile, poc, vah, val, ts_code, start_date, end_date):
    data = []
    data.append(["股票代码", ts_code])
    data.append(["开始日期", start_date])
    data.append(["结束日期", end_date])
    data.append(["POC", f"{poc:.2f}" if poc else "N/A"])
    data.append(["VAH", f"{vah:.2f}" if vah else "N/A"])
    data.append(["VAL", f"{val:.2f}" if val else "N/A"])
    data.append([]) 
    data.append(["价格水平", "TPO计数", "出现的字母"])
    sorted_prices = sorted(profile.keys(), reverse=True)
    for p in sorted_prices:
        count = len(profile[p])
        letters = ''.join(profile[p])
        data.append([f"{p:.2f}", count, letters])
    df_excel = pd.DataFrame(data)
    
    excel_bytes = io.BytesIO()
    df_excel.to_excel(excel_bytes, index=False, header=False)
    excel_bytes.seek(0)
    
    return excel_bytes

def generate_tpo_image(ts_code, start_date, end_date):
    try:
        stock_data = fetch_stock_data(ts_code, start_date, end_date)
        if stock_data is None or stock_data.empty:
            raise ValueError("获取的数据为空")
        stock_data, profile = calculate_tpo(stock_data)
        img_bytes, poc, vah, val = plot_market_profile(stock_data, profile, ts_code, start_date, end_date)
        excel_bytes = export_to_excel(profile, poc, vah, val, ts_code, start_date, end_date)
        return img_bytes, poc, vah, val, excel_bytes
    except Exception as e:
        raise RuntimeError(str(e))

# ───────────── 主界面 ─────────────
st.title("日线 TPO 四度空间 生成器")
st.caption("专业 Market Profile | POC/VAH/VAL 一键生成高清图 + Excel")

code = st.text_input("股票代码", value="000001.SZ", help="示例：600519.SH 或 000001.SZ")
start = st.text_input("开始日期", value="20260101", help="格式 YYYYMMDD")
end = st.text_input("结束日期", value=datetime.date.today().strftime("%Y%m%d"), help="格式 YYYYMMDD")

if st.button("生成高清 TPO 图", type="primary", use_container_width=True):
    if not (code.strip() and len(start)==8 and len(end)==8 and start.isdigit() and end.isdigit()):
        st.error("请完整填写代码和日期（8位数字 YYYYMMDD）")
        st.stop()

    # 必须有解锁码才能生成
    if "unlock_code" not in st.session_state or st.session_state.unlock_code.strip() != "tpo20260303":
        st.error("请先输入正确的解锁码才能使用生成功能")
        st.stop()

    with st.spinner("获取数据 → 计算TPO → 绘图（可能10-60秒，视日期跨度）..."):
        try:
            img_bytes, poc, vah, val, excel_bytes = generate_tpo_image(code, start, end)
            
            st.success(f"生成完成！POC {poc:.2f} | VAH {vah:.2f} | VAL {val:.2f}")
            
            st.info("图片像素较大（高清600dpi），建议直接下载查看（推荐用图片查看器打开）。")
            
            st.download_button(
                label="下载高清 TPO 图片 (.png)",
                data=img_bytes,
                file_name=f"{start}_{end}_{code}_TPO.png",
                mime="image/png",
                use_container_width=True
            )
            
            st.download_button(
                label="下载 Excel 结果 (.xlsx)",
                data=excel_bytes,
                file_name=f"{start}_{end}_{code}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        
        except Exception as e:
            st.error(f"生成失败：{str(e)}")
            st.info("常见原因：日期跨度太长导致图太高 → 尝试缩短日期范围，或联系我优化。")

# 解锁码输入区（现在是必须的入口）
st.markdown("### 输入解锁码使用工具")
code_input = st.text_input("解锁码", type="password", key="unlock_input")
if st.button("验证解锁码"):
    if code_input.strip() == "tpo20260303":  # ← 每天手动修改这个口令
        st.session_state.unlock_code = code_input.strip()
        st.success("解锁成功！现在可以生成 TPO 图了～")
        st.rerun()  # 刷新页面以更新状态
    else:
        st.error("解锁码错误，请联系获取正确码")

# 付费引导（始终显示，帮助获取解锁码）
st.markdown("---")
st.markdown("**未解锁或解锁码过期？**")
st.markdown("""
- **单次**：5元 / 次  
- **包月**：200元 / 30天无限次  

请微信扫码付款，付款备注：  
「TPO + 示例股票代码 + 微信昵称」
""")

st.image("static/QRcode.png", caption="微信扫码付款（支持红包/转账）", width=300)

st.markdown("""
付款成功后截图发微信：**你的微信号**  
我会在几分钟到半小时内给你最新解锁码。
""")

st.caption("数据来源于Tushare | 仅供学习交流 | 如有疑问加微信：你的微信号")
