import sys
import os
import matplotlib
import datetime
import warnings
import hashlib
import io
import json
from datetime import timedelta
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
import psycopg2
from psycopg2.pool import SimpleConnectionPool

# ═══════════════ 数据库配置 ═══════════════

# 从 Streamlit Secrets 读取数据库连接信息
DATABASE_URL = st.secrets.get("DATABASE_URL", "")

if not DATABASE_URL:
    st.error("❌ 缺少 DATABASE_URL 配置！请在 .streamlit/secrets.toml 中添加数据库连接信息")
    st.stop()

# 创建连接池
try:
    db_pool = SimpleConnectionPool(
        1, 5,  # min 1, max 5 connections
        DATABASE_URL,
        sslmode='require'
    )
except Exception as e:
    st.error(f"❌ 数据库连接失败：{str(e)}")
    st.stop()

def get_db_connection():
    """获取数据库连接"""
    return db_pool.getconn()

def return_db_connection(conn):
    """归还数据库连接"""
    db_pool.putconn(conn)

def init_database():
    """初始化数据库表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 创建用户试用次数表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_trials (
                fingerprint TEXT PRIMARY KEY,
                free_trials INTEGER DEFAULT 1,
                unlock_success INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP,
                ip_address TEXT,
                user_agent TEXT
            )
        ''')
        
        # 创建使用记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_log (
                id SERIAL PRIMARY KEY,
                fingerprint TEXT,
                ts_code TEXT,
                start_date TEXT,
                end_date TEXT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (fingerprint) REFERENCES user_trials(fingerprint)
            )
        ''')
        
        # 创建解锁码表（便于管理）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS unlock_codes (
                code TEXT PRIMARY KEY,
                is_used INTEGER DEFAULT 0,
                used_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                max_uses INTEGER DEFAULT 1,
                current_uses INTEGER DEFAULT 0
            )
        ''')
        
        conn.commit()
        print("✅ 数据库初始化成功")
    except Exception as e:
        st.error(f"❌ 数据库初始化失败：{str(e)}")
        conn.rollback()
    finally:
        return_db_connection(conn)

def get_user_fingerprint():
    """获取用户指纹（IP + User-Agent 哈希）"""
    try:
        # Streamlit Cloud 使用 X-Forwarded-For 获取真实 IP
        headers = st.context.headers if hasattr(st.context, 'headers') else {}
        ip = headers.get("X-Forwarded-For", "unknown")
        ua = headers.get("User-Agent", "unknown")
    except:
        ip = "unknown"
        ua = "unknown"
    
    fingerprint = hashlib.md5((ip + ua).encode()).hexdigest()[:16]
    return fingerprint, ip, ua

def get_user_trials(fingerprint):
    """从数据库获取用户剩余试用次数"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            'SELECT free_trials, unlock_success FROM user_trials WHERE fingerprint = %s',
            (fingerprint,)
        )
        result = cursor.fetchone()
        
        if result:
            return result[0], bool(result[1])
        return 1, False
    finally:
        return_db_connection(conn)

