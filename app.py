import streamlit as st
import requests
import pandas as pd
import time
from datetime import date, datetime, timedelta
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(
    page_title="MOEX Оповещения",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
.block-container {padding-top: 3rem; padding-bottom: 2rem; max-width: 520px;}
h1 {font-size: 1.15rem !important; font-weight: 700 !important;
    margin-top: 1.2rem !important; margin-bottom: .3rem !important;}
</style>
""", unsafe_allow_html=True)

# ================= Устойчивый HTTP-клиент к ISS =================
def _make_iss_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({"User-Agent": "MOEX-Alerts/1.0",
                      "Accept": "application/json, */*"})
    return s


_ISS_SESSION = _make_iss_session()


def iss_get_json(url, params=None, timeout=15):
    for attempt in range(3):
        try:
            r = _ISS_SESSION.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            if attempt == 2:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


# ================= Рыночная цена =================
@st.cache_data(ttl=3, show_spinner=False)
def fetch_last_price_from_iss(secid: str, asset_type_ui: str):
    if not secid:
        return None

    if asset_type_ui in ("Фьючерс", "Валюта", "Товар"):
        engine, market = "futures", "forts"
    elif asset_type_ui == "Индекс":
        engine, market = "stock", "index"
    else:
        engine, market = "stock", "shares"

    # ---- 1. Основной источник: последняя D1-свеча ----
    try:
        end = datetime.now()
        start = end - timedelta(days=20)
        url_c = (f"https://iss.moex.com/iss/engines/{engine}/markets/{market}"
                 f"/securities/{secid}/candles.json")
        data_c = iss_get_json(url_c, params={
            "from": start.strftime("%Y-%m-%d"),
            "till": end.strftime("%Y-%m-%d"),
            "interval": 24,
            "iss.meta": "off",
        })
        if data_c:
            cols = data_c.get("candles", {}).get("columns", [])
            rows = data_c.get("candles", {}).get("data", [])
            if rows and cols:
                df = pd.DataFrame(rows, columns=cols)
                if "close" in df.columns:
                    df = df.dropna(subset=["close"])
                    df = df[df["close"] > 0]
                    df = df.sort_values("begin")
                    if not df.empty:
                        return float(df.iloc[-1]["close"])
    except Exception:
        pass

    # ---- 2. Fallback: marketdata ----
    url = (f"https://iss.moex.com/iss/engines/{engine}/markets/{market}"
           f"/securities/{secid}.json")
    data = iss_get_json(url,
                        params={"iss.meta": "off",
                                "iss.only": "marketdata"})
    if data:
        md = data.get("marketdata", {})
        cols = md.get("columns", [])
        rows = md.get("data", [])
        if rows and cols:
            rd = dict(zip(cols, rows[0]))
            for key in ("LAST", "MARKETPRICE", "SETTLEPRICE"):
                val = rd.get(key)
                if val and val > 0:
                    return float(val)

    return None


@st.cache_data(ttl=3600, show_spinner=False)
def resolve_underlying_secid(asset_code: str, asset_type_ui: str):
    if asset_type_ui == "Акция":
        return asset_code.upper()
    if asset_type_ui == "Индекс":
        idx_map = {"RTS": "RTSI", "MIX": "IMOEX"}
        return idx_map.get(asset_code.upper(), asset_code.upper())
    if asset_type_ui in ("Фьючерс", "Валюта", "Товар"):
        url = ("https://iss.moex.com/iss/engines/futures/markets/forts/"
               "securities.json")
        data = iss_get_json(url,
                            params={"iss.meta": "off", "iss.only": "securities"})
        if data is None:
            return None
        try:
            cols = data["securities"]["columns"]
            rows = data["securities"]["data"]
            df = pd.DataFrame(rows, columns=cols)
            if "ASSETCODE" not in df.columns:
                return None
            df = df[df["ASSETCODE"] == asset_code.upper()]
            if df.empty:
                return None
            today_str = date.today().isoformat()
            if "LASTTRADEDATE" in df.columns:
                df_live = df[df["LASTTRADEDATE"] >= today_str]
                df = df_live if not df_live.empty else df
                df = df.dropna(subset=["LASTTRADEDATE"])
                df = df.sort_values("LASTTRADEDATE")
            return df.iloc[0]["SECID"] if not df.empty else None
        except Exception:
            return None
    return asset_code.upper()


# ================= Загрузка Google Sheets =================
@st.cache_data(ttl=60, show_spinner=False)
def load_google_sheet(sheet_url: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(sheet_url)
        return df
    except Exception:
        try:
            df = pd.read_excel(sheet_url)
            return df
        except Exception as e:
            st.error(f"Не удалось загрузить таблицу: {e}")
            return pd.DataFrame()


# ================= UI =================
# Отступ сверху, чтобы заголовок не обрезался на смартфоне
st.markdown("<div style='height:2.5rem;'></div>", unsafe_allow_html=True)

st.title("Оповещения")

# 🔗 Ссылка на экспорт Google Таблицы (CSV или XLSX)
SHEET_EXPORT_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1BhFbdaXC3tgoURYkuyZeOJkm16FSC5xM/export?format=xlsx"
)

if "alerts_df" not in st.session_state:
    st.session_state.alerts_df = None
if "alerts_loaded_count" not in st.session_state:
    st.session_state.alerts_loaded_count = 0

_xls = load_google_sheet(SHEET_EXPORT_URL)

if not _xls.empty:
    _col_map = {}
    for c in _xls.columns:
        c_str = str(c).strip().lower()
        if "тикер" in c_str:
            _col_map[c] = "Тикер БА"
        elif "категор" in c_str:
            _col_map[c] = "Категория БА"
        elif "покуп" in c_str or "bid" in c_str:
            _col_map[c] = "Уровень покупок"
        elif "продаж" in c_str or "ask" in c_str:
            _col_map[c] = "Уровень продаж"
    _xls = _xls.rename(columns=_col_map)

    required = ["Тикер БА", "Категория БА",
                "Уровень покупок", "Уровень продаж"]
    missing = [c for c in required if c not in _xls.columns]
    if missing:
        st.error(f"В таблице нет колонок: {', '.join(missing)}")
    else:
        _xls["Уровень покупок"] = pd.to_numeric(
            _xls["Уровень покупок"], errors="coerce")
        _xls["Уровень продаж"] = pd.to_numeric(
            _xls["Уровень продаж"], errors="coerce")
        _xls = _xls.dropna(subset=["Тикер БА", "Уровень покупок",
                                   "Уровень продаж"])
        st.session_state.alerts_df = _xls
        st.session_state.alerts_loaded_count = len(_xls)


# ================= Подготовка данных =================
def _compute_rows(df_alerts: pd.DataFrame):
    out_rows = []
    for _, row in df_alerts.iterrows():
        ticker = str(row["Тикер БА"]).strip()
        category = str(row["Категория БА"]).strip()
        lvl_buy = float(row["Уровень покупок"])
        lvl_sell = float(row["Уровень продаж"])

        secid = resolve_underlying_secid(ticker.upper(), category)
        last = fetch_last_price_from_iss(secid, category) if secid else None

        if last and last > 0:
            buy_dev_pct = (lvl_buy - last) / last * 100.0
            sell_dev_pct = (lvl_sell - last) / last * 100.0
            buy_active = last <= lvl_buy
            sell_active = last >= lvl_sell
        else:
            buy_dev_pct = None
            sell_dev_pct = None
            buy_active = False
            sell_active = False

        out_rows.append({
            "ticker": ticker,
            "category": category,
            "lvl_buy": lvl_buy,
            "lvl_sell": lvl_sell,
            "buy_dev_pct": buy_dev_pct,
            "sell_dev_pct": sell_dev_pct,
            "market_price": last,
            "buy_active": buy_active,
            "sell_active": sell_active,
        })
    return out_rows


# ================= Рендер карточки =================
def render_card(r: dict) -> str:
    ticker = r["ticker"]
    category = r["category"]
    last = r["market_price"]
    lvl_buy = r["lvl_buy"]
    lvl_sell = r["lvl_sell"]
    buy_dev_pct = r["buy_dev_pct"]
    sell_dev_pct = r["sell_dev_pct"]
    buy_active = r["buy_active"]
    sell_active = r["sell_active"]

    price_str = f"{last:,.2f} ₽" if last else "— ₽"
    buy_dev_str = f"{buy_dev_pct:+.2f} %" if buy_dev_pct is not None else "—"
    sell_dev_str = f"{sell_dev_pct:+.2f} %" if sell_dev_pct is not None else "—"

    buy_bg  = "#0a8f3c" if buy_active  else "#e6e9ec"
    buy_fg  = "#ffffff" if buy_active  else "#5a6b78"
    sell_bg = "#0a8f3c" if sell_active else "#e6e9ec"
    sell_fg = "#ffffff" if sell_active else "#5a6b78"
    buy_mark  = "✓" if buy_active  else "·"
    sell_mark = "✓" if sell_active else "·"

    return f"""
    <div style="border:1px solid #cfd6dc; border-radius:8px; padding:10px 12px;
                background:#ffffff; margin-bottom:8px;">
      <div style="display:flex; justify-content:space-between; align-items:baseline;">
        <div style="font-size:1.15rem; font-weight:700; color:#111; line-height:1.1;">
          {ticker}</div>
        <div style="font-size:1.05rem; font-weight:700; color:#111;">
          {price_str}</div>
      </div>
      <div style="font-size:.68rem; color:#5a6b78; margin-top:2px;
                  text-transform:uppercase; letter-spacing:.06em;">
        {category}</div>

      <div style="display:flex; gap:6px; margin-top:10px;">
        <div style="flex:1; background:#f2f4f6; border:1px solid #d8dee3;
                    border-radius:6px; padding:7px 9px;">
          <div style="font-size:.6rem; color:#333; font-weight:700;
                      text-transform:uppercase; letter-spacing:.06em;">
            Покупка</div>
          <div style="font-size:1.02rem; font-weight:700; color:#111;
                      line-height:1.2; margin-top:2px;">{lvl_buy:.2f}</div>
          <div style="font-size:.76rem; color:#333; margin-top:1px;">
            {buy_dev_str}</div>
        </div>
        <div style="flex:1; background:#f2f4f6; border:1px solid #d8dee3;
                    border-radius:6px; padding:7px 9px;">
          <div style="font-size:.6rem; color:#333; font-weight:700;
                      text-transform:uppercase; letter-spacing:.06em;">
            Продажа</div>
          <div style="font-size:1.02rem; font-weight:700; color:#111;
                      line-height:1.2; margin-top:2px;">{lvl_sell:.2f}</div>
          <div style="font-size:.76rem; color:#333; margin-top:1px;">
            {sell_dev_str}</div>
        </div>
      </div>

      <div style="display:flex; gap:6px; margin-top:8px;">
        <div style="flex:1; text-align:center; padding:6px;
                    border-radius:6px; font-size:.82rem; font-weight:700;
                    background:{buy_bg}; color:{buy_fg};
                    letter-spacing:.05em;">
          {buy_mark} ПОКУПКА
        </div>
        <div style="flex:1; text-align:center; padding:6px;
                    border-radius:6px; font-size:.82rem; font-weight:700;
                    background:{sell_bg}; color:{sell_fg};
                    letter-spacing:.05em;">
          {sell_mark} ПРОДАЖА
        </div>
      </div>
    </div>
    """


# ================= Живой рендер с автообновлением =================
@st.fragment(run_every="3s")
def render_alerts_live():
    df_alerts = st.session_state.get("alerts_df")
    if df_alerts is None or df_alerts.empty:
        return

    rows = _compute_rows(df_alerts)
    n_buy = sum(1 for r in rows if r["buy_active"])
    n_sell = sum(1 for r in rows if r["sell_active"])

    _loaded = st.session_state.get("alerts_loaded_count", len(rows))
    st.caption(f"Загружено: {_loaded} · "
               f"Всего: {len(rows)} · Покупка: {n_buy} · Продажа: {n_sell}")

    for r in rows:
        st.markdown(render_card(r), unsafe_allow_html=True)


# ================= Запуск =================
if st.session_state.get("alerts_df") is not None:
    render_alerts_live()
else:
    st.info("Таблица не загружена. Проверьте доступ к Google Sheets.")
