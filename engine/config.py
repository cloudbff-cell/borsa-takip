"""Evren, parametreler ve ayarlar.

Aday listesi geniş tutulur; motor her gün piyasa değerine göre sıralayıp
ilk TOP_N şirketi ve her sektörün liderlerini seçer. Listeye hisse eklemek
için CANDIDATES'a sembol eklemen yeterli.
"""

# Yahoo Finance sembolleri (BRK.B -> BRK-B)
CANDIDATES = [
    # Teknoloji / yarı iletken / yazılım
    "NVDA", "AAPL", "MSFT", "AVGO", "MU", "AMD", "ORCL", "CSCO", "INTC", "PLTR",
    "LRCX", "AMAT", "KLAC", "DELL", "PANW", "CRWD", "TXN", "ANET", "SNDK", "MRVL",
    "APH", "IBM", "ADI", "QCOM", "CRM", "NOW", "ADBE", "INTU", "STX", "WDC", "APP",
    # İletişim
    "GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T",
    # Tüketici (döngüsel)
    "AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "NKE",
    # Tüketici (temel)
    "WMT", "COST", "PG", "KO", "PEP", "PM",
    # Sağlık
    "LLY", "JNJ", "ABBV", "UNH", "MRK", "TMO", "ABT", "ISRG", "AMGN",
    # Finans
    "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "C", "BX", "SCHW",
    # Sanayi
    "GE", "CAT", "RTX", "GEV", "HON", "UBER", "UNP", "ETN", "BA", "DE", "LMT",
    # Enerji
    "XOM", "CVX", "COP",
    # Hammadde
    "LIN", "SHW", "FCX", "NEM", "ECL",
    # Kamu hizmetleri
    "NEE", "SO", "DUK", "CEG", "VST",
    # Gayrimenkul
    "PLD", "AMT", "WELL", "EQIX",
    # Yeni halka arzlar / büyük şirketler (Yahoo'da yoksa sessizce atlanır)
    "SPCX",
]

# ABD dışı ama ABD'de işlem gören dev şirketler (ADR). İstersen True yap.
INCLUDE_ADRS = False
ADRS = ["TSM", "ASML", "ARM", "SHOP", "NVS", "AZN", "SAP", "BABA", "HSBC"]

BENCHMARK = "SPY"
TOP_N = 50
LEADERS_PER_SECTOR = 3

# Teknik parametreler (Pine Script ile aynı tutuldu)
EMA_FAST, EMA_MID, EMA_SLOW = 20, 50, 200
RSI_LEN = 14
RSI_OVERSOLD, RSI_PULLBACK, RSI_OVERBOUGHT = 30, 40, 70
BREAKOUT_LEN = 20
VOLUME_MULT = 1.5
ATR_LEN = 14
STOP_ATR, T1_ATR, T2_ATR = 2.0, 2.0, 4.0
# Giriş kuralları (puan 100 üzerinden)
ENTRY_SIGNAL_MIN_SCORE = 65    # son 3 günde AL sinyali varsa gereken en düşük puan
ENTRY_TREND_MIN_SCORE = 75     # sinyal yoksa: yükseliş trendi + bu puan + 20 EMA'ya geri çekilmede alım
# (65/75, 10 yıllık testte 60/70, 70/80 ve 75/85'e göre en iyi işlem başı sonucu ve en az kriz kaybını verdi)
EARNINGS_GUARD_DAYS = 3        # bilançoya bu kadar gün kala yeni alım sinyali "riskli" işaretlenir
FORWARD_DAYS = 20              # geçmiş sinyal başarısı ölçülürken bakılan gün sayısı

# Mevsimsellik
SEASONAL_YEARS = 10

# Önbellek süresi (saat): temel veriler günde bir yenilenir
FUNDAMENTALS_TTL_HOURS = 20

SECTOR_TR = {
    "Technology": "Teknoloji",
    "Communication Services": "İletişim",
    "Consumer Cyclical": "Tüketici (Döngüsel)",
    "Consumer Defensive": "Tüketici (Temel)",
    "Healthcare": "Sağlık",
    "Financial Services": "Finans",
    "Industrials": "Sanayi",
    "Energy": "Enerji",
    "Basic Materials": "Hammadde",
    "Utilities": "Kamu Hizmetleri",
    "Real Estate": "Gayrimenkul",
}

# ---------- Otomatik işlem (Alpaca) ----------
# Bot yalnızca TRADING_ENABLED=true ortam değişkeni varsa emir verir.
# ALPACA_PAPER=false yapılmadıkça her zaman SANAL PARA (paper) hesabı kullanılır.
RISK_PER_TRADE = 0.01        # işlem başına sermayenin %1'i riske girer (giriş - stop)
MAX_POSITIONS = 5            # aynı anda en fazla açık pozisyon (bekleyen alım emirleri dahil)
MAX_POSITION_PCT = 0.20      # tek pozisyon sermayenin en fazla %20'si
DAILY_LOSS_LIMIT = 0.02      # gün içi kayıp %2'yi aşarsa yeni alım yapılmaz
TAKE_PROFIT = "t2"           # kâr al emri: "t1" veya "t2"
BREAKEVEN_AT_T1 = True       # fiyat Hedef 1'e ulaşınca stop giriş fiyatına çekilir
EXIT_SIGNALS = ["S_BREAKDOWN", "S_TREND", "S_DEATH"]   # elde varsa kapanışta bu sinyallerde çık
EXIT_BEFORE_EARNINGS = True  # bilançodan 1 gün önce pozisyonu kapat