def deduct_free_trial(fingerprint, ip, ua):
    """扣除一次免费试用"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 检查用户是否存在
        cursor.execute(
            'SELECT free_trials FROM user_trials WHERE fingerprint = %s',
            (fingerprint,)
        )
        result = cursor.fetchone()
        
        if result is None:
            # 新用户，初始化为1次
            cursor.execute('''
                INSERT INTO user_trials 
                (fingerprint, free_trials, ip_address, user_agent)
                VALUES (%s, %s, %s, %s)
            ''', (fingerprint, 0, ip, ua))  # 使用后变为0
        else:
            # 扣除一次
            current_trials = result[0]
            if current_trials > 0:
                cursor.execute(
                    'UPDATE user_trials SET free_trials = %s, last_used = CURRENT_TIMESTAMP WHERE fingerprint = %s',
                    (current_trials - 1, fingerprint)
                )
            else:
                conn.commit()
                return_db_connection(conn)
                return False
        
        conn.commit()
        return True
    except Exception as e:
        st.error(f"❌ 扣除试用次数失败：{str(e)}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)

def unlock_user(fingerprint, ip, ua, unlock_code=None):
    """解锁用户（无限使用）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            'SELECT * FROM user_trials WHERE fingerprint = %s',
            (fingerprint,)
        )
        
        if cursor.fetchone() is None:
            cursor.execute('''
                INSERT INTO user_trials 
                (fingerprint, unlock_success, ip_address, user_agent)
                VALUES (%s, %s, %s, %s)
            ''', (fingerprint, 1, ip, ua))
        else:
            cursor.execute(
                'UPDATE user_trials SET unlock_success = %s WHERE fingerprint = %s',
                (1, fingerprint)
            )
        
        # 标记解锁码已使用
        if unlock_code:
            cursor.execute(
                'UPDATE unlock_codes SET is_used = %s, used_by = %s, used_at = CURRENT_TIMESTAMP WHERE code = %s',
                (1, fingerprint, unlock_code)
            )
        
        conn.commit()
        return True
    except Exception as e:
        st.error(f"❌ 解锁失败：{str(e)}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)

def verify_unlock_code(code):
    """验证解锁码"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            'SELECT is_used, current_uses, max_uses FROM unlock_codes WHERE code = %s',
            (code,)
        )
        result = cursor.fetchone()
        
        if result is None:
            return False, "解锁码不存在"
        
        is_used, current_uses, max_uses = result
        
        if current_uses >= max_uses:
            return False, "解锁码已达到使用上限"
        
        return True, "验证成功"
    finally:
        return_db_connection(conn)

def check_daily_limit(fingerprint, ts_code, start_date, end_date):
    """检查是否已经生成过相同的图表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            SELECT COUNT(*) FROM usage_log 
            WHERE fingerprint = %s AND ts_code = %s AND start_date = %s AND end_date = %s
            AND DATE(used_at) = CURRENT_DATE
        ''', (fingerprint, ts_code, start_date, end_date))
        
        count = cursor.fetchone()[0]
        return count == 0
    finally:
        return_db_connection(conn)

def log_usage(fingerprint, ts_code, start_date, end_date):
    """记录使用日志"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO usage_log (fingerprint, ts_code, start_date, end_date)
            VALUES (%s, %s, %s, %s)
        ''', (fingerprint, ts_code, start_date, end_date))
        
        conn.commit()
    except Exception as e:
        st.error(f"❌ 日志记录失败：{str(e)}")
        conn.rollback()
    finally:
        return_db_connection(conn)

# Tushare 初始化
ts.set_token(st.secrets.get("TUSHARE_TOKEN", "bc66f726f32f0a61b3d1f417ca44c9ed81c19e0240e12a53bbdb5773"))
pro = ts.pro_api()

# ───────────── 函数定义 ─────────────

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
    fig = plt.figure(figsize=(18, max(12, len(prices_list) * 0.2)))
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
    plt.savefig(img_bytes, dpi=200, bbox_inches='tight', format='png')
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

# ═══════════════ 初始化应用 ═══════════════
init_database()

fingerprint, ip_address, user_agent = get_user_fingerprint()
free_trials, unlock_success = get_user_trials(fingerprint)

# 初始化 session_state
if 'unlock_attempts' not in st.session_state:
    st.session_state.unlock_attempts = 0

if 'unlock_success' not in st.session_state:
    st.session_state.unlock_success = unlock_success

CORRECT_CODE = "0304"

# ═══════════════ 主界面 ═══════════════
st.title("日线 TPO 四度空间 生成器")
st.caption("专业 Market Profile | POC/VAH/VAL 一键生成高清图 + Excel")

code = st.text_input("股票代码", value="000001.SZ", help="示例：600519.SH 或 000001.SZ")
start = st.text_input("开始日期", value="20260101", help="格式 YYYYMMDD")
end = st.text_input("结束日期", value=datetime.date.today().strftime("%Y%m%d"), help="格式 YYYYMMDD")

