import streamlit as st
import streamlit.components.v1 as components  # noqa: F401
import requests
import pandas as pd
import time
from datetime import date, datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

st.set_page_config(
    page_title="MOEX Оповещения",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
.block-container {padding-top: 1rem; padding-bottom: 2rem; max-width: 1400px;}
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


# ================= Рыночная цена (LAST) с ISS — кэш 3 сек =================
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
    url = (f"https://iss.moex.com/iss/engines/{engine}/markets/{market}"
           f"/securities/{secid}.json")
    data = iss_get_json(url,
                        params={"iss.meta": "off", "iss.only": "marketdata"})
    if data is None:
        return None
    md = data.get("marketdata", {})
    cols = md.get("columns", [])
    rows = md.get("data", [])
    if not rows or not cols:
        return None
    rd = dict(zip(cols, rows[0]))
    for key in ("LAST", "MARKETPRICE", "LCLOSEPRICE",
                "LASTTOPREVPRICE", "OPEN", "SETTLEPRICE"):
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


# ================= UI =================
st.title("Оповещения по уровням")
st.caption("Сравнение рыночной цены (LAST) с уровнями покупок / продаж. "
           "Автообновление каждые 3 секунды.")

uploaded = st.file_uploader(
    "Excel-файл (.xlsx) — колонки: Тикер БА · Категория БА · Уровень покупок · Уровень продаж",
    type=["xlsx", "xls"],
    key="alerts_xlsx_uploader")

if "alerts_df" not in st.session_state:
    st.session_state.alerts_df = None

if uploaded is not None:
    try:
        _xls = pd.read_excel(uploaded)
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
            st.error(f"В файле нет колонок: {', '.join(missing)}")
        else:
            _xls["Уровень покупок"] = pd.to_numeric(
                _xls["Уровень покупок"], errors="coerce")
            _xls["Уровень продаж"] = pd.to_numeric(
                _xls["Уровень продаж"], errors="coerce")
            _xls = _xls.dropna(subset=["Тикер БА", "Уровень покупок",
                                       "Уровень продаж"])
            st.session_state.alerts_df = _xls
            st.success(f"Загружено {len(_xls)} строк.")
    except Exception as e:
        st.error(f"Не удалось прочитать файл: {e}")

if st.session_state.alerts_df is not None:
    _hc1, _hc2 = st.columns([1, 5])
    with _hc1:
        if st.button("Очистить таблицу", use_container_width=True,
                     key="alerts_clear_btn"):
            st.session_state.alerts_df = None
            st.rerun()


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

    buy_bg   = "#d9ffe0" if buy_active  else "#f0f4f8"
    buy_fg   = "#0a3d0e" if buy_active  else "#7f9bb3"
    buy_brd  = "#00a651" if buy_active  else "#e2edf4"
    sell_bg  = "#d9ffe0" if sell_active else "#f0f4f8"
    sell_fg  = "#0a3d0e" if sell_active else "#7f9bb3"
    sell_brd = "#00a651" if sell_active else "#e2edf4"
    buy_mark  = "☑" if buy_active  else "☐"
    sell_mark = "☑" if sell_active else "☐"

    return f"""
    <div style="border:1px solid #e2edf4; border-radius:14px; padding:12px 14px;
                background:#fff; margin-bottom:10px;
                box-shadow:0 2px 8px rgba(0,0,0,.05);">
      <div style="font-size:1.15rem; font-weight:800; color:#1a3b4f;
                  line-height:1.1;">{ticker}</div>
      <div style="font-size:.7rem; color:#7f9bb3; margin-top:2px;
                  text-transform:uppercase; letter-spacing:.05em;">
        {category}</div>
      <div style="font-size:1rem; font-weight:700; color:#1c5a7a;
                  margin:6px 0 10px 0;">{price_str}</div>

      <div style="display:flex; gap:8px;">
        <div style="flex:1; background:#f3e8ff; border-radius:10px;
                    padding:8px 10px;">
          <div style="font-size:.6rem; color:#5c0099; font-weight:800;
                      text-transform:uppercase; letter-spacing:.05em;">
            Уровень покупок</div>
          <div style="font-size:1.1rem; font-weight:800; color:#5c0099;
                      line-height:1.2; margin-top:2px;">{lvl_buy:.2f}</div>
          <div style="font-size:.78rem; color:#7a5c99; font-weight:600;
                      margin-top:1px;">{buy_dev_str}</div>
        </div>
        <div style="flex:1; background:#fff0fe; border-radius:10px;
                    padding:8px 10px;">
          <div style="font-size:.6rem; color:#a3139e; font-weight:800;
                      text-transform:uppercase; letter-spacing:.05em;">
            Уровень продаж</div>
          <div style="font-size:1.1rem; font-weight:800; color:#a3139e;
                      line-height:1.2; margin-top:2px;">{lvl_sell:.2f}</div>
          <div style="font-size:.78rem; color:#997a99; font-weight:600;
                      margin-top:1px;">{sell_dev_str}</div>
        </div>
      </div>

      <div style="display:flex; gap:8px; margin-top:10px;">
        <div style="flex:1; text-align:center; padding:7px; border-radius:8px;
                    font-size:.85rem; font-weight:800;
                    background:{buy_bg}; color:{buy_fg};
                    border:1px solid {buy_brd};">
          {buy_mark} Покупка
        </div>
        <div style="flex:1; text-align:center; padding:7px; border-radius:8px;
                    font-size:.85rem; font-weight:800;
                    background:{sell_bg}; color:{sell_fg};
                    border:1px solid {sell_brd};">
          {sell_mark} Продажа
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

    # --- Экспорт CSV + сводка сверху ---
    export_df = pd.DataFrame([{
        "Тикер БА": r["ticker"],
        "Категория БА": r["category"],
        "Уровень покупок": r["lvl_buy"],
        "Откл. покупок, %": (round(r["buy_dev_pct"], 4)
                             if r["buy_dev_pct"] is not None else None),
        "Уровень продаж": r["lvl_sell"],
        "Откл. продаж, %": (round(r["sell_dev_pct"], 4)
                            if r["sell_dev_pct"] is not None else None),
        "Рыночная цена": r["market_price"],
        "Покупка активна": r["buy_active"],
        "Продажа активна": r["sell_active"],
    } for r in rows])

    _ec1, _ec2 = st.columns([1.2, 4])
    with _ec1:
        st.download_button(
            "📤 Экспорт в CSV",
            data=export_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"alerts_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
            key="alerts_export_csv",
        )
    with _ec2:
        n_buy = sum(1 for r in rows if r["buy_active"])
        n_sell = sum(1 for r in rows if r["sell_active"])
        st.caption(
            f"Всего: **{len(rows)}** · "
            f"🟢 Покупка активна: **{n_buy}** · "
            f"🟢 Продажа активна: **{n_sell}** · "
            f"🔄 обновление 3 сек")

    # --- Сетка карточек 2×N ---
    for i in range(0, len(rows), 2):
        cols = st.columns(2, gap="small")
        for j in range(2):
            if i + j >= len(rows):
                break
            with cols[j]:
                st.markdown(render_card(rows[i + j]),
                            unsafe_allow_html=True)


# ================= Запуск =================
if st.session_state.get("alerts_df") is not None:
    render_alerts_live()
else:
    st.info("Загрузите Excel-файл, чтобы увидеть оповещения в виде карточек.")