if st.button("生成高清 TPO 图", type="primary", use_container_width=True):
    if not (code.strip() and len(start)==8 and len(end)==8 and start.isdigit() and end.isdigit()):
        st.error("请完整填写代码和日期（8位数字 YYYYMMDD）")
        st.stop()

    # 检查是否已解锁
    if st.session_state.unlock_success:
        pass
    else:
        # 从数据库重新查询剩余次数
        free_trials, _ = get_user_trials(fingerprint)
        
        if free_trials <= 0:
            st.error("❌ 免费试用次数已用完（每设备限1次）！")
            st.info("请输入解锁码获得无限使用权，或联系微信付费购买。")
            st.stop()
        else:
            # 检查是否是重复请求
            if not check_daily_limit(fingerprint, code, start, end):
                st.warning("⚠️ 今日已生成过此图表，请不要重复生成相同的请求！")
                st.stop()
            
            # 扣除试用次数
            if not deduct_free_trial(fingerprint, ip_address, user_agent):
                st.error("❌ 免费试用次数不足")
                st.stop()
            
            st.info(f"✅ 本次为免费试用，使用后剩余 0 次（总限1次/设备）")

    with st.spinner("获取数据 → 计算TPO → 绘图（可能10-60秒）..."):
        try:
            img_bytes, poc, vah, val, excel_bytes = generate_tpo_image(code, start, end)
            
            # 记录使用日志
            log_usage(fingerprint, code, start, end)
           
            st.success(f"✅ 生成完成！POC {poc:.2f} | VAH {vah:.2f} | VAL {val:.2f}")
           
            st.info("图片像素较大，建议直接下载查看（推荐用图片查看器打开）。")
           
            st.download_button(
                label="📥 下载高清 TPO 图片 (.png)",
                data=img_bytes,
                file_name=f"{start}_{end}_{code}_TPO.png",
                mime="image/png",
                use_container_width=True
            )
           
            st.download_button(
                label="📊 下载 Excel 结果 (.xlsx)",
                data=excel_bytes,
                file_name=f"{start}_{end}_{code}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
       
        except Exception as e:
            st.error(f"❌ 生成失败：{str(e)}")
            st.info("常见原因：日期跨度太长导致图太高 → 尝试缩短日期范围，或联系我优化。")

# ═══════════════ 解锁码输入区 ═══════════════
st.markdown("### 输入解锁码使用工具（可免费试用1次）")

if st.session_state.unlock_success:
    st.success("✅ 验证码正确，已解锁！现在可以无限生成 TPO 图～")
else:
    if st.session_state.unlock_attempts < 3:
        col1, col2 = st.columns([3, 1])
        with col1:
            code_input = st.text_input("解锁码", value="", type="password", key="unlock_input")
        with col2:
            if st.button("验证", use_container_width=True):
                if not code_input.strip():
                    st.warning("请输入解锁码")
                else:
                    # 验证解锁码
                    is_valid, msg = verify_unlock_code(code_input.strip())
                    
                    if is_valid or code_input.strip() == CORRECT_CODE:
                        st.session_state.unlock_success = True
                        st.session_state.unlock_attempts = 0
                        unlock_user(fingerprint, ip_address, user_agent, code_input.strip())
                        st.success("✅ 验证码正确，已解锁！现在可以无限生成 TPO 图～")
                        st.rerun()
                    else:
                        st.session_state.unlock_attempts += 1
                        remaining = 3 - st.session_state.unlock_attempts
                        st.error(f"❌ 验证码错误，还剩 {remaining} 次尝试机会。")
    else:
        st.error("❌ 输入错误次数过多，请稍后再试或联系获取正确码。")
        st.text_input("解锁码", type="password", disabled=True)
        st.button("验证", disabled=True)

# ═══════════════ 付费引导 ═══════════════
st.markdown("---")
st.markdown("**未解锁或解锁码过期？**")
st.markdown("""
- **单次**：5元 / 次  
- **包月**：120元（首次60元） / 30天无限次  

请微信扫码付款，付款备注：  
「TPO + 示例股票代码 + 微信昵称」
""")

try:
    st.image("tpo/static/QRcode.png", caption="微信扫码加好友（支持红包/转账）", width=150)
except:
    st.info("💬 微信号：（请直接在下方输入）")

st.markdown("""
付款成功后截图发微信：**你的微信号**  
我会在几分钟到半小时内给你最新解锁码。
""")

st.caption("数据来源于Tushare | 仅供学习交流 | 如有疑问加微信")
